import json
import uuid
import importlib
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.apps import apps
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone

from accounts.models import GuestPrincipal, Organization
from surveys.models import Question, Response, Survey, SurveyAttempt, SurveyPolicyRevision, SurveyPolicyScopeLock
from surveys.models import SurveyAccessCodeAttempt, SurveyAdmissionGrant
from feedora_project.api_views import _normalized_survey_policy


User = get_user_model()


class RestrictedSurveyAdmissionFixture:
    def setUp(self):
        self.client = Client()
        self.creator = User.objects.create_user(
            username='policy-owner@example.test',
            email='policy-owner@example.test',
            password='Password123!',
            role='creator',
        )
        organization = Organization.objects.create(created_by=self.creator, org_name='Restricted Lab')
        self.survey = Survey.objects.create(
            created_by=self.creator,
            organization_scope=organization,
            scope_kind='organization',
            title='Restricted participation survey',
            features={'accessCodeRequired': True},
        )
        Question.objects.create(
            survey=self.survey,
            code='Q1',
            text='A private question',
            question_type='likert',
            rating_max=5,
        )
        guest = self.client.post('/api/v1/guest/session/').json()
        self.guest_id = guest['guestParticipantId']
        self.guest_token = guest['guestToken']

class RestrictedSurveyAdmissionGateTests(RestrictedSurveyAdmissionFixture, TestCase):
    def test_public_catalog_never_discloses_the_access_code(self):
        catalog = self.client.get(
            '/api/v1/surveys/',
            HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token,
        ).json()
        item = next(item for item in catalog['surveys'] if item['uuid'] == str(self.survey.access_uuid))
        self.assertNotIn('accessCode', item)
        self.assertTrue(item['accessCodeRequired'])
        self.assertFalse(item['eligibility']['eligible'])
        self.assertEqual(item['eligibility']['reasons'][0]['code'], 'access_code_required')

    def test_uuid_metadata_and_submission_are_denied_without_access_grant(self):
        meta = self.client.get(f'/api/v1/surveys/{self.survey.access_uuid}/meta/')
        submitted = self.client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/submit/',
            data=json.dumps({
                'submissionId': str(uuid.uuid4()),
                'principalId': self.guest_id,
                'scopeId': f'org-{self.survey.organization_scope_id}',
                'answers': {'Q1': 5},
            }),
            content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token,
        )
        self.assertEqual(meta.status_code, 403)
        self.assertEqual(meta.json()['code'], 'access_code_required')
        self.assertEqual(submitted.status_code, 403)
        self.assertEqual(submitted.json()['code'], 'access_code_required')
        self.assertEqual(Response.objects.filter(survey=self.survey).count(), 0)

    def test_membership_requirement_is_enforced_for_guest_submission(self):
        self.survey.features = {'accessPolicy': {'organizationMembershipRequired': True}}
        self.survey.save(update_fields=['features'])
        meta = self.client.get(
            f'/api/v1/surveys/{self.survey.access_uuid}/meta/',
            HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token,
        )
        submitted = self.client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/submit/',
            data=json.dumps({
                'submissionId': str(uuid.uuid4()),
                'principalId': self.guest_id,
                'scopeId': f'org-{self.survey.organization_scope_id}',
                'answers': {'Q1': 5},
            }),
            content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token,
        )
        self.assertEqual(meta.status_code, 403)
        self.assertEqual(meta.json()['code'], 'organization_membership_required')
        self.assertNotIn('questions', meta.json())
        self.assertEqual(submitted.status_code, 403)
        self.assertEqual(submitted.json()['code'], 'organization_membership_required')
        self.assertEqual(Response.objects.filter(survey=self.survey).count(), 0)

    def test_access_code_metadata_does_not_issue_a_grant_when_another_rule_denies(self):
        self.survey.features = {
            'accessCodeRequired': True,
            'accessPolicy': {'organizationMembershipRequired': True},
        }
        self.survey.save(update_fields=['features'])

        response = self.client.get(
            f'/api/v1/surveys/code/{self.survey.access_code}/meta/',
            HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['code'], 'organization_membership_required')
        self.assertNotIn('questions', response.json())
        self.assertFalse(SurveyAdmissionGrant.objects.filter(survey=self.survey).exists())


class MetadataEligibilityTests(TestCase):
    def setUp(self):
        self.creator = User.objects.create_user(
            username='metadata-owner@example.test', email='metadata-owner@example.test',
            password='Password123!', role='creator',
        )
        self.organization = Organization.objects.create(created_by=self.creator, org_name='Metadata Eligibility Lab')
        self.survey = Survey.objects.create(
            created_by=self.creator, organization_scope=self.organization, scope_kind='organization',
            title='Eligibility protected questions',
        )
        self.question = Question.objects.create(
            survey=self.survey, code='Q1', text='Private question text', question_type='likert', rating_max=5,
        )
        guest = Client().post('/api/v1/guest/session/').json()
        self.guest_id = guest['guestParticipantId']
        self.guest_token = guest['guestToken']

    def meta(self, client=None):
        return (client or Client()).get(
            f'/api/v1/surveys/{self.survey.access_uuid}/meta/',
            HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token,
        )

    def test_account_requirement_hides_question_metadata_from_guest(self):
        self.survey.features = {'accessPolicy': {'requiresAccount': True}}
        self.survey.save(update_fields=['features'])

        response = self.meta()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['code'], 'account_required')
        self.assertNotIn('questions', response.json())

    def test_prerequisite_and_sequence_locks_hide_question_metadata(self):
        prerequisite = Survey.objects.create(
            created_by=self.creator, organization_scope=self.organization, scope_kind='organization',
            title='Required consent survey',
        )
        self.survey.features = {'prerequisites': {'completedFormIds': [str(prerequisite.access_uuid)]}}
        self.survey.save(update_fields=['features'])
        prerequisite_response = self.meta()
        self.assertEqual(prerequisite_response.status_code, 409)
        self.assertEqual(prerequisite_response.json()['code'], 'prerequisite_unmet')
        self.assertNotIn('questions', prerequisite_response.json())

        self.survey.features = {'sequence': {'groupId': 'metadata-wave', 'order': 2, 'enforce': True}}
        self.survey.save(update_fields=['features'])
        Survey.objects.create(
            created_by=self.creator, organization_scope=self.organization, scope_kind='organization',
            title='Earlier step',
            features={'sequence': {'groupId': 'metadata-wave', 'order': 1, 'enforce': True}},
        )
        sequence_response = self.meta()
        self.assertEqual(sequence_response.status_code, 409)
        self.assertEqual(sequence_response.json()['code'], 'sequence_locked')
        self.assertNotIn('questions', sequence_response.json())

    def test_draft_sequence_survey_does_not_lock_or_leak_metadata(self):
        self.survey.features = {'sequence': {'groupId': 'metadata-wave', 'order': 2, 'enforce': True}}
        self.survey.save(update_fields=['features'])
        draft = Survey.objects.create(
            created_by=self.creator, organization_scope=self.organization, scope_kind='organization',
            title='Private draft sequence title', is_draft=True,
            features={'sequence': {'groupId': 'metadata-wave', 'order': 1, 'enforce': True}},
        )

        response = self.meta()

        self.assertEqual(response.status_code, 200, response.content)
        features = response.json()['features']
        self.assertNotIn(str(draft.access_uuid), features['prerequisiteSurveyUuids'])
        self.assertNotIn('Private draft sequence title', features['prerequisiteSurveyTitles'])

    def test_malformed_legacy_prerequisite_fails_closed_without_server_error(self):
        self.survey.features = {'prerequisites': {'completedFormIds': ['not-a-survey-uuid']}}
        self.survey.save(update_fields=['features'])

        response = self.meta()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'unsupported_policy')
        self.assertNotIn('questions', response.json())

    def test_attempt_limit_and_cooldown_hide_question_metadata(self):
        Response.objects.create(
            survey=self.survey, guest_participant_id=self.guest_id,
            scope_kind='organization', scope_id=f'org-{self.organization.id}',
        )
        self.survey.features = {'accessPolicy': {'allowRedo': False, 'maxAttempts': 1}}
        self.survey.save(update_fields=['features'])
        limited = self.meta()
        self.assertEqual(limited.status_code, 409)
        self.assertEqual(limited.json()['code'], 'attempt_limit_reached')
        self.assertNotIn('questions', limited.json())

        self.survey.features = {'accessPolicy': {'allowRedo': True, 'maxAttempts': 3, 'cooldownMinutes': 60}}
        self.survey.save(update_fields=['features'])
        cooldown = self.meta()
        self.assertEqual(cooldown.status_code, 429)
        self.assertEqual(cooldown.json()['code'], 'cooldown_active')
        self.assertNotIn('questions', cooldown.json())

    def test_creator_can_load_survey_metadata_for_studio_even_if_participant_rules_lock_it(self):
        prerequisite = Survey.objects.create(
            created_by=self.creator, organization_scope=self.organization, scope_kind='organization',
            title='Creator prerequisite',
        )
        other_creator = User.objects.create_user(
            username='other-metadata-owner@example.test', email='other-metadata-owner@example.test',
            password='Password123!', role='creator',
        )
        other_org = Organization.objects.create(created_by=other_creator, org_name='Other Metadata Lab')
        foreign_prerequisite = Survey.objects.create(
            created_by=other_creator, organization_scope=other_org, scope_kind='organization',
            title='Foreign private survey title',
        )
        draft_sequence = Survey.objects.create(
            created_by=self.creator, organization_scope=self.organization, scope_kind='organization',
            title='Private draft sequence title', is_draft=True,
            features={'sequence': {'groupId': 'creator-wave', 'order': 1, 'enforce': True}},
        )
        self.survey.features = {
            'accessCodeRequired': True,
            'accessPolicy': {'requiresAccount': True, 'organizationMembershipRequired': True},
            'prerequisites': {'completedFormIds': [
                str(prerequisite.access_uuid), str(foreign_prerequisite.access_uuid), 'malformed-prerequisite-id',
            ]},
            'sequence': {'groupId': 'creator-wave', 'order': 2, 'enforce': True},
        }
        self.survey.save(update_fields=['features'])
        client = Client()
        client.force_login(self.creator)

        response = self.meta(client)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['questions'][0]['text'], 'Private question text')
        features = response.json()['features']
        self.assertEqual(features['prerequisiteSurveyUuids'], [str(prerequisite.access_uuid)])
        self.assertEqual(features['prerequisiteSurveyTitles'], ['Creator prerequisite'])
        self.assertNotIn(str(draft_sequence.access_uuid), features['prerequisiteSurveyUuids'])


