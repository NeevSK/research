"""
Adversarial & Hostile Concurrency Test Suite for Feedora REST API.
Verifies runtime survivability under:
  1. Threaded concurrent survey submissions.
  2. Schema drift & orphaned question inputs.
  3. Oversized and unbounded text inputs.
"""
import concurrent.futures
import json
import uuid
from unittest.mock import patch
from django.test import TransactionTestCase
from django.db import OperationalError
from django.contrib.auth import get_user_model
from surveys.models import Survey, Question, MCQOption, Response, Answer, SurveyAttempt
from accounts.models import Organization

User = get_user_model()


class HostileConcurrencyTests(TransactionTestCase):
    def setUp(self):
        from django.test import Client
        session_response = Client().post('/api/v1/guest/session/').json()
        self.guest_id = session_response['guestParticipantId']
        self.guest_token = session_response['guestToken']
        self.creator = User.objects.create_user(
            username='lab_creator',
            email='lab_creator@feedora.org',
            password='Password123!',
            role='creator',
            is_active=True,
        )
        self.organization = Organization.objects.create(created_by=self.creator, org_name='Concurrency Lab')
        self.survey = Survey.objects.create(
            created_by=self.creator,
            organization_scope=self.organization,
            scope_kind='organization',
            title='Concurrency Stress Survey',
            description='Test survey under adversarial multi-threaded load',
            is_active=True,
            features={'allow_multiple_submissions': True},
        )
        self.q_likert = Question.objects.create(
            survey=self.survey,
            code='STRESS_L1',
            text='Stress test Likert item',
            question_type='likert',
            order=1,
        )
        self.q_open = Question.objects.create(
            survey=self.survey,
            code='STRESS_O1',
            text='Stress test open text item',
            question_type='open',
            order=2,
        )
        self.q_mcq = Question.objects.create(
            survey=self.survey,
            code='STRESS_M1',
            text='Stress test choice item',
            question_type='mcq',
            order=3,
        )
        self.opt1 = MCQOption.objects.create(question=self.q_mcq, text='Alpha', order=1)
        self.opt2 = MCQOption.objects.create(question=self.q_mcq, text='Beta', order=2)

    def test_concurrent_submissions_do_not_deadlock_sqlite(self):
        """Simulate concurrent threads submitting answers to the same survey simultaneously."""
        num_workers = 8
        url = f'/api/v1/surveys/{self.survey.access_uuid}/submit/'

        def submit_worker(worker_id):
            from django.test import Client
            client = Client()
            guest_session = client.post('/api/v1/guest/session/').json()
            payload = {
                'answers': {
                    'STRESS_L1': 4,
                    'STRESS_O1': f'Worker {worker_id} qualitative feedback',
                    'STRESS_M1': self.opt1.id,
                },
                'guestParticipantId': f'forged-guest-{worker_id}',
            }
            res = client.post(url, data=json.dumps(payload), content_type='application/json', HTTP_X_FEEDORA_GUEST_TOKEN=guest_session['guestToken'])
            return res.status_code, res.json()

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(submit_worker, i) for i in range(num_workers)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        for status_code, data in results:
            self.assertEqual(status_code, 200)
            self.assertTrue(data.get('ok'))
            self.assertGreater(
                data.get('pointsEarned', 0),
                0,
                f"A unique guest's first response lost its award: {data!r}",
            )

        # Assert exactly num_workers responses were recorded
        self.assertEqual(Response.objects.filter(survey=self.survey).count(), num_workers)
        self.assertEqual(Answer.objects.filter(question=self.q_likert).count(), num_workers)

    def test_post_commit_receipt_read_lock_does_not_replay_submission(self):
        """A locked receipt aggregate retries its read, never the committed submission."""
        from django.test import Client
        from feedora_project import api_views

        client = Client()
        guest_session = client.post('/api/v1/guest/session/').json()
        url = f'/api/v1/surveys/{self.survey.access_uuid}/submit/'
        payload = {
            'answers': {
                'STRESS_L1': 4,
                'STRESS_O1': 'A useful qualitative response for the study.',
                'STRESS_M1': self.opt1.id,
            },
        }
        real_scope_total_points = api_views._scope_total_points
        calls = 0

        def locked_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OperationalError('database table is locked')
            return real_scope_total_points(*args, **kwargs)

        with patch('feedora_project.api_views._scope_total_points', side_effect=locked_once):
            response = client.post(
                url,
                data=json.dumps(payload),
                content_type='application/json',
                HTTP_X_FEEDORA_GUEST_TOKEN=guest_session['guestToken'],
            )

        self.assertEqual(response.status_code, 200)
        self.assertGreater(response.json()['pointsEarned'], 0)
        self.assertEqual(calls, 2)
        self.assertEqual(Response.objects.filter(survey=self.survey).count(), 1)
        self.assertEqual(Answer.objects.filter(response__survey=self.survey).count(), 3)

    def test_platform_receipt_aggregate_retries_sqlite_read_lock(self):
        """Receipt serialization retries this aggregate without replaying the accepted write."""
        from feedora_project import api_views

        class LockedOnceQuerySet:
            calls = 0

            def filter(self, **_kwargs):
                return self

            def aggregate(self, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise OperationalError('database table is locked')
                return {'total': 17}

        queryset = LockedOnceQuerySet()
        with patch.object(api_views, '_responses_for_principal', return_value=queryset):
            total = api_views._platform_total_points(guest_participant_id='guest-1')

        self.assertEqual(total, 17)
        self.assertEqual(queryset.calls, 2)

    def test_concurrent_replay_of_one_submission_creates_one_receipt(self):
        """A lost response retried by multiple workers must create one response and one award."""
        num_workers = 6
        url = f'/api/v1/surveys/{self.survey.access_uuid}/submit/'
        submission_id = str(uuid.uuid4())
        payload = {
            'submissionId': submission_id,
            'guestParticipantId': 'forged-guest',
            'answers': {
                'STRESS_L1': 4,
                'STRESS_O1': 'One stable response retried concurrently.',
            },
        }

        def submit_worker(_):
            from django.test import Client
            return Client().post(url, data=json.dumps(payload), content_type='application/json', HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token)

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            responses = list(executor.map(submit_worker, range(num_workers)))

        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(Response.objects.filter(survey=self.survey, guest_participant_id=self.guest_id).count(), 1)
        receipt_ids = {response.json()['responseId'] for response in responses}
        self.assertEqual(len(receipt_ids), 1)
        self.assertEqual(Answer.objects.filter(response_id=next(iter(receipt_ids))).count(), 2)

    def test_concurrent_start_reserves_one_attempt_for_one_guest(self):
        """Competing start requests must converge on one DB-unique reservation."""
        self.survey.features = {'accessPolicy': {'allowRedo': False, 'maxAttempts': 1}}
        self.survey.save(update_fields=['features'])
        url = f'/api/v1/surveys/{self.survey.access_uuid}/attempts/start/'

        def start_worker(_):
            from django.test import Client
            attempt_id = str(uuid.uuid4())
            response = Client().post(
                url,
                data=json.dumps({'attemptId': attempt_id}),
                content_type='application/json',
                HTTP_X_FEEDORA_GUEST_TOKEN=self.guest_token,
            )
            return response.status_code, response.json()

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(start_worker, range(8)))

        self.assertTrue(all(status in (200, 201) for status, _ in results), results)
        attempt_ids = {data['attemptId'] for _, data in results}
        self.assertEqual(len(attempt_ids), 1)
        active = SurveyAttempt.objects.filter(
            survey=self.survey, guest_principal_id=self.guest_id, status=SurveyAttempt.IN_PROGRESS,
        )
        self.assertEqual(active.count(), 1)
        self.assertEqual(active.get().active_key, f'{self.survey.id}:guest:{self.guest_id}')

    def test_schema_drift_reconciles_when_client_submits_unknown_questions(self):
        """Verify client submitting orphaned or deleted question IDs does not trigger 500 error."""
        from django.test import Client
        client = Client()
        guest_session = client.post('/api/v1/guest/session/').json()
        url = f'/api/v1/surveys/{self.survey.access_uuid}/submit/'
        payload = {
            'answers': {
                'STRESS_L1': 5,
                'ORPHANED_UNKNOWN_QUESTION_CODE': 'Should be ignored gracefully',
                '99999999': 'Unknown ID',
            }
        }
        res = client.post(url, data=json.dumps(payload), content_type='application/json', HTTP_X_FEEDORA_GUEST_TOKEN=guest_session['guestToken'])
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get('ok'))
        self.assertEqual(data.get('pointsEarned'), 10)  # Only STRESS_L1 scored

    def test_unbounded_text_payload_is_safely_truncated(self):
        """Verify 50 KB text payload is safely accepted and truncated to 10,000 characters."""
        from django.test import Client
        client = Client()
        guest_session = client.post('/api/v1/guest/session/').json()
        url = f'/api/v1/surveys/{self.survey.access_uuid}/submit/'
        huge_text = 'A' * 50000
        payload = {
            'answers': {
                'STRESS_O1': huge_text,
            }
        }
        res = client.post(url, data=json.dumps(payload), content_type='application/json', HTTP_X_FEEDORA_GUEST_TOKEN=guest_session['guestToken'])
        self.assertEqual(res.status_code, 200)
        ans = Answer.objects.filter(question=self.q_open).last()
        self.assertIsNotNone(ans)
        self.assertLessEqual(len(ans.text_value), 10000)
