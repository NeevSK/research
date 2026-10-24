import json
import uuid

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from accounts.models import Organization
from surveys.models import Question, Response, Survey


User = get_user_model()


class ScopedSubmissionBoundaryTests(TestCase):
    def setUp(self):
        self.client = Client()
        first_guest = self.client.post('/api/v1/guest/session/').json()
        second_guest = self.client.post('/api/v1/guest/session/').json()
        self.guest_a_id = first_guest['guestParticipantId']
        self.guest_b_id = second_guest['guestParticipantId']
        self.guest_tokens = {self.guest_a_id: first_guest['guestToken'], self.guest_b_id: second_guest['guestToken']}
        self.creator = User.objects.create_user(
            username='slice-owner@example.test',
            email='slice-owner@example.test',
            password='Password123!',
            role='creator',
        )
        self.organization = Organization.objects.create(
            created_by=self.creator,
            org_name='Slice Research Group',
            org_type='research',
        )
        self.survey = Survey.objects.create(
            created_by=self.creator,
            organization_scope=self.organization,
            scope_kind='organization',
            title='Scoped response survey',
            features={'points': {'enabled': True, 'values': {'likert': 4}}},
        )
        Question.objects.create(
            survey=self.survey,
            code='Q1',
            text='How was the experience?',
            question_type='likert',
            rating_max=5,
            order=1,
        )

    def submit(self, payload):
        guest_id = payload.get('guestParticipantId', self.guest_a_id)
        return self.client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/submit/',
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_tokens[guest_id],
        )

    def test_guest_replay_returns_same_receipt_and_does_not_duplicate_response(self):
        submission_id = str(uuid.uuid4())
        payload = {
            'submissionId': submission_id,
            'principalId': self.guest_a_id,
            'guestParticipantId': self.guest_a_id,
            'scopeId': f'org-{self.organization.id}',
            'answers': {'Q1': 4},
        }

        first = self.submit(payload)
        replay = self.submit(payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(Response.objects.filter(survey=self.survey).count(), 1)
        self.assertEqual(replay.json()['responseId'], first.json()['responseId'])
        self.assertEqual(replay.json()['pointsEarned'], first.json()['pointsEarned'])
        self.assertTrue(replay.json()['idempotent'])

    def test_identical_submission_ids_are_isolated_between_guest_principals(self):
        submission_id = str(uuid.uuid4())
        first = self.submit({
            'submissionId': submission_id,
            'principalId': self.guest_a_id,
            'guestParticipantId': self.guest_a_id,
            'scopeId': f'org-{self.organization.id}',
            'answers': {'Q1': 3},
        })
        second = self.submit({
            'submissionId': submission_id,
            'principalId': self.guest_b_id,
            'guestParticipantId': self.guest_b_id,
            'scopeId': f'org-{self.organization.id}',
            'answers': {'Q1': 5},
        })

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(first.json()['responseId'], second.json()['responseId'])
        self.assertEqual(Response.objects.filter(survey=self.survey).count(), 2)

    def test_request_cannot_claim_a_different_scope_or_principal(self):
        wrong_scope = self.submit({
            'submissionId': str(uuid.uuid4()),
            'principalId': self.guest_a_id,
            'guestParticipantId': self.guest_a_id,
            'scopeId': 'org-999999',
            'answers': {'Q1': 4},
        })
        wrong_principal = self.submit({
            'submissionId': str(uuid.uuid4()),
            'principalId': self.guest_b_id,
            'guestParticipantId': self.guest_a_id,
            'scopeId': f'org-{self.organization.id}',
            'answers': {'Q1': 4},
        })

        self.assertEqual(wrong_scope.status_code, 409)
        self.assertEqual(wrong_principal.status_code, 400)
        self.assertEqual(Response.objects.filter(survey=self.survey).count(), 0)

    def test_scope_points_are_independent_for_one_global_account(self):
        participant = User.objects.create_user(
            username='multi-scope@example.test',
            email='multi-scope@example.test',
            password='Password123!',
            role='respondent',
        )
        self.client.force_login(participant)
        first_meta = self.client.get(f'/api/v1/surveys/{self.survey.access_uuid}/meta/').json()
        first = self.submit({
            'submissionId': str(uuid.uuid4()),
            'principalId': f'user-{participant.pk}',
            'scopeId': f'org-{self.organization.id}',
            'policyVersion': first_meta['survey']['policyVersion'],
            'answers': {'Q1': 4},
        })

        second_creator = User.objects.create_user(
            username='second-owner@example.test',
            email='second-owner@example.test',
            password='Password123!',
            role='creator',
        )
        second_org = Organization.objects.create(created_by=second_creator, org_name='Second Lab', org_type='research')
        second_survey = Survey.objects.create(
            created_by=second_creator,
            organization_scope=second_org,
            scope_kind='organization',
            title='Second scoped survey',
            features={'points': {'enabled': True, 'values': {'likert': 9}}},
        )
        Question.objects.create(
            survey=second_survey,
            code='Q2',
            text='A separate organization question',
            question_type='likert',
            rating_max=5,
            order=1,
        )
        second_meta = self.client.get(f'/api/v1/surveys/{second_survey.access_uuid}/meta/').json()
        second = self.client.post(
            f'/api/v1/surveys/{second_survey.access_uuid}/submit/',
            data=json.dumps({
                'submissionId': str(uuid.uuid4()),
                'principalId': f'user-{participant.pk}',
                'scopeId': f'org-{second_org.id}',
                'policyVersion': second_meta['survey']['policyVersion'],
                'answers': {'Q2': 5},
            }),
            content_type='application/json',
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()['scopeTotalPoints'], first.json()['pointsEarned'])
        self.assertEqual(second.json()['scopeTotalPoints'], second.json()['pointsEarned'])
        self.assertEqual(first.json()['totalPoints'], first.json()['accountTotalPoints'])
        self.assertEqual(second.json()['totalPoints'], first.json()['pointsEarned'] + second.json()['pointsEarned'])
        self.assertNotEqual(first.json()['scopeId'], second.json()['scopeId'])

    def test_stale_policy_version_is_rejected_without_a_response(self):
        response = self.submit({
            'submissionId': str(uuid.uuid4()),
            'principalId': self.guest_a_id,
            'guestParticipantId': self.guest_a_id,
            'scopeId': f'org-{self.organization.id}',
            'policyVersion': 'stale-version',
            'answers': {'Q1': 4},
        })

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'policy_changed')
        self.assertEqual(Response.objects.filter(survey=self.survey).count(), 0)

    def test_reusing_a_submission_id_for_different_answers_is_rejected(self):
        submission_id = str(uuid.uuid4())
        first = self.submit({
            'submissionId': submission_id,
            'principalId': self.guest_a_id,
            'guestParticipantId': self.guest_a_id,
            'scopeId': f'org-{self.organization.id}',
            'answers': {'Q1': 2},
        })
        conflict = self.submit({
            'submissionId': submission_id,
            'principalId': self.guest_a_id,
            'guestParticipantId': self.guest_a_id,
            'scopeId': f'org-{self.organization.id}',
            'answers': {'Q1': 5},
        })

        self.assertEqual(first.status_code, 200)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()['code'], 'submission_id_conflict')
        self.assertEqual(Response.objects.filter(survey=self.survey).count(), 1)

    def test_accepted_response_keeps_its_original_scope_for_replay(self):
        submission_id = str(uuid.uuid4())
        original_scope = f'org-{self.organization.id}'
        first = self.submit({
            'submissionId': submission_id,
            'principalId': self.guest_a_id,
            'guestParticipantId': self.guest_a_id,
            'scopeId': original_scope,
            'answers': {'Q1': 4},
        })
        self.survey.features = {'is_global': True, 'points': {'enabled': True, 'values': {'likert': 4}}}
        self.survey.save(update_fields=['features'])

        replay = self.submit({
            'submissionId': submission_id,
            'principalId': self.guest_a_id,
            'guestParticipantId': self.guest_a_id,
            'scopeId': original_scope,
            'answers': {'Q1': 4},
        })

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()['scopeId'], original_scope)
        self.assertTrue(replay.json()['replayed'])