class RestrictedSurveyAdmissionTests(RestrictedSurveyAdmissionFixture, TestCase):
    def test_verified_code_grant_unlocks_only_its_bound_survey_and_guest(self):
        verified = self.client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/access/verify/',
            data=json.dumps({'accessCode': self.survey.access_code}),
            content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token,
        )
        self.assertEqual(verified.status_code, 200)
        grant = verified.json()['accessGrant']

        meta = self.client.get(
            f'/api/v1/surveys/{self.survey.access_uuid}/meta/',
            HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token,
            HTTP_X_FEEDORA_ACCESS_GRANT=grant,
        )
        self.assertEqual(meta.status_code, 200)

        submitted = self.client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/submit/',
            data=json.dumps({
                'submissionId': str(uuid.uuid4()),
                'principalId': self.guest_id,
                'scopeId': f'org-{self.survey.organization_scope_id}',
                'accessGrant': grant,
                'policyVersion': meta.json()['survey']['policyVersion'],
                'answers': {'Q1': 5},
            }),
            content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token,
        )
        self.assertEqual(submitted.status_code, 200)

        other_guest = self.client.post('/api/v1/guest/session/').json()
        wrong_principal = self.client.get(
            f'/api/v1/surveys/{self.survey.access_uuid}/meta/',
            HTTP_X_FEEDORA_GUEST_TOKEN=other_guest['guestToken'],
            HTTP_X_FEEDORA_ACCESS_GRANT=grant,
        )
        self.assertEqual(wrong_principal.status_code, 403)
        self.assertEqual(wrong_principal.json()['code'], 'access_grant_invalid')

    def test_access_grant_is_repeatable_for_its_survey_but_expires_and_cannot_cross_scope(self):
        verified = self.client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/access/verify/',
            data=json.dumps({'accessCode': self.survey.access_code}),
            content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token,
        )
        grant = verified.json()['accessGrant']
        headers = {
            'HTTP_X_FEEDORA_GUEST_TOKEN': self.guest_token,
            'HTTP_X_FEEDORA_ACCESS_GRANT': grant,
        }

        # Admission grants are scoped reusable credentials, not one-time codes.
        self.assertEqual(self.client.get(f'/api/v1/surveys/{self.survey.access_uuid}/meta/', **headers).status_code, 200)
        self.assertEqual(self.client.get(f'/api/v1/surveys/{self.survey.access_uuid}/meta/', **headers).status_code, 200)

        other_organization = Organization.objects.create(created_by=self.creator, org_name='Other Restricted Lab')
        other_survey = Survey.objects.create(
            created_by=self.creator,
            organization_scope=other_organization,
            scope_kind='organization',
            title='Other restricted survey',
            features={'accessCodeRequired': True},
        )
        wrong_survey = self.client.get(f'/api/v1/surveys/{other_survey.access_uuid}/meta/', **headers)
        self.assertEqual(wrong_survey.status_code, 403)
        self.assertEqual(wrong_survey.json()['code'], 'access_grant_invalid')

        SurveyAdmissionGrant.objects.filter(survey=self.survey).update(expires_at=timezone.now() - timedelta(seconds=1))
        expired = self.client.get(f'/api/v1/surveys/{self.survey.access_uuid}/meta/', **headers)
        self.assertEqual(expired.status_code, 403)
        self.assertEqual(expired.json()['code'], 'access_grant_invalid')

    def test_access_code_verification_does_not_disclose_survey_existence(self):
        valid = self.client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/access/verify/',
            data=json.dumps({'accessCode': 'WRONG1'}),
            content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token,
        )
        missing = self.client.post(
            f'/api/v1/surveys/{uuid.uuid4()}/access/verify/',
            data=json.dumps({'accessCode': 'WRONG1'}),
            content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token,
        )
        self.assertEqual(valid.status_code, 403)
        self.assertEqual(missing.status_code, 403)
        self.assertEqual(valid.json()['code'], missing.json()['code'])

    def test_code_guesses_are_throttled_without_per_uuid_storage_amplification(self):
        for index in range(10):
            self.client.post(
                f'/api/v1/surveys/{uuid.uuid4()}/access/verify/',
                data=json.dumps({'accessCode': f'BAD{index:03d}'}),
                content_type='application/json',
                HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token,
            )
        blocked = self.client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/access/verify/',
            data=json.dumps({'accessCode': self.survey.access_code}),
            content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token,
        )
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.json()['code'], 'access_code_invalid')
        self.assertEqual(SurveyAccessCodeAttempt.objects.count(), 1)

    def test_guest_grant_remains_valid_after_guest_is_claimed_by_an_account(self):
        verified = self.client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/access/verify/',
            data=json.dumps({'accessCode': self.survey.access_code}),
            content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token,
        )
        grant = verified.json()['accessGrant']
        self.client.force_login(self.creator)
        claimed = self.client.post('/api/v1/guest/claim/', HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token)
        self.assertEqual(claimed.status_code, 200)
        meta = self.client.get(
            f'/api/v1/surveys/{self.survey.access_uuid}/meta/',
            HTTP_X_FEEDORA_ACCESS_GRANT=grant,
            HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token,
        )
        self.assertEqual(meta.status_code, 200)

    def test_code_link_metadata_issues_a_grant_for_legacy_code_links(self):
        meta = self.client.get(
            f'/api/v1/surveys/code/{self.survey.access_code}/meta/',
            HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token,
        )
        self.assertEqual(meta.status_code, 200)
        self.assertNotIn('accessCode', meta.json()['survey'])
        self.assertTrue(meta.json()['accessGrant'])
        submitted = self.client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/submit/',
            data=json.dumps({
                'submissionId': str(uuid.uuid4()),
                'principalId': self.guest_id,
                'scopeId': f'org-{self.survey.organization_scope_id}',
                'accessGrant': meta.json()['accessGrant'],
                'answers': {'Q1': 5},
            }),
            content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token,
        )
        self.assertEqual(submitted.status_code, 200)

    def test_accepted_submission_replay_survives_admission_expiry(self):
        verified = self.client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/access/verify/',
            data=json.dumps({'accessCode': self.survey.access_code}),
            content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token,
        )
        grant = verified.json()['accessGrant']
        submission = {
            'submissionId': str(uuid.uuid4()),
            'principalId': self.guest_id,
            'scopeId': f'org-{self.survey.organization_scope_id}',
            'accessGrant': grant,
            'answers': {'Q1': 5},
        }
        headers = {'HTTP_X_FEEDORA_GUEST_TOKEN': self.guest_token}
        first = self.client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/submit/', data=json.dumps(submission),
            content_type='application/json', **headers,
        )
        self.assertEqual(first.status_code, 200)
        SurveyAdmissionGrant.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        replay = self.client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/submit/', data=json.dumps(submission),
            content_type='application/json', **headers,
        )
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()['replayed'])
        self.assertEqual(Response.objects.filter(survey=self.survey).count(), 1)


class SurveyAttemptReservationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.creator = User.objects.create_user(
            username='attempt-owner@example.test', email='attempt-owner@example.test',
            password='Password123!', role='creator',
        )
        self.organization = Organization.objects.create(created_by=self.creator, org_name='Attempt Lab')
        self.survey = Survey.objects.create(
            created_by=self.creator,
            organization_scope=self.organization,
            scope_kind='organization',
            title='Attempt reservation survey',
            features={'accessPolicy': {'allowRedo': False, 'maxAttempts': 1}},
        )
        Question.objects.create(survey=self.survey, code='Q1', text='Question', question_type='likert', rating_max=5)
        guest = self.client.post('/api/v1/guest/session/').json()
        self.guest_id = guest['guestParticipantId']
        self.guest_token = guest['guestToken']

    def start(self, attempt_id):
        return self.client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/attempts/start/',
            data=json.dumps({'attemptId': attempt_id}),
            content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token,
        )

    def test_attempt_start_is_idempotent_and_reserves_the_only_attempt(self):
        attempt_id = str(uuid.uuid4())
        first = self.start(attempt_id)
        replay = self.start(attempt_id)
        second = self.start(str(uuid.uuid4()))
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(first.json()['attemptId'], replay.json()['attemptId'])
        self.assertEqual(replay.json()['resumed'], True)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()['attemptId'], first.json()['attemptId'])
        self.assertTrue(second.json()['resumed'])

    def test_attempt_start_requires_csrf_for_session_capable_requests(self):
        client = Client(enforce_csrf_checks=True)
        guest = client.post('/api/v1/guest/session/').json()
        meta = client.get(f'/api/v1/surveys/{self.survey.access_uuid}/meta/')
        self.assertEqual(meta.status_code, 200)
        payload = json.dumps({'attemptId': str(uuid.uuid4())})
        headers = {'HTTP_X_FEEDORA_GUEST_TOKEN': guest['guestToken']}
        denied = client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/attempts/start/',
            data=payload, content_type='application/json', **headers,
        )
        self.assertEqual(denied.status_code, 403)
        accepted = client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/attempts/start/',
            data=payload, content_type='application/json',
            HTTP_X_CSRFTOKEN=meta.json()['csrfToken'], **headers,
        )
        self.assertEqual(accepted.status_code, 201, accepted.content)

    def test_attempt_start_rejects_non_object_json_without_server_error(self):
        response = self.client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/attempts/start/',
            data='[]', content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'invalid_json')

    def test_submit_completes_the_reserved_attempt_and_binds_the_policy_revision(self):
        attempt_id = str(uuid.uuid4())
        started = self.start(attempt_id)
        self.assertEqual(started.status_code, 201, started.content)
        submitted = self.client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/submit/',
            data=json.dumps({
                'attemptId': attempt_id,
                'submissionId': str(uuid.uuid4()),
                'principalId': self.guest_id,
                'scopeId': f'org-{self.organization.id}',
                'policyVersion': started.json()['policyVersion'],
                'answers': {'Q1': 5},
            }),
            content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token,
        )
        self.assertEqual(submitted.status_code, 200, submitted.content)
        response = Response.objects.get(survey=self.survey)
        self.assertEqual(str(response.attempt.attempt_id), attempt_id)
        self.assertEqual(response.attempt.status, 'completed')
        self.assertIsNone(response.attempt.active_key)
        self.assertEqual(response.policy_version, response.attempt.policy_revision.version)

    def test_policy_edit_stales_an_open_attempt_without_discarding_its_draft_identity(self):
        attempt_id = str(uuid.uuid4())
        started = self.start(attempt_id)
        self.assertEqual(started.status_code, 201, started.content)
        self.survey.features = {'accessPolicy': {'allowRedo': True, 'maxAttempts': 2}}
        self.survey.save(update_fields=['features'])
        submitted = self.client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/submit/',
            data=json.dumps({
                'attemptId': attempt_id,
                'submissionId': str(uuid.uuid4()),
                'principalId': self.guest_id,
                'scopeId': f'org-{self.organization.id}',
                'policyVersion': started.json()['policyVersion'],
                'answers': {'Q1': 5},
            }),
            content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token,
        )
        self.assertEqual(submitted.status_code, 409)
        self.assertEqual(submitted.json()['code'], 'policy_changed')
        self.assertEqual(Response.objects.filter(survey=self.survey).count(), 0)
        resumed = self.start(attempt_id)
        self.assertEqual(resumed.status_code, 409, resumed.content)
        self.assertEqual(resumed.json()['code'], 'policy_changed')
        resumed = self.start(str(uuid.uuid4()))
        self.assertEqual(resumed.status_code, 201, resumed.content)
        self.assertNotEqual(resumed.json()['attemptId'], attempt_id)
        self.assertEqual(SurveyAttempt.objects.filter(survey=self.survey, status=SurveyAttempt.IN_PROGRESS).count(), 1)
        self.assertIsNone(SurveyAttempt.objects.get(attempt_id=attempt_id).active_key)

    def test_start_does_not_report_a_stale_same_policy_attempt_as_resumable(self):
        attempt_id = str(uuid.uuid4())
        started = self.start(attempt_id)
        self.assertEqual(started.status_code, 201, started.content)
        attempt = SurveyAttempt.objects.get(attempt_id=attempt_id)
        attempt.status = SurveyAttempt.STALE
        attempt.save(update_fields=['status', 'updated_at'])

        resumed = self.start(attempt_id)

        self.assertEqual(resumed.status_code, 409)
        self.assertEqual(resumed.json()['code'], 'attempt_not_active')


