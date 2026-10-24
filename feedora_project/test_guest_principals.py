import json
import uuid

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from accounts.models import Organization
from surveys.models import Question, Response, Survey, SurveyAttempt
from feedora_project.api_views import (
    _ensure_policy_revision,
    _normalized_survey_policy,
    _policy_revision_version,
    _survey_policy_version,
)


User = get_user_model()


class GuestPrincipalIntegrityTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.creator = User.objects.create_user(
            username='guest-owner@example.test', email='guest-owner@example.test',
            password='Password123!', role='creator',
        )
        self.organization = Organization.objects.create(created_by=self.creator, org_name='Guest Test Org')
        self.survey = Survey.objects.create(
            created_by=self.creator, organization_scope=self.organization,
            title='Guest identity survey', features={'points': {'enabled': True}},
        )
        Question.objects.create(survey=self.survey, code='Q1', text='Rate this', question_type='likert', rating_max=5)

    def new_guest(self):
        response = self.client.post('/api/v1/guest/session/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        return data['guestParticipantId'], data['guestToken']

    def submit(self, token, **payload):
        return self.client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/submit/',
            data=json.dumps({'submissionId': str(uuid.uuid4()), 'answers': {'Q1': 4}, **payload}),
            content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=token,
        )

    def test_client_cannot_choose_or_reset_guest_identity(self):
        guest_id, token = self.new_guest()
        first = self.submit(token, guestParticipantId='forged-identity')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(Response.objects.get(pk=first.json()['responseId']).guest_participant_id, guest_id)
        self.assertEqual(self.submit(token).status_code, 409)

        other_id, other_token = self.new_guest()
        second = self.submit(other_token)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(Response.objects.get(pk=second.json()['responseId']).guest_participant_id, other_id)

    def test_claim_links_guest_history_to_account_without_rewriting_response(self):
        guest_id, token = self.new_guest()
        submitted = self.submit(token)
        response_row = Response.objects.get(pk=submitted.json()['responseId'])
        account = User.objects.create_user(
            username='claimant@example.test', email='claimant@example.test',
            password='Password123!', role='respondent',
        )
        self.client.force_login(account)
        claim = self.client.post('/api/v1/guest/claim/', HTTP_X_FEEDORA_GUEST_TOKEN=token)
        self.assertEqual(claim.status_code, 200)
        self.assertEqual(self.client.post('/api/v1/guest/claim/', HTTP_X_FEEDORA_GUEST_TOKEN=token).status_code, 200)
        response_row.refresh_from_db()
        self.assertIsNone(response_row.user_id)

        history = self.client.get('/api/v1/respondent/history/').json()['history']
        self.assertEqual([item['responseId'] for item in history], [response_row.id])
        self.assertEqual(history[0]['surveyUuid'], str(self.survey.access_uuid))
        leaderboard = self.client.get(f'/api/v1/leaderboard/?entity_id=org-{self.organization.id}').json()
        contributor = next(item for item in leaderboard['contributors'] if item['userId'] == f'user-{account.id}')
        self.assertEqual(contributor['surveysCompleted'], 1)
        self.client.logout()
        rejected = self.submit(token)
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.json()['code'], 'guest_session_claimed')
        self.assertEqual(self.client.get('/api/v1/respondent/history/', HTTP_X_FEEDORA_GUEST_TOKEN=token).status_code, 401)

    def test_creator_json_cannot_escalate_scope_to_platform(self):
        self.client.force_login(self.creator)
        result = self.client.post(
            '/api/v1/surveys/create/',
            data=json.dumps({
                'title': 'Organization survey',
                'features': {'is_global': True, 'scope': {'kind': 'platform'}},
                'questions': [{'text': 'One question'}],
            }),
            content_type='application/json',
        )
        self.assertEqual(result.status_code, 201)
        created = Survey.objects.get(access_uuid=result.json()['uuid'])
        self.assertEqual(created.scope_kind, 'organization')
        self.assertEqual(created.organization_scope_id, self.organization.id)
        self.assertEqual(created.features.get('is_global'), None)
        self.assertEqual(created.features.get('scope'), None)

    def test_survey_creation_rejects_unknown_policy_fields_before_writing(self):
        self.client.force_login(self.creator)
        before = Survey.objects.count()
        result = self.client.post(
            '/api/v1/surveys/create/',
            data=json.dumps({
                'title': 'Unsupported create rule',
                'features': {
                    'accessPolicy': {'requiresAccount': False, 'maxAttempts': 1, 'scoreGateMode': 'client'},
                    'sequence': {'groupId': 'research', 'order': 1, 'enforce': True, 'window': 3},
                },
                'questions': [{'text': 'Question'}],
            }),
            content_type='application/json',
        )

        self.assertEqual(result.status_code, 400)
        self.assertEqual(result.json()['field'], 'accessPolicy.scoreGateMode')
        self.assertEqual(Survey.objects.count(), before)

    def test_survey_creation_rejects_fractional_prerequisite_delay(self):
        self.client.force_login(self.creator)
        result = self.client.post(
            '/api/v1/surveys/create/',
            data=json.dumps({
                'title': 'Fractional delay',
                'features': {
                    'accessPolicy': {'requiresAccount': False, 'maxAttempts': 1},
                    'prerequisites': {'completedFormIds': [str(self.survey.access_uuid)], 'unlockDelayMs': 12.5},
                },
                'questions': [{'text': 'Question'}],
            }),
            content_type='application/json',
        )

        self.assertEqual(result.status_code, 400)
        self.assertEqual(result.json()['field'], 'prerequisites[0].unlockDelayMs')

    def test_survey_creation_rejects_contradictory_repeat_limit(self):
        self.client.force_login(self.creator)
        before = Survey.objects.count()
        for allow_redo, max_attempts in ((False, 3), (True, 1)):
            result = self.client.post(
                '/api/v1/surveys/create/',
                data=json.dumps({
                    'title': 'Contradictory attempt policy',
                    'features': {'accessPolicy': {'allowRedo': allow_redo, 'maxAttempts': max_attempts}},
                    'questions': [{'text': 'Question'}],
                }),
                content_type='application/json',
            )

            self.assertEqual(result.status_code, 400)
            self.assertEqual(result.json()['field'], 'accessPolicy.allowRedo')
        self.assertEqual(Survey.objects.count(), before)

    def test_survey_creation_derives_repeat_flag_from_max_attempts(self):
        self.client.force_login(self.creator)
        result = self.client.post(
            '/api/v1/surveys/create/',
            data=json.dumps({
                'title': 'Repeatable study',
                'features': {'accessPolicy': {'maxAttempts': 3}},
                'questions': [{'text': 'Question'}],
            }),
            content_type='application/json',
        )

        self.assertEqual(result.status_code, 201, result.content)
        created = Survey.objects.get(access_uuid=result.json()['uuid'])
        self.assertTrue(created.features['accessPolicy']['allowRedo'])
        self.assertEqual(created.features['accessPolicy']['maxAttempts'], 3)

    def test_policy_fingerprint_includes_legacy_attempt_toggle(self):
        before = _survey_policy_version(self.survey)
        self.survey.features = {'allow_multiple_submissions': True}
        self.survey.save(update_fields=['features'])
        self.assertNotEqual(before, _survey_policy_version(self.survey))

    def test_policy_revision_fingerprint_matches_canonical_legacy_attempt_limit(self):
        self.survey.features = {'accessPolicy': {'allowRedo': True, 'maxAttempts': 1001}}
        self.survey.save(update_fields=['features'])

        revision = _ensure_policy_revision(self.survey, self.creator)

        self.assertEqual(revision.version, _survey_policy_version(self.survey))
        self.assertEqual(revision.document['policy']['attempts']['maxAttempts'], 1)
        self.assertIn('invalid_legacy_policy', revision.document['warnings'])
        self.assertEqual(_normalized_survey_policy(self.survey)['access']['maxAttempts'], 1)
        self.assertEqual(revision.version, _policy_revision_version(revision.document))

    def test_unsupported_nested_score_input_is_preserved_and_fingerprinted(self):
        self.survey.features = {'prerequisites': {'min_score_percent': 75}}
        self.survey.save(update_fields=['features'])
        before = _ensure_policy_revision(self.survey, self.creator)

        self.survey.features = {'prerequisites': {'min_score_percent': 80}}
        self.survey.save(update_fields=['features'])
        after = _ensure_policy_revision(self.survey, self.creator)

        self.assertNotEqual(before.version, after.version)
        self.assertEqual(before.document['unsupportedScoreInputs']['prerequisites']['min_score_percent'], 75)
        self.assertEqual(after.document['unsupportedScoreInputs']['prerequisites']['min_score_percent'], 80)
        self.assertEqual(after.version, _policy_revision_version(after.document))

    def test_legacy_api_projection_uses_canonical_policy_for_all_rules(self):
        prerequisite = Survey.objects.create(
            created_by=self.creator,
            organization_scope=self.organization,
            title='Projection prerequisite',
        )
        self.survey.features = {
            'accessPolicy': {
                'requiresAccount': True,
                'organizationMembershipRequired': True,
                'maxAttempts': 2,
                'rewardStrategy': 'score_delta',
                'cooldownMinutes': 1.5,
            },
            'prerequisites': {
                'completedFormIds': [{'surveyId': str(prerequisite.access_uuid), 'unlockDelayMs': 1200}],
            },
            'sequence': {'groupId': 'wave-a', 'order': 2, 'enforce': True},
        }
        self.survey.save(update_fields=['features'])

        projection = _normalized_survey_policy(self.survey)

        self.assertEqual(projection['access'], {
            'allowRedo': True,
            'maxAttempts': 2,
            'rewardStrategy': 'score_delta',
            'requiresAccount': True,
            'organizationMembershipRequired': True,
            'cooldownMinutes': 1.5,
        })
        self.assertEqual(projection['prerequisites'], [str(prerequisite.access_uuid)])
        self.assertEqual(projection['unlockDelayMs'], 1200)
        self.assertEqual(projection['sequence'], {'group': 'wave-a', 'order': 2, 'enforce': True})

    def test_repeated_attempts_remain_distinct_and_catalog_uses_verified_guest(self):
        self.survey.features = {'allow_multiple_submissions': True}
        self.survey.save(update_fields=['features'])
        guest_id, token = self.new_guest()
        self.assertEqual(self.submit(token).status_code, 200)
        self.assertEqual(self.submit(token).status_code, 200)

        unverified = self.client.get('/api/v1/surveys/', {'guest_participant_id': guest_id}).json()
        verified = self.client.get('/api/v1/surveys/', HTTP_X_FEEDORA_GUEST_TOKEN=token).json()
        self.assertEqual(unverified['surveys'][0]['attemptsUsed'], 0)
        self.assertEqual(verified['surveys'][0]['attemptsUsed'], 2)
        history = self.client.get('/api/v1/respondent/history/', HTTP_X_FEEDORA_GUEST_TOKEN=token).json()['history']
        self.assertEqual(len(history), 2)
        self.assertEqual(len({item['responseId'] for item in history}), 2)

    def test_sign_in_before_guest_retry_applies_the_guest_attempt_limit(self):
        guest_id, token = self.new_guest()
        first = self.submit(token)
        self.assertEqual(first.status_code, 200)
        account = User.objects.create_user(
            username='retry-owner@example.test', email='retry-owner@example.test',
            password='Password123!', role='respondent',
        )
        self.client.force_login(account)
        retry = self.client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/submit/',
            data=json.dumps({'submissionId': str(uuid.uuid4()), 'answers': {'Q1': 3}}),
            content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=token,
        )
        self.assertEqual(retry.status_code, 409)
        self.assertEqual(retry.json()['code'], 'attempt_limit_reached')
        from accounts.models import GuestPrincipal
        self.assertEqual(GuestPrincipal.objects.get(pk=guest_id).claimed_by_id, account.id)
        self.assertEqual(Response.objects.filter(survey=self.survey).count(), 1)

    def test_authenticated_attempt_start_claims_guest_before_policy_evaluation(self):
        from accounts.models import GuestPrincipal

        guest_id, token = self.new_guest()
        first = self.submit(token)
        self.assertEqual(first.status_code, 200)
        account = User.objects.create_user(
            username='attempt-start-claimant@example.test', email='attempt-start-claimant@example.test',
            password='Password123!', role='respondent',
        )
        self.client.force_login(account)

        result = self.client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/attempts/start/',
            data=json.dumps({'attemptId': str(uuid.uuid4())}),
            content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=token,
        )

        self.assertEqual(result.status_code, 409, result.content)
        self.assertEqual(result.json()['code'], 'attempt_limit_reached')
        self.assertEqual(GuestPrincipal.objects.get(pk=guest_id).claimed_by_id, account.id)
        self.assertFalse(SurveyAttempt.objects.filter(survey=self.survey).exists())
        self.assertEqual(Response.objects.filter(survey=self.survey).count(), 1)

    def test_claim_consolidates_guest_and_account_active_attempts(self):
        guest_id, token = self.new_guest()
        guest_client = Client()
        account = User.objects.create_user(
            username='reservation-claimant@example.test', email='reservation-claimant@example.test',
            password='Password123!', role='respondent',
        )
        account_client = Client()
        account_client.force_login(account)
        account_start = account_client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/attempts/start/',
            data=json.dumps({'attemptId': str(uuid.uuid4())}), content_type='application/json',
        )
        guest_start = guest_client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/attempts/start/',
            data=json.dumps({'attemptId': str(uuid.uuid4())}), content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=token,
        )
        self.assertEqual(account_start.status_code, 201, account_start.content)
        self.assertEqual(guest_start.status_code, 201, guest_start.content)

        claim = account_client.post('/api/v1/guest/claim/', HTTP_X_FEEDORA_GUEST_TOKEN=token)

        self.assertEqual(claim.status_code, 200, claim.content)
        active = SurveyAttempt.objects.filter(survey=self.survey, status=SurveyAttempt.IN_PROGRESS)
        self.assertEqual(active.count(), 1)
        self.assertEqual(active.get().user_id, account.id)
        self.assertIsNone(active.get().guest_principal_id)
        attempts = SurveyAttempt.objects.filter(survey=self.survey).order_by('started_at')
        self.assertEqual(attempts.filter(status=SurveyAttempt.STALE).count(), 1)
        self.assertEqual(attempts.filter(active_key__isnull=False).count(), 1)

    def test_claim_transfers_guest_only_attempt_for_account_resume(self):
        guest_id, token = self.new_guest()
        guest_client = Client()
        started = guest_client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/attempts/start/',
            data=json.dumps({'attemptId': str(uuid.uuid4())}), content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=token,
        )
        account = User.objects.create_user(
            username='guest-draft-claimant@example.test', email='guest-draft-claimant@example.test',
            password='Password123!', role='respondent',
        )
        account_client = Client()
        account_client.force_login(account)

        claim = account_client.post('/api/v1/guest/claim/', HTTP_X_FEEDORA_GUEST_TOKEN=token)
        attempt = SurveyAttempt.objects.get(attempt_id=started.json()['attemptId'])
        resumed = account_client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/attempts/start/',
            data=json.dumps({'attemptId': str(attempt.attempt_id)}), content_type='application/json',
        )

        self.assertEqual(claim.status_code, 200, claim.content)
        self.assertEqual(attempt.user_id, account.id)
        self.assertIsNone(attempt.guest_principal_id)
        self.assertEqual(resumed.status_code, 200, resumed.content)
        self.assertTrue(resumed.json()['resumed'])

    def test_authenticated_submission_auto_claim_uses_reservation_consolidation(self):
        guest_id, token = self.new_guest()
        guest_client = Client()
        guest_attempt = guest_client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/attempts/start/',
            data=json.dumps({'attemptId': str(uuid.uuid4())}), content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=token,
        )
        account = User.objects.create_user(
            username='auto-claim-owner@example.test', email='auto-claim-owner@example.test',
            password='Password123!', role='respondent',
        )
        account_client = Client()
        account_client.force_login(account)
        account_attempt = account_client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/attempts/start/',
            data=json.dumps({'attemptId': str(uuid.uuid4())}), content_type='application/json',
        )
        self.assertEqual(guest_attempt.status_code, 201, guest_attempt.content)
        self.assertEqual(account_attempt.status_code, 201, account_attempt.content)

        submitted = account_client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/submit/',
            data=json.dumps({
                'attemptId': guest_attempt.json()['attemptId'],
                'submissionId': str(uuid.uuid4()),
                'answers': {'Q1': 4},
            }),
            content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=token,
        )

        self.assertEqual(submitted.status_code, 200, submitted.content)
        attempts = SurveyAttempt.objects.filter(survey=self.survey)
        self.assertEqual(attempts.filter(status=SurveyAttempt.COMPLETED, user=account).count(), 1)
        self.assertEqual(attempts.filter(status=SurveyAttempt.STALE).count(), 1)
        self.assertEqual(attempts.filter(status=SurveyAttempt.IN_PROGRESS).count(), 0)
