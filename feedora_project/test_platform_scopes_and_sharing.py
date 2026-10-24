import json
import uuid

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from accounts.models import Organization
from surveys.models import PlatformSharedAggregate, Question, Survey


User = get_user_model()


class PlatformScopeAndSharingTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.owner = User.objects.create_user(
            username='scope-owner@example.test',
            email='scope-owner@example.test',
            password='StrongPassword123!',
            role='creator',
        )
        self.organization = Organization.objects.create(created_by=self.owner, org_name='Scope Test Org')
        self.org_survey = Survey.objects.create(
            created_by=self.owner,
            organization_scope=self.organization,
            scope_kind='organization',
            title='Organization feedback',
            features={'points': {'enabled': True}},
        )
        self.platform_survey = Survey.objects.create(
            created_by=self.owner,
            scope_kind='platform',
            title='Platform feedback',
            features={'points': {'enabled': True}},
        )
        for survey in (self.org_survey, self.platform_survey):
            Question.objects.create(
                survey=survey,
                code='Q1',
                text='Rate this',
                question_type='likert',
                rating_max=5,
            )

    def issue_guest(self):
        response = self.client.post('/api/v1/guest/session/')
        self.assertEqual(response.status_code, 200)
        return response.json()['guestToken']

    def submit(self, token, survey):
        return self.client.post(
            f'/api/v1/surveys/{survey.access_uuid}/submit/',
            data=json.dumps({
                'submissionId': str(uuid.uuid4()),
                'answers': {'Q1': 4},
            }),
            content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=token,
        )

    def test_platform_is_a_selectable_separate_scope(self):
        entities = self.client.get('/api/v1/entities/').json()['entities']
        platform = next(item for item in entities if item['entityId'] == 'platform')
        self.assertEqual(platform['scopeKind'], 'platform')

        platform_catalog = self.client.get('/api/v1/surveys/', {'entity_id': 'platform'}).json()['surveys']
        self.assertEqual([item['uuid'] for item in platform_catalog], [str(self.platform_survey.access_uuid)])
        org_catalog = self.client.get(
            '/api/v1/surveys/', {'entity_id': f'org-{self.organization.id}'},
        ).json()['surveys']
        self.assertEqual([item['uuid'] for item in org_catalog], [str(self.org_survey.access_uuid)])

        leaderboard = self.client.get('/api/v1/leaderboard/', {'entity_id': 'platform'}).json()
        self.assertEqual(leaderboard['scopeKind'], 'platform')
        self.assertEqual(leaderboard['scopeId'], 'platform')

    def test_points_keep_scope_totals_and_history_compatibility_total(self):
        token = self.issue_guest()
        org_result = self.submit(token, self.org_survey)
        platform_result = self.submit(token, self.platform_survey)
        self.assertEqual(org_result.status_code, 200)
        self.assertEqual(platform_result.status_code, 200)
        self.assertEqual(org_result.json()['scopeKind'], 'organization')
        self.assertEqual(platform_result.json()['scopeKind'], 'platform')
        self.assertEqual(platform_result.json()['platformPoints'], platform_result.json()['pointsEarned'])

        history = self.client.get(
            '/api/v1/respondent/history/', HTTP_X_FEEDORA_GUEST_TOKEN=token,
        ).json()
        by_scope = {item['scopeKind']: item['points'] for item in history['scopePoints']}
        scope_rows = {item['scopeKind']: item for item in history['scopePoints']}
        self.assertEqual(by_scope['organization'], org_result.json()['pointsEarned'])
        self.assertEqual(by_scope['platform'], platform_result.json()['pointsEarned'])
        self.assertEqual(scope_rows['organization']['completedResponses'], 1)
        self.assertEqual(scope_rows['platform']['completedResponses'], 1)
        self.assertEqual(scope_rows['platform']['levelInfo']['points'], by_scope['platform'])
        self.assertEqual(history['totalPoints'], org_result.json()['pointsEarned'] + platform_result.json()['pointsEarned'])
        self.assertEqual(history['platformPoints'], platform_result.json()['pointsEarned'])

    def test_organization_sharing_defaults_on_and_can_be_disabled(self):
        self.assertTrue(self.organization.share_aggregate_stats_to_platform)
        self.client.force_login(self.owner)
        disabled = self.client.post(
            f'/api/v1/entities/{self.organization.id}/platform-sharing/',
            data=json.dumps({'shareAggregateStats': False}),
            content_type='application/json',
        )
        self.assertEqual(disabled.status_code, 200)
        self.assertFalse(disabled.json()['shareAggregateStats'])

        approval = self.client.post(
            f'/api/v1/entities/{self.organization.id}/platform-sharing/',
            data=json.dumps({'shareAggregateStats': True}),
            content_type='application/json',
        )
        self.assertEqual(approval.status_code, 200)
        self.assertTrue(approval.json()['shareAggregateStats'])

        token = self.issue_guest()
        submitted = self.submit(token, self.org_survey)
        self.assertEqual(submitted.status_code, 200)
        aggregate = PlatformSharedAggregate.objects.get(organization=self.organization, survey=self.org_survey)
        self.assertEqual(aggregate.completion_count, 1)

        platform_admin = User.objects.create_superuser(
            username='platform-admin@example.test',
            email='platform-admin@example.test',
            password='StrongPassword123!',
        )
        self.client.force_login(platform_admin)
        shared = self.client.get('/api/v1/platform/aggregate-stats/').json()
        self.assertEqual(shared['aggregates'][0]['completedResponses'], 1)
        self.assertNotIn('points', shared['aggregates'][0])
        self.assertNotIn('respondent', shared['aggregates'][0])

        self.client.force_login(self.owner)
        revoked = self.client.post(
            f'/api/v1/entities/{self.organization.id}/platform-sharing/',
            data=json.dumps({'shareAggregateStats': False}),
            content_type='application/json',
        )
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(PlatformSharedAggregate.objects.filter(organization=self.organization).count(), 0)