class CatalogSequenceProjectionTests(TestCase):
    def _catalog_survey_with_numeric_prerequisite(self, *, other_organization=False):
        creator = User.objects.create_user(
            username='numeric-prerequisite-owner@example.test',
            email='numeric-prerequisite-owner@example.test',
            password='Password123!', role='creator',
        )
        organization = Organization.objects.create(created_by=creator, org_name='Numeric Prerequisite Lab')
        prerequisite_org = organization
        if other_organization:
            prerequisite_org = Organization.objects.create(created_by=creator, org_name='Private Prerequisite Lab')
        prerequisite = Survey.objects.create(
            created_by=creator, organization_scope=prerequisite_org, scope_kind='organization',
            title='Required consent survey',
        )
        target = Survey.objects.create(
            created_by=creator, organization_scope=organization, scope_kind='organization',
            title='Follow-up survey', features={'prerequisites': {'completedFormIds': [str(prerequisite.id)]}},
        )
        guest_client = Client()
        guest = guest_client.post('/api/v1/guest/session/').json()
        return organization, prerequisite, target, guest_client, guest

    def test_catalog_resolves_numeric_legacy_prerequisite_against_uuid_history(self):
        organization, prerequisite, target, guest_client, guest = self._catalog_survey_with_numeric_prerequisite()
        Response.objects.create(
            survey=prerequisite, guest_participant_id=guest['guestParticipantId'],
            scope_kind='organization', scope_id=f'org-{organization.id}',
        )

        response = guest_client.get(
            f'/api/v1/surveys/?entity_id=org-{organization.id}',
            HTTP_X_FEEDORA_GUEST_TOKEN=guest['guestToken'],
        )

        self.assertEqual(response.status_code, 200)
        item = next(row for row in response.json()['surveys'] if row['uuid'] == str(target.access_uuid))
        self.assertTrue(item['eligibility']['eligible'], item['eligibility'])

    def test_catalog_names_same_scope_unmet_prerequisite(self):
        organization, prerequisite, target, guest_client, guest = self._catalog_survey_with_numeric_prerequisite()

        response = guest_client.get(
            f'/api/v1/surveys/?entity_id=org-{organization.id}',
            HTTP_X_FEEDORA_GUEST_TOKEN=guest['guestToken'],
        )

        item = next(row for row in response.json()['surveys'] if row['uuid'] == str(target.access_uuid))
        reason = item['eligibility']['reasons'][0]
        self.assertEqual(reason['code'], 'prerequisite_unmet')
        self.assertEqual(reason['surveyId'], str(prerequisite.access_uuid))
        self.assertEqual(reason['title'], prerequisite.title)

    def test_catalog_never_names_cross_scope_prerequisite(self):
        organization, prerequisite, target, guest_client, guest = self._catalog_survey_with_numeric_prerequisite(other_organization=True)

        response = guest_client.get(
            f'/api/v1/surveys/?entity_id=org-{organization.id}',
            HTTP_X_FEEDORA_GUEST_TOKEN=guest['guestToken'],
        )

        item = next(row for row in response.json()['surveys'] if row['uuid'] == str(target.access_uuid))
        self.assertFalse(item['eligibility']['eligible'])
        self.assertTrue(all('title' not in reason for reason in item['eligibility']['reasons']))

    def test_sequence_prerequisite_outside_first_fifty_still_locks_catalog_item(self):
        creator = User.objects.create_user(
            username='sequence-owner@example.test', email='sequence-owner@example.test',
            password='Password123!', role='creator',
        )
        organization = Organization.objects.create(created_by=creator, org_name='Sequence Lab')
        prerequisite = Survey.objects.create(
            created_by=creator, organization_scope=organization, scope_kind='organization',
            title='Earlier step', features={'sequence': {'groupId': 'study', 'order': 1, 'enforce': True}},
        )
        for index in range(50):
            Survey.objects.create(
                created_by=creator, organization_scope=organization, scope_kind='organization',
                title=f'Unrelated {index}',
            )
        target = Survey.objects.create(
            created_by=creator, organization_scope=organization, scope_kind='organization',
            title='Later step', features={'sequence': {'groupId': 'study', 'order': 2, 'enforce': True}},
        )
        guest = Client().post('/api/v1/guest/session/').json()
        response = Client().get(
            f'/api/v1/surveys/?entity_id=org-{organization.id}',
            HTTP_X_FEEDORA_GUEST_TOKEN=guest['guestToken'],
        )
        self.assertEqual(response.status_code, 200)
        catalog = response.json()['surveys']
        self.assertIn(str(target.access_uuid), {item['uuid'] for item in catalog})
        target_item = next(item for item in catalog if item['uuid'] == str(target.access_uuid))
        self.assertFalse(target_item['eligibility']['eligible'])
        self.assertEqual(target_item['eligibility']['reasons'][0]['code'], 'sequence_locked')
        self.assertEqual(target_item['eligibility']['reasons'][0]['requiredSurveyIds'], [str(prerequisite.access_uuid)])

    def test_batched_sequence_lookup_keeps_legacy_group_aliases(self):
        creator = User.objects.create_user(
            username='sequence-alias-owner@example.test', email='sequence-alias-owner@example.test',
            password='Password123!', role='creator',
        )
        organization = Organization.objects.create(created_by=creator, org_name='Sequence Alias Lab')
        prerequisite = Survey.objects.create(
            created_by=creator, organization_scope=organization, scope_kind='organization',
            title='Legacy earlier step', features={'sequence': {'group_id': 'legacy-study', 'order': 1, 'enforceSequence': True}},
        )
        target = Survey.objects.create(
            created_by=creator, organization_scope=organization, scope_kind='organization',
            title='Canonical later step', features={'sequence': {'groupId': 'legacy-study', 'order': 2, 'enforce': True}},
        )
        guest = Client().post('/api/v1/guest/session/').json()

        response = Client().get(
            f'/api/v1/surveys/?entity_id=org-{organization.id}',
            HTTP_X_FEEDORA_GUEST_TOKEN=guest['guestToken'],
        )

        self.assertEqual(response.status_code, 200)
        item = next(row for row in response.json()['surveys'] if row['uuid'] == str(target.access_uuid))
        self.assertEqual(item['eligibility']['reasons'][0]['code'], 'sequence_locked')
        self.assertEqual(item['eligibility']['reasons'][0]['requiredSurveyIds'], [str(prerequisite.access_uuid)])


class CatalogQueryBudgetTests(TestCase):
    def test_sequence_catalog_query_growth_is_bounded_from_one_to_fifty_surveys(self):
        creator = User.objects.create_user(
            username='catalog-budget-owner@example.test', email='catalog-budget-owner@example.test',
            password='Password123!', role='creator',
        )
        organization = Organization.objects.create(created_by=creator, org_name='Catalog Budget Lab')
        client = Client()
        guest = client.post('/api/v1/guest/session/').json()

        def create_survey(index):
            return Survey.objects.create(
                created_by=creator, organization_scope=organization, scope_kind='organization',
                title=f'Sequence {index}',
                features={'sequence': {'groupId': f'group-{index}', 'order': 1, 'enforce': True}},
            )

        create_survey(0)
        headers = {'HTTP_X_FEEDORA_GUEST_TOKEN': guest['guestToken']}
        with CaptureQueriesContext(connection) as one_query_count:
            one_response = client.get(f'/api/v1/surveys/?entity_id=org-{organization.id}', **headers)
        self.assertEqual(one_response.status_code, 200)

        for index in range(1, 50):
            create_survey(index)
        with CaptureQueriesContext(connection) as fifty_query_count:
            fifty_response = client.get(f'/api/v1/surveys/?entity_id=org-{organization.id}', **headers)
        self.assertEqual(fifty_response.status_code, 200)
        self.assertEqual(len(fifty_response.json()['surveys']), 50)

        self.assertLessEqual(len(one_query_count), 8, f'one-item catalog used {len(one_query_count)} queries')
        self.assertLessEqual(len(fifty_query_count), 8, f'fifty-item catalog used {len(fifty_query_count)} queries')


