import json
from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from surveys.models import Survey
from accounts.models import Organization

User = get_user_model()


class ApiV1Tests(TestCase):
    def setUp(self):
        self.client = Client()
        # Create a test creator / superuser
        self.admin_user = User.objects.create_superuser(
            username='TestAdmin',
            email='admin@test.com',
            password='testpassword123'
        )
        # Create a regular respondent user
        self.resp_user = User.objects.create_user(
            username='TestRespondent',
            email='respondent@test.com',
            password='testpassword123',
            role='respondent'
        )
        # Create a test survey
        organization = Organization.objects.create(created_by=self.admin_user, org_name='API Test Org')
        self.survey = Survey.objects.create(
            created_by=self.admin_user,
            organization_scope=organization,
            scope_kind='organization',
            title='Sample API Survey',
            is_active=True
        )

    def test_admin_login_creator_persona(self):
        """Superuser with role=None MUST be permitted into creator persona."""
        response = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({
                'identity': 'TestAdmin',
                'password': 'testpassword123',
                'persona': 'creator'
            }),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get('ok'))
        self.assertEqual(data['user']['username'], 'TestAdmin')
        self.assertEqual(data['user']['persona'], 'creator')
        self.assertTrue(data['user']['isStaff'])

    def test_login_by_email(self):
        """Login by email address succeeds."""
        response = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({
                'identity': 'admin@test.com',
                'password': 'testpassword123',
                'persona': 'creator'
            }),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])

    def test_invalid_password_returns_401(self):
        """Invalid password returns 401."""
        response = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({
                'identity': 'TestAdmin',
                'password': 'wrongpassword',
                'persona': 'creator'
            }),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 401)
        data = response.json()
        self.assertFalse(data['ok'])

    def test_respondent_cannot_login_as_creator(self):
        """Standard respondent is blocked from creator persona."""
        response = self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({
                'identity': 'TestRespondent',
                'password': 'testpassword123',
                'persona': 'creator'
            }),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 403)

    def test_auth_me_and_logout(self):
        """Test me endpoint before and after login/logout."""
        # Unauthenticated me
        me_resp = self.client.get('/api/v1/auth/me/')
        self.assertEqual(me_resp.status_code, 200)
        self.assertFalse(me_resp.json()['authenticated'])

        # Login
        self.client.post(
            '/api/v1/auth/login/',
            data=json.dumps({
                'identity': 'TestRespondent',
                'password': 'testpassword123',
                'persona': 'respondent'
            }),
            content_type='application/json'
        )

        # Authenticated me
        me_auth = self.client.get('/api/v1/auth/me/')
        self.assertTrue(me_auth.json()['authenticated'])
        self.assertEqual(me_auth.json()['user']['username'], 'TestRespondent')

        # Logout
        logout_resp = self.client.post('/api/v1/auth/logout/')
        self.assertEqual(logout_resp.status_code, 200)
        me_after = self.client.get('/api/v1/auth/me/')
        self.assertFalse(me_after.json()['authenticated'])

    def test_openapi_spec_and_swagger(self):
        """Verify OpenAPI spec endpoint and Swagger UI load."""
        spec_resp = self.client.get('/api/v1/openapi.json')
        self.assertEqual(spec_resp.status_code, 200)
        spec = spec_resp.json()
        self.assertEqual(spec['openapi'], '3.1.0')
        self.assertIn('/api/v1/auth/login/', spec['paths'])

        docs_resp = self.client.get('/api/v1/docs/')
        self.assertEqual(docs_resp.status_code, 200)
        self.assertIn(b'swagger-ui', docs_resp.content)

    def test_unauthenticated_cannot_create_survey(self):
        """Unauthenticated requests to /api/v1/surveys/create/ MUST return 401."""
        response = self.client.post(
            '/api/v1/surveys/create/',
            data=json.dumps({'title': 'Rogue Survey'}),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 401)

    def test_creator_can_create_survey_and_questions(self):
        """Authenticated creator can scaffold and publish surveys with questions."""
        # Login as admin/creator
        self.client.force_login(self.admin_user)
        payload = {
            'title': 'Autonomous AI Study',
            'description': 'Evaluating agentic flow interactions.',
            'is_hmsam': True,
            'questions': [
                {
                    'text': 'The interface responds quickly to my requests.',
                    'code': 'PEU1',
                    'type': 'likert',
                    'construct': 'peou',
                    'required': True,
                    'ratingMax': 5
                },
                {
                    'text': 'Which platform do you prefer?',
                    'code': 'PLAT1',
                    'type': 'mcq',
                    'required': True,
                    'options': ['Expo Native', 'Web Browser', 'Desktop']
                }
            ]
        }
        response = self.client.post(
            '/api/v1/surveys/create/',
            data=json.dumps(payload),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertIn('uuid', data)
        self.assertIn('accessCode', data)

        # Verify created in DB
        created_survey = Survey.objects.get(access_uuid=data['uuid'])
        self.assertEqual(created_survey.title, 'Autonomous AI Study')
        self.assertEqual(created_survey.questions.count(), 2)
        q_mcq = created_survey.questions.filter(question_type='mcq').first()
        self.assertIsNotNone(q_mcq)
        self.assertEqual(q_mcq.options.count(), 3)

    def test_creator_list_my_surveys(self):
        """Creator can list their own surveys with question counts."""
        self.client.force_login(self.admin_user)
        response = self.client.get('/api/v1/surveys/mine/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertGreaterEqual(len(data['surveys']), 1)

    def test_survey_meta_and_json_submit(self):
        """Respondent can fetch survey meta and submit answers via JSON API."""
        from surveys.models import Question
        q1 = Question.objects.create(
            survey=self.survey,
            text='Rate your experience',
            question_type='likert',
            order=0,
            rating_max=5
        )

        # 1. Fetch meta
        meta_resp = self.client.get(f'/api/v1/surveys/{self.survey.access_uuid}/meta/')
        self.assertEqual(meta_resp.status_code, 200)
        meta_data = meta_resp.json()
        self.assertTrue(meta_data['ok'])
        self.assertEqual(len(meta_data['questions']), 1)

        # 2. Submit response via JSON
        guest = self.client.post('/api/v1/guest/session/').json()
        submit_payload = {
            'answers': {
                str(q1.id): 5
            }
        }
        submit_resp = self.client.post(
            f'/api/v1/surveys/{self.survey.access_uuid}/submit/',
            data=json.dumps(submit_payload),
            content_type='application/json',
            HTTP_X_FEEDORA_GUEST_TOKEN=guest['guestToken'],
        )
        self.assertEqual(submit_resp.status_code, 200)
        submit_data = submit_resp.json()
        self.assertTrue(submit_data['ok'])
        self.assertGreater(submit_data['pointsEarned'], 0)
        self.assertEqual(self.survey.responses.count(), 1)