class MalformedLegacyPolicyApiTests(TestCase):
    def test_malformed_legacy_rules_are_locked_in_catalog_and_metadata(self):
        creator = User.objects.create_user(
            username='malformed-policy-owner@example.test', email='malformed-policy-owner@example.test',
            password='Password123!', role='creator',
        )
        organization = Organization.objects.create(created_by=creator, org_name='Malformed Policy Lab')
        surveys = [
            Survey.objects.create(
                created_by=creator, organization_scope=organization, scope_kind='organization',
                title='Invalid redo flag', features={'allowRedo': 'false'},
            ),
            Survey.objects.create(
                created_by=creator, organization_scope=organization, scope_kind='organization',
                title='Invalid reward strategy', features={'accessPolicy': {'rewardStrategy': []}},
            ),
            Survey.objects.create(
                created_by=creator, organization_scope=organization, scope_kind='organization',
                title='Invalid feature document', features=['unexpected', 'top-level', 'array'],
            ),
        ]
        for survey in surveys:
            Question.objects.create(
                survey=survey, code='Q1', text='Policy gate item', question_type='likert', rating_max=5,
            )
        guest = Client().post('/api/v1/guest/session/').json()
        client = Client()
        catalog = client.get(
            f'/api/v1/surveys/?entity_id=org-{organization.id}',
            HTTP_X_FEEDORA_GUEST_TOKEN=guest['guestToken'],
        )

        self.assertEqual(catalog.status_code, 200)
        by_uuid = {item['uuid']: item for item in catalog.json()['surveys']}
        for survey in surveys:
            with self.subTest(survey=survey.title):
                self.assertFalse(by_uuid[str(survey.access_uuid)]['eligibility']['eligible'])
                self.assertEqual(by_uuid[str(survey.access_uuid)]['eligibility']['reasons'][0]['code'], 'unsupported_policy')
                metadata = client.get(
                    f'/api/v1/surveys/{survey.access_uuid}/meta/',
                    HTTP_X_FEEDORA_GUEST_TOKEN=guest['guestToken'],
                )
                self.assertEqual(metadata.status_code, 409)
                self.assertEqual(metadata.json()['code'], 'unsupported_policy')
                submission = client.post(
                    f'/api/v1/surveys/{survey.access_uuid}/submit/',
                    data=json.dumps({'submissionId': str(uuid.uuid4()), 'answers': {'Q1': 3}}),
                    content_type='application/json',
                    HTTP_X_FEEDORA_GUEST_TOKEN=guest['guestToken'],
                )
                self.assertEqual(submission.status_code, 409)
                self.assertEqual(submission.json()['code'], 'unsupported_policy')
                self.assertFalse(Response.objects.filter(survey=survey).exists())


class LegacyRespondentPolicyEndpointTests(TestCase):
    def setUp(self):
        self.creator = User.objects.create_user(
            username='legacy-policy-owner@example.test', email='legacy-policy-owner@example.test',
            password='Password123!', role='creator',
        )
        self.organization = Organization.objects.create(created_by=self.creator, org_name='Legacy Route Lab')
        self.survey = Survey.objects.create(
            created_by=self.creator, organization_scope=self.organization, scope_kind='organization',
            title='Legacy HTML policy survey',
        )
        self.question = Question.objects.create(
            survey=self.survey, code='Q1', text='Private browser question', question_type='likert', rating_max=5,
        )
        self.client = Client()

    def test_legacy_meta_and_html_hide_questions_when_account_is_required(self):
        self.survey.features = {'accessPolicy': {'requiresAccount': True}}
        self.survey.save(update_fields=['features'])

        metadata = self.client.get(f'/surveys/respond/{self.survey.access_uuid}/meta/')
        page = self.client.get(f'/surveys/respond/{self.survey.access_uuid}/')

        self.assertEqual(metadata.status_code, 401)
        self.assertEqual(metadata.json()['code'], 'account_required')
        self.assertNotIn('questions', metadata.json())
        self.assertEqual(page.status_code, 401)
        self.assertNotIn('Private browser question', page.content.decode('utf-8'))

    def test_legacy_access_code_flow_issues_a_session_bound_grant(self):
        self.survey.features = {'accessCodeRequired': True}
        self.survey.save(update_fields=['features'])

        denied = self.client.get(f'/surveys/respond/{self.survey.access_uuid}/meta/')
        verified = self.client.post('/surveys/access/', {'code': self.survey.access_code})
        allowed = self.client.get(f'/surveys/respond/{self.survey.access_uuid}/meta/')

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()['code'], 'access_code_required')
        self.assertEqual(verified.status_code, 302)
        self.assertEqual(allowed.status_code, 200)
        self.assertNotIn('access_code', allowed.json()['survey'])
        self.assertEqual(len(allowed.json()['questions']), 1)

    def test_legacy_submit_uses_session_guest_and_enforces_attempt_limit(self):
        self.survey.features = {'accessPolicy': {'allowRedo': False, 'maxAttempts': 1}}
        self.survey.save(update_fields=['features'])
        page = self.client.get(f'/surveys/respond/{self.survey.access_uuid}/')
        self.assertEqual(page.status_code, 200)

        first = self.client.post(f'/surveys/respond/{self.survey.access_uuid}/submit/', {f'q_{self.question.id}': '4'})
        second = self.client.post(f'/surveys/respond/{self.survey.access_uuid}/submit/', {f'q_{self.question.id}': '5'})

        self.assertEqual(first.status_code, 200)
        self.assertContains(second, 'Response not submitted', status_code=409)
        responses = Response.objects.filter(survey=self.survey)
        self.assertEqual(responses.count(), 1)
        accepted = responses.get()
        self.assertTrue(accepted.guest_participant_id)
        self.assertEqual(accepted.scope_kind, 'organization')
        self.assertEqual(accepted.scope_id, f'org-{self.organization.id}')
        self.assertTrue(accepted.policy_version)

    def test_legacy_submit_fails_closed_for_malformed_top_level_feature_json(self):
        self.survey.features = ['unrecognized', 'legacy', 'shape']
        self.survey.save(update_fields=['features'])
        self.client.get(f'/surveys/respond/{self.survey.access_uuid}/')

        result = self.client.post(
            f'/surveys/respond/{self.survey.access_uuid}/submit/',
            {f'q_{self.question.id}': '4'},
        )

        self.assertContains(result, 'Response not submitted', status_code=409)
        self.assertFalse(Response.objects.filter(survey=self.survey).exists())

    def test_invalid_bearer_does_not_fall_back_to_anonymous_shared_identity(self):
        client = Client()
        headers = {'HTTP_X_FEEDORA_GUEST_TOKEN': 'invalid-guest-token'}

        metadata = client.get(f'/surveys/respond/{self.survey.access_uuid}/meta/', **headers)
        submitted = client.post(
            f'/surveys/respond/{self.survey.access_uuid}/submit/',
            {'submission_id': str(uuid.uuid4()), f'q_{self.question.id}': '4'},
            **headers,
        )

        self.assertEqual(metadata.status_code, 401)
        self.assertEqual(metadata.json()['code'], 'guest_session_required')
        self.assertEqual(submitted.status_code, 401)
        self.assertFalse(Response.objects.filter(survey=self.survey).exists())

    def test_legacy_submit_cannot_consume_a_reserved_api_attempt(self):
        guest = self.client.post('/api/v1/guest/session/').json()
        attempt_id = str(uuid.uuid4())
        start = self.client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/attempts/start/',
            data=json.dumps({'attemptId': attempt_id}),
            content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=guest['guestToken'],
        )
        submitted = self.client.post(
            f'/surveys/respond/{self.survey.access_uuid}/submit/',
            {
                'submission_id': str(uuid.uuid4()),
                f'q_{self.question.id}': '4',
            },
            HTTP_X_FEEDORA_GUEST_TOKEN=guest['guestToken'],
        )

        self.assertEqual(start.status_code, 201)
        self.assertContains(submitted, 'saved attempt in progress', status_code=409)
        self.assertFalse(Response.objects.filter(survey=self.survey).exists())
        self.assertEqual(SurveyAttempt.objects.get(attempt_id=attempt_id).status, SurveyAttempt.IN_PROGRESS)

    def test_legacy_submission_receipt_is_idempotent_and_rejects_changed_answers(self):
        self.client.get(f'/surveys/respond/{self.survey.access_uuid}/')
        submission_id = str(uuid.uuid4())
        payload = {'submission_id': submission_id, f'q_{self.question.id}': '4'}

        first = self.client.post(f'/surveys/respond/{self.survey.access_uuid}/submit/', payload)
        replay = self.client.post(f'/surveys/respond/{self.survey.access_uuid}/submit/', payload)
        conflict = self.client.post(
            f'/surveys/respond/{self.survey.access_uuid}/submit/',
            {'submission_id': submission_id, f'q_{self.question.id}': '5'},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertContains(conflict, 'Response not submitted', status_code=409)
        self.assertEqual(Response.objects.filter(survey=self.survey).count(), 1)

    def test_legacy_submit_rejects_a_policy_revision_changed_since_form_opened(self):
        from feedora_project.api_views import _survey_policy_version

        opened = _survey_policy_version(self.survey)
        self.survey.features = {'accessPolicy': {'allowRedo': True, 'maxAttempts': 2}}
        self.survey.save(update_fields=['features'])

        result = self.client.post(
            f'/surveys/respond/{self.survey.access_uuid}/submit/',
            {
                'submission_id': str(uuid.uuid4()),
                'policy_version': opened,
                f'q_{self.question.id}': '4',
            },
        )

        self.assertContains(result, 'rules changed', status_code=409)
        self.assertFalse(Response.objects.filter(survey=self.survey).exists())

    def test_legacy_repeat_rewards_follow_saved_strategy_and_scoped_history(self):
        second_question = Question.objects.create(
            survey=self.survey, code='Q2', text='Another browser question', question_type='likert', rating_max=5,
        )
        self.survey.features = {
            'accessPolicy': {'allowRedo': True, 'maxAttempts': 3, 'rewardStrategy': 'score_delta'},
        }
        self.survey.save(update_fields=['features'])
        self.client.get(f'/surveys/respond/{self.survey.access_uuid}/')

        first = self.client.post(
            f'/surveys/respond/{self.survey.access_uuid}/submit/',
            {'submission_id': str(uuid.uuid4()), f'q_{self.question.id}': '4'},
        )
        second = self.client.post(
            f'/surveys/respond/{self.survey.access_uuid}/submit/',
            {'submission_id': str(uuid.uuid4()), f'q_{self.question.id}': '4', f'q_{second_question.id}': '3'},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        points = list(Response.objects.filter(survey=self.survey).order_by('id').values_list('points_earned', flat=True))
        self.assertEqual(points, [10, 10])

        self.survey.features['accessPolicy']['rewardStrategy'] = 'first_attempt_only'
        self.survey.features['accessPolicy']['maxAttempts'] = 4
        self.survey.save(update_fields=['features'])
        third = self.client.post(
            f'/surveys/respond/{self.survey.access_uuid}/submit/',
            {'submission_id': str(uuid.uuid4()), f'q_{second_question.id}': '2'},
        )
        self.assertEqual(third.status_code, 200)
        self.assertEqual(Response.objects.filter(survey=self.survey).order_by('id').last().points_earned, 0)

    def test_unrestricted_repeat_reward_awards_full_points_on_each_allowed_attempt(self):
        self.survey.features = {
            'accessPolicy': {'allowRedo': True, 'maxAttempts': 2, 'rewardStrategy': 'unrestricted'},
        }
        self.survey.save(update_fields=['features'])
        self.client.get(f'/surveys/respond/{self.survey.access_uuid}/')

        first = self.client.post(
            f'/surveys/respond/{self.survey.access_uuid}/submit/',
            {'submission_id': str(uuid.uuid4()), f'q_{self.question.id}': '4'},
        )
        second = self.client.post(
            f'/surveys/respond/{self.survey.access_uuid}/submit/',
            {'submission_id': str(uuid.uuid4()), f'q_{self.question.id}': '4'},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            list(Response.objects.filter(survey=self.survey).order_by('id').values_list('points_earned', flat=True)),
            [10, 10],
        )


class PolicyRevisionMigrationTests(TestCase):
    def test_legacy_policy_backfill_preserves_feature_snapshot(self):
        creator = User.objects.create_user(
            username='migration-owner@example.test', email='migration-owner@example.test',
            password='Password123!', role='creator',
        )
        organization = Organization.objects.create(created_by=creator, org_name='Migration Lab')
        features = {'accessPolicy': {'allowRedo': False, 'maxAttempts': 1}, 'points': {'enabled': True}}
        survey = Survey.objects.create(
            created_by=creator, organization_scope=organization, scope_kind='organization',
            title='Legacy snapshot', features=features,
        )
        migration = importlib.import_module('surveys.migrations.0018_policy_revisions_and_attempts')
        migration.seed_legacy_policy_snapshots(apps, connection.schema_editor())
        revision = SurveyPolicyRevision.objects.get(survey=survey)
        self.assertEqual(revision.document, {'kind': 'legacy-baseline', 'features': features})

    def test_active_attempt_key_backfill_builds_portable_reservation_key(self):
        creator = User.objects.create_user(
            username='attempt-migration-owner@example.test', email='attempt-migration-owner@example.test',
            password='Password123!', role='creator',
        )
        organization = Organization.objects.create(created_by=creator, org_name='Attempt Migration Lab')
        survey = Survey.objects.create(
            created_by=creator, organization_scope=organization, scope_kind='organization',
            title='Active attempt backfill',
        )
        revision = SurveyPolicyRevision.objects.create(survey=survey, version='legacy-test', document={})
        guest = GuestPrincipal.objects.create(token_hash='a' * 64)
        attempt = SurveyAttempt.objects.create(
            attempt_id=uuid.uuid4(), survey=survey, policy_revision=revision,
            guest_principal=guest, scope_kind='organization', scope_id=f'org-{organization.id}',
        )
        SurveyAttempt.objects.filter(pk=attempt.pk).update(active_key=None)
        migration = importlib.import_module('surveys.migrations.0019_remove_surveyattempt_one_active_user_survey_attempt_and_more')
        migration.backfill_active_attempt_keys(apps, connection.schema_editor())
        attempt.refresh_from_db()
        self.assertEqual(attempt.active_key, f'{survey.id}:guest:{guest.id}')


class ResponseSubmissionKeyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='submission-key-owner@example.test', email='submission-key-owner@example.test',
            password='Password123!', role='respondent',
        )
        creator = User.objects.create_user(
            username='submission-key-creator@example.test', email='submission-key-creator@example.test',
            password='Password123!', role='creator',
        )
        organization = Organization.objects.create(created_by=creator, org_name='Submission Key Lab')
        self.survey = Survey.objects.create(
            created_by=creator, organization_scope=organization, scope_kind='organization',
            title='Submission key survey',
        )

    def test_database_unique_key_rejects_duplicate_principal_submission(self):
        receipt = Response.objects.create(
            survey=self.survey, user=self.user, submission_id='stable-submission-id',
        )
        duplicate = Response(
            survey=self.survey, user=self.user, submission_id='stable-submission-id',
        )
        self.assertEqual(len(receipt.submission_key), 64)
        with self.assertRaises(IntegrityError), transaction.atomic():
            duplicate.save()

    def test_migration_backfills_keys_and_preserves_legacy_duplicate_rows(self):
        migration = importlib.import_module('surveys.migrations.0020_portable_response_submission_key')
        rows = [
            Response(survey=self.survey, user=self.user, submission_id='legacy-duplicate'),
            Response(survey=self.survey, user=self.user, submission_id='legacy-duplicate'),
            Response(survey=self.survey, user=self.user, submission_id='legacy-unique'),
        ]
        Response.objects.bulk_create(rows)

        migration.backfill_response_submission_keys(apps, connection.schema_editor())

        legacy_rows = list(Response.objects.filter(submission_id='legacy-duplicate'))
        unique_row = Response.objects.get(submission_id='legacy-unique')
        self.assertEqual(len(legacy_rows), 2)
        self.assertTrue(all(row.submission_key is None for row in legacy_rows))
        self.assertEqual(len(unique_row.submission_key), 64)


class UnsupportedScorePolicyTests(TestCase):
    def test_creator_survey_list_projects_authoritative_scope(self):
        creator = User.objects.create_user(
            username='scope-list-owner@example.test', email='scope-list-owner@example.test',
            password='Password123!', role='creator',
        )
        organization = Organization.objects.create(created_by=creator, org_name='Scope Listing Lab')
        survey = Survey.objects.create(
            created_by=creator, organization_scope=organization, scope_kind='organization', title='Scoped survey',
        )
        client = Client()
        client.force_login(creator)

        response = client.get('/api/v1/surveys/mine/')

        self.assertEqual(response.status_code, 200)
        item = next(item for item in response.json()['surveys'] if item['uuid'] == str(survey.access_uuid))
        self.assertEqual(item['scopeId'], f'org-{organization.id}')
        self.assertEqual(item['scopeKind'], 'organization')

    def test_survey_creation_rejects_malformed_prerequisite_without_write(self):
        creator = User.objects.create_user(
            username='malformed-create-owner@example.test', email='malformed-create-owner@example.test',
            password='Password123!', role='creator',
        )
        organization = Organization.objects.create(created_by=creator, org_name='Malformed Create Lab')
        client = Client()
        client.force_login(creator)
        before = Survey.objects.count()

        response = client.post(
            '/api/v1/surveys/create/',
            data=json.dumps({
                'title': 'Must reject malformed prerequisite',
                'organizationId': organization.id,
                'features': {'prerequisites': {'completedFormIds': ['not-a-survey-uuid']}},
                'questions': [],
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['field'], 'prerequisites')
        self.assertEqual(Survey.objects.count(), before)

    def test_legacy_repeat_flag_is_projected_from_effective_attempt_limit(self):
        creator = User.objects.create_user(
            username='legacy-repeat-owner@example.test', email='legacy-repeat-owner@example.test',
            password='Password123!', role='creator',
        )
        organization = Organization.objects.create(created_by=creator, org_name='Legacy Repeat Lab')
        survey = Survey.objects.create(
            created_by=creator, organization_scope=organization, scope_kind='organization',
            title='Contradictory legacy form',
            features={'accessPolicy': {'allowRedo': False, 'maxAttempts': 3}},
        )

        effective = _normalized_survey_policy(survey)['access']

        self.assertEqual(effective['maxAttempts'], 3)
        self.assertTrue(effective['allowRedo'])

    def test_creator_policy_update_rejects_contradictory_repeat_limit_without_write(self):
        creator = User.objects.create_user(
            username='contradictory-policy-editor@example.test',
            email='contradictory-policy-editor@example.test',
            password='Password123!', role='creator',
        )
        organization = Organization.objects.create(created_by=creator, org_name='Contradictory Policy Lab')
        survey = Survey.objects.create(
            created_by=creator, organization_scope=organization, scope_kind='organization',
            title='Repeat limit policy', features={'points': {'enabled': True}},
        )
        client = Client()
        client.force_login(creator)

        for allow_redo, max_attempts in ((False, 3), (True, 1)):
            result = client.post(
                f'/api/v1/surveys/{survey.access_uuid}/policy/',
                data=json.dumps({'features': {'accessPolicy': {'allowRedo': allow_redo, 'maxAttempts': max_attempts}}}),
                content_type='application/json',
            )

            self.assertEqual(result.status_code, 400)
            self.assertEqual(result.json()['field'], 'accessPolicy.allowRedo')
        survey.refresh_from_db()
        self.assertEqual(survey.features, {'points': {'enabled': True}})

    def test_creator_policy_update_derives_repeat_flag_from_maximum(self):
        creator = User.objects.create_user(
            username='derived-policy-editor@example.test',
            email='derived-policy-editor@example.test', password='Password123!', role='creator',
        )
        organization = Organization.objects.create(created_by=creator, org_name='Derived Policy Lab')
        survey = Survey.objects.create(
            created_by=creator, organization_scope=organization, scope_kind='organization', title='Derived attempt rules',
            features={'access_policy': {'allow_redo': False, 'max_attempts': 1}},
        )
        client = Client()
        client.force_login(creator)

        result = client.post(
            f'/api/v1/surveys/{survey.access_uuid}/policy/',
            data=json.dumps({'features': {'accessPolicy': {'maxAttempts': 3}}}),
            content_type='application/json',
        )

        self.assertEqual(result.status_code, 200, result.content)
        self.assertTrue(result.json()['features']['accessPolicy']['allowRedo'])
        self.assertEqual(result.json()['features']['accessPolicy']['maxAttempts'], 3)
        survey.refresh_from_db()
        self.assertNotIn('access_policy', survey.features)

    def test_creator_policy_update_rejects_malformed_prerequisite_without_write(self):
        creator = User.objects.create_user(
            username='malformed-prerequisite-editor@example.test',
            email='malformed-prerequisite-editor@example.test', password='Password123!', role='creator',
        )
        organization = Organization.objects.create(created_by=creator, org_name='Malformed Prerequisite Lab')
        survey = Survey.objects.create(
            created_by=creator, organization_scope=organization, scope_kind='organization',
            title='Malformed prerequisite form', features={'points': {'enabled': True}},
        )
        client = Client()
        client.force_login(creator)

        response = client.post(
            f'/api/v1/surveys/{survey.access_uuid}/policy/',
            data=json.dumps({'features': {'prerequisites': {'completedFormIds': ['not-a-survey-uuid']}}}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        survey.refresh_from_db()
        self.assertEqual(survey.features, {'points': {'enabled': True}})

    def test_minimum_score_policy_is_not_advertised_as_enforced(self):
        creator = User.objects.create_user(
            username='score-owner@example.test',
            email='score-owner@example.test',
            password='Password123!',
            role='creator',
        )
        organization = Organization.objects.create(created_by=creator, org_name='Score Gate Lab')
        survey = Survey.objects.create(
            created_by=creator,
            organization_scope=organization,
            scope_kind='organization',
            title='Score gated survey',
            features={'minScorePercent': 75},
        )
        client = Client()
        result = client.get(f'/api/v1/surveys/{survey.access_uuid}/meta/')
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()['code'], 'unsupported_policy')

    def test_creator_can_save_supported_membership_and_code_rules(self):
        creator = User.objects.create_user(
            username='policy-editor@example.test', email='policy-editor@example.test',
            password='Password123!', role='creator',
        )
        organization = Organization.objects.create(created_by=creator, org_name='Policy Editor Lab')
        survey = Survey.objects.create(created_by=creator, organization_scope=organization, scope_kind='organization', title='Configurable survey')
        client = Client()
        client.force_login(creator)
        result = client.post(
            f'/api/v1/surveys/{survey.access_uuid}/policy/',
            data=json.dumps({'features': {
                'accessPolicy': {
                    'requiresAccount': False,
                    'organizationMembershipRequired': True,
                    'allowRedo': False,
                    'maxAttempts': 1,
                    'cooldownMinutes': 0,
                },
                'accessCodeRequired': True,
                'prerequisites': [],
            }}),
            content_type='application/json',
        )
        self.assertEqual(result.status_code, 200, result.content)
        survey.refresh_from_db()
        self.assertTrue(survey.features['accessCodeRequired'])
        self.assertTrue(survey.features['accessPolicy']['organizationMembershipRequired'])

    def test_creator_cannot_save_unsupported_score_gate(self):
        creator = User.objects.create_user(
            username='score-editor@example.test', email='score-editor@example.test',
            password='Password123!', role='creator',
        )
        organization = Organization.objects.create(created_by=creator, org_name='Score Editor Lab')
        survey = Survey.objects.create(created_by=creator, organization_scope=organization, scope_kind='organization', title='No score rule')
        client = Client()
        client.force_login(creator)
        result = client.post(
            f'/api/v1/surveys/{survey.access_uuid}/policy/',
            data=json.dumps({'features': {'prerequisites': {'minScorePercent': 75}}}),
            content_type='application/json',
        )
        self.assertEqual(result.status_code, 400)
        self.assertEqual(result.json()['code'], 'unsupported_policy')

    def test_creator_cannot_save_silently_ignored_access_or_sequence_rules(self):
        creator = User.objects.create_user(
            username='strict-policy-editor@example.test', email='strict-policy-editor@example.test',
            password='Password123!', role='creator',
        )
        organization = Organization.objects.create(created_by=creator, org_name='Strict Policy Lab')
        survey = Survey.objects.create(
            created_by=creator, organization_scope=organization, scope_kind='organization',
            title='Strict policy survey', features={'points': {'enabled': True}},
        )
        client = Client()
        client.force_login(creator)

        unknown_access = client.post(
            f'/api/v1/surveys/{survey.access_uuid}/policy/',
            data=json.dumps({'features': {'accessPolicy': {'isAdminRequired': True}}}),
            content_type='application/json',
        )
        unknown_sequence = client.post(
            f'/api/v1/surveys/{survey.access_uuid}/policy/',
            data=json.dumps({'features': {'sequence': {'groupId': 'study', 'order': 1, 'enforce': True, 'window': 5}}}),
            content_type='application/json',
        )

        self.assertEqual(unknown_access.status_code, 400)
        self.assertEqual(unknown_access.json()['field'], 'accessPolicy.isAdminRequired')
        self.assertEqual(unknown_sequence.status_code, 400)
        self.assertEqual(unknown_sequence.json()['field'], 'sequence.window')
        survey.refresh_from_db()
        self.assertEqual(survey.features, {'points': {'enabled': True}})

    def test_creator_cannot_save_fractional_or_negative_prerequisite_wait(self):
        creator = User.objects.create_user(
            username='delay-policy-editor@example.test', email='delay-policy-editor@example.test',
            password='Password123!', role='creator',
        )
        organization = Organization.objects.create(created_by=creator, org_name='Delay Policy Lab')
        survey = Survey.objects.create(
            created_by=creator, organization_scope=organization, scope_kind='organization',
            title='Delay policy survey',
        )
        client = Client()
        client.force_login(creator)

        for delay in (12.5, -1):
            result = client.post(
                f'/api/v1/surveys/{survey.access_uuid}/policy/',
                data=json.dumps({'features': {'prerequisites': {'completedFormIds': [], 'unlockDelayMs': delay}}}),
                content_type='application/json',
            )
            self.assertEqual(result.status_code, 400)
            self.assertEqual(result.json()['field'], 'prerequisites.unlockDelayMs')
        survey.refresh_from_db()
        self.assertEqual(survey.features, {})


class LegacyCreatorFeaturePolicyBoundaryTests(TestCase):
    def setUp(self):
        self.creator = User.objects.create_user(
            username='legacy-feature-editor@example.test',
            email='legacy-feature-editor@example.test',
            password='Password123!',
            role='creator',
        )
        self.organization = Organization.objects.create(created_by=self.creator, org_name='Legacy Feature Lab')
        self.survey = Survey.objects.create(
            created_by=self.creator,
            organization_scope=self.organization,
            scope_kind='organization',
            title='Legacy feature policy survey',
            features={
                'accessPolicy': {'requiresAccount': True, 'maxAttempts': 2, 'allowRedo': True},
                'prerequisites': [],
                'sequence': {'groupId': 'study-wave', 'order': 1, 'enforce': True},
                'points': {'enabled': True},
            },
        )
        self.client = Client()
        self.client.force_login(self.creator)
        self.url = f'/surveys/{self.survey.access_uuid}/features/'

    def test_legacy_feature_editor_cannot_change_policy(self):
        response = self.client.post(
            self.url,
            data=json.dumps({'features': {
                'accessPolicy': {'requiresAccount': False, 'maxAttempts': 5, 'allowRedo': True},
                'points': {'enabled': True, 'values': {'likert': 20}},
            }}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'policy_editor_required')
        self.survey.refresh_from_db()
        self.assertEqual(self.survey.features['accessPolicy']['requiresAccount'], True)
        self.assertEqual(self.survey.features['accessPolicy']['maxAttempts'], 2)
        self.assertEqual(self.survey.features['points'], {'enabled': True})

    def test_legacy_feature_save_preserves_omitted_policy_fields(self):
        response = self.client.post(
            self.url,
            data=json.dumps({'features': {
                'points': {'enabled': True, 'values': {'likert': 20}},
                'collection': {'email': True, 'accept': True},
            }}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.survey.refresh_from_db()
        self.assertTrue(self.survey.features['accessPolicy']['requiresAccount'])
        self.assertEqual(self.survey.features['accessPolicy']['maxAttempts'], 2)
        self.assertEqual(self.survey.features['sequence'], {'groupId': 'study-wave', 'order': 1, 'enforce': True})
        self.assertTrue(self.survey.features['collection']['email'])
        self.assertEqual(SurveyPolicyRevision.objects.filter(survey=self.survey).count(), 1)

    def test_legacy_feature_editor_rejects_non_object_without_writes(self):
        response = self.client.post(
            self.url,
            data=json.dumps({'features': ['unexpected', 'shape']}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.survey.refresh_from_db()
        self.assertTrue(self.survey.features['accessPolicy']['requiresAccount'])

    def test_legacy_feature_save_cannot_clear_malformed_policy_by_replacing_document(self):
        self.survey.features = ['legacy', 'malformed']
        self.survey.save(update_fields=['features'])

        response = self.client.post(
            self.url,
            data=json.dumps({'features': {'points': {'enabled': True}}}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'policy_editor_required')
        self.survey.refresh_from_db()
        self.assertEqual(self.survey.features, ['legacy', 'malformed'])


class CreatorPolicyTransactionBoundaryTests(TransactionTestCase):
    def test_policy_validation_and_revision_write_share_transaction(self):
        creator = User.objects.create_user(
            username='transaction-policy-editor@example.test',
            email='transaction-policy-editor@example.test',
            password='Password123!',
            role='creator',
        )
        organization = Organization.objects.create(created_by=creator, org_name='Policy Transaction Lab')
        survey = Survey.objects.create(
            created_by=creator,
            organization_scope=organization,
            scope_kind='organization',
            title='Serialized policy update',
        )
        client = Client()
        client.force_login(creator)

        def inspect_transaction(*_args, **_kwargs):
            self.assertTrue(connection.in_atomic_block)
            return False

        with patch('feedora_project.api_views._sequence_position_conflicts', side_effect=inspect_transaction):
            response = client.post(
                f'/api/v1/surveys/{survey.access_uuid}/policy/',
                data=json.dumps({'features': {'sequence': {'groupId': 'transaction-wave', 'order': 1, 'enforce': True}}}),
                content_type='application/json',
            )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(SurveyPolicyRevision.objects.filter(survey=survey).count(), 1)
        self.assertFalse(SurveyPolicyScopeLock.objects.filter(scope_kind='organization', scope_id=f'org-{organization.id}').exists())

    def test_scope_lock_uses_existing_organization_and_migrated_platform_rows(self):
        from feedora_project.api_views import _lock_survey_policy_scope

        creator = User.objects.create_user(
            username='scope-lock-owner@example.test',
            email='scope-lock-owner@example.test',
            password='Password123!',
            role='creator',
        )
        organization = Organization.objects.create(created_by=creator, org_name='Existing Organization Row')

        with transaction.atomic():
            locked_org = _lock_survey_policy_scope('organization', f'org-{organization.pk}')
            self.assertEqual(locked_org.pk, organization.pk)
        with transaction.atomic():
            platform_lock = _lock_survey_policy_scope('platform', 'platform')
            self.assertEqual(platform_lock.scope_kind, 'platform')
            self.assertEqual(platform_lock.scope_id, 'platform')

        self.assertFalse(SurveyPolicyScopeLock.objects.filter(scope_kind='organization').exists())

    def test_survey_creation_uses_scope_lock_for_unique_sequence_slots(self):
        creator = User.objects.create_user(
            username='sequence-slot-creator@example.test',
            email='sequence-slot-creator@example.test',
            password='Password123!',
            role='creator',
        )
        organization = Organization.objects.create(created_by=creator, org_name='Sequence Slot Lab')
        client = Client()
        client.force_login(creator)
        url = '/api/v1/surveys/create/'
        payload = {
            'title': 'First sequence step',
            'features': {'sequence': {'groupId': 'locked-wave', 'order': 1, 'enforce': True}},
            'questions': [],
        }

        first = client.post(url, data=json.dumps(payload), content_type='application/json')
        duplicate = client.post(
            url,
            data=json.dumps({**payload, 'title': 'Duplicate sequence step'}),
            content_type='application/json',
        )

        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(duplicate.json()['field'], 'sequence.order')
        self.assertEqual(Survey.objects.filter(title__in=['First sequence step', 'Duplicate sequence step']).count(), 1)
        self.assertFalse(SurveyPolicyScopeLock.objects.filter(scope_kind='organization', scope_id=f'org-{organization.id}').exists())
        created = Survey.objects.get(title='First sequence step')
        self.assertEqual(SurveyPolicyRevision.objects.filter(survey=created).count(), 1)

