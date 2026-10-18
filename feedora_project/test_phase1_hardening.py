"""
feedora_project/test_phase1_hardening.py
Verification suite for Feedora backend data integrity, session management, and validation.
Tests:
  1. Registration succeeds and establishes active session under multi-backend auth.
  2. Submissions strictly enforce rating boundaries (rejecting out-of-bounds integers with 400).
  3. Empty submissions are rejected with 400.
  4. Idempotency guarantees: repeat submissions with same submissionId return original 200 without duplicating points.
  5. RespondentProfile.total_points is atomically incremented on submission.
  6. Duplicate submissions by the same user to the same survey are prevented (409 Conflict).
  7. CSRF validation is enforced on write endpoints when enforce_csrf_checks=True.
"""
import json
import uuid
from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from surveys.models import Survey, Question, MCQOption, Response, Answer
from accounts.models import RespondentProfile

User = get_user_model()


class Phase1DataIntegrityTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.creator = User.objects.create_user(
            username='lab_lead@feedora.org',
            email='lab_lead@feedora.org',
            password='Password123!',
            role='creator',
            is_active=True,
        )
        self.survey = Survey.objects.create(
            organization=self.creator,
            title='Empirical HMSAM Study',
            is_active=True,
            features={'points': {'enabled': True, 'values': {'likert': 15}}}
        )
        self.q_likert = Question.objects.create(
            survey=self.survey,
            code='JOY1',
            text='Using this tool is enjoyable',
            question_type='likert',
            rating_max=5,
            order=1,
        )
        self.q_open = Question.objects.create(
            survey=self.survey,
            code='FEEDBACK1',
            text='What did you enjoy most?',
            question_type='open',
            order=2,
        )

    def test_registration_establishes_authenticated_session(self):
        """Registration creates user, profile, and active session without multi-backend error."""
        payload = {
            'email': 'new_respondent@feedora.org',
            'password': 'SecurePassword123!',
            'persona': 'respondent',
            'displayName': 'New Respondent',
        }
        res = self.client.post(
            '/api/v1/auth/register/',
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertIn(res.status_code, (200, 201))
        data = res.json()
        self.assertTrue(data.get('ok'))
        self.assertEqual(data['user']['email'], 'new_respondent@feedora.org')
        self.assertTrue(data.get('csrfToken'))

        # Verify session is actively authenticated
        me_res = self.client.get('/api/v1/auth/me/')
        self.assertEqual(me_res.status_code, 200)
        self.assertTrue(me_res.json()['authenticated'])
        self.assertEqual(me_res.json()['user']['email'], 'new_respondent@feedora.org')

    def test_out_of_bounds_rating_rejected_with_400(self):
        """Ratings outside the valid scale (e.g. 99 or -5 on a 1..5 scale) must return 400 and write zero rows."""
        url = f'/api/v1/surveys/{self.survey.access_uuid}/submit/'

        # Test positive out-of-range value
        res_high = self.client.post(
            url,
            data=json.dumps({
                'submissionId': str(uuid.uuid4()),
                'answers': {'JOY1': 99}
            }),
            content_type='application/json',
        )
        self.assertEqual(res_high.status_code, 400)
        self.assertFalse(res_high.json()['ok'])

        # Test negative out-of-range value
        res_low = self.client.post(
            url,
            data=json.dumps({
                'submissionId': str(uuid.uuid4()),
                'answers': {'JOY1': -300}
            }),
            content_type='application/json',
        )
        self.assertEqual(res_low.status_code, 400)
        self.assertFalse(res_low.json()['ok'])

        # Confirm zero responses or answers were saved
        self.assertEqual(Response.objects.filter(survey=self.survey).count(), 0)

    def test_empty_submission_rejected_with_400(self):
        """A submission with no answers must be rejected with 400 instead of creating an empty response."""
        url = f'/api/v1/surveys/{self.survey.access_uuid}/submit/'
        res = self.client.post(
            url,
            data=json.dumps({'submissionId': str(uuid.uuid4()), 'answers': {}}),
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(Response.objects.filter(survey=self.survey).count(), 0)

    def test_idempotent_resubmission_with_same_submission_id(self):
        """Retrying a submit with identical submissionId returns the original 200 without creating extra rows or points."""
        user = User.objects.create_user(
            username='repeat_user@feedora.org',
            email='repeat_user@feedora.org',
            password='Password123!',
            role='respondent',
        )
        profile = RespondentProfile.objects.create(user=user, total_points=0)
        self.client.force_login(user)

        client_sub_id = str(uuid.uuid4())
        payload = {
            'submissionId': client_sub_id,
            'answers': {'JOY1': 5, 'FEEDBACK1': 'Great interactive experience!'}
        }
        url = f'/api/v1/surveys/{self.survey.access_uuid}/submit/'

        # First submission
        res1 = self.client.post(url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res1.status_code, 200)
        pts1 = res1.json()['pointsEarned']
        self.assertGreater(pts1, 0)

        profile.refresh_from_db()
        self.assertEqual(profile.total_points, pts1)
        self.assertEqual(Response.objects.filter(survey=self.survey, user=user).count(), 1)

        # Flapping network retry with same submissionId
        res2 = self.client.post(url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()
        self.assertTrue(data2.get('idempotent'))
        self.assertEqual(data2['submissionId'], client_sub_id)

        # Profile points and response count must remain unchanged
        profile.refresh_from_db()
        self.assertEqual(profile.total_points, pts1)
        self.assertEqual(Response.objects.filter(survey=self.survey, user=user).count(), 1)

    def test_duplicate_submission_by_same_user_rejected_with_409(self):
        """Submitting a second time with a different submissionId returns 409 Conflict when multi-submit is disabled."""
        user = User.objects.create_user(
            username='once_user@feedora.org',
            email='once_user@feedora.org',
            password='Password123!',
            role='respondent',
        )
        RespondentProfile.objects.create(user=user, total_points=0)
        self.client.force_login(user)
        url = f'/api/v1/surveys/{self.survey.access_uuid}/submit/'

        # First submission
        res1 = self.client.post(
            url,
            data=json.dumps({'submissionId': str(uuid.uuid4()), 'answers': {'JOY1': 4}}),
            content_type='application/json',
        )
        self.assertEqual(res1.status_code, 200)

        # Second submission attempt by same user with new submissionId
        res2 = self.client.post(
            url,
            data=json.dumps({'submissionId': str(uuid.uuid4()), 'answers': {'JOY1': 3}}),
            content_type='application/json',
        )
        self.assertEqual(res2.status_code, 409)
        self.assertFalse(res2.json()['ok'])

    def test_profile_total_points_updated_atomically(self):
        """Submitting responses increments RespondentProfile.total_points by the exact points earned."""
        user = User.objects.create_user(
            username='points_user@feedora.org',
            email='points_user@feedora.org',
            password='Password123!',
            role='respondent',
        )
        profile = RespondentProfile.objects.create(user=user, total_points=100)
        self.client.force_login(user)
        url = f'/api/v1/surveys/{self.survey.access_uuid}/submit/'

        res = self.client.post(
            url,
            data=json.dumps({'submissionId': str(uuid.uuid4()), 'answers': {'JOY1': 5}}),
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 200)
        earned = res.json()['pointsEarned']

        profile.refresh_from_db()
        self.assertEqual(profile.total_points, 100 + earned)

    def test_csrf_protection_with_enforced_checks(self):
        """When CSRF checks are enforced, write requests require a valid CSRF token."""
        csrf_client = Client(enforce_csrf_checks=True)

        # 1. Fetch survey meta to obtain CSRF token and cookie
        meta_url = f'/api/v1/surveys/{self.survey.access_uuid}/meta/'
        meta_res = csrf_client.get(meta_url)
        self.assertEqual(meta_res.status_code, 200)
        token = meta_res.json().get('csrfToken')
        self.assertTrue(token)

        # 2. Submitting without X-CSRFToken header should return 403 Forbidden
        submit_url = f'/api/v1/surveys/{self.survey.access_uuid}/submit/'
        payload = {'submissionId': str(uuid.uuid4()), 'answers': {'JOY1': 4}}
        res_no_token = csrf_client.post(
            submit_url,
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(res_no_token.status_code, 403)

        # 3. Submitting with valid HTTP_X_CSRFTOKEN header succeeds with 200 OK
        res_with_token = csrf_client.post(
            submit_url,
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(res_with_token.status_code, 200)
        self.assertTrue(res_with_token.json()['ok'])

    def test_entities_list_and_surveys(self):
        """Entities list endpoint returns active organizations and their surveys."""
        from accounts.models import Organization
        org = Organization.objects.create(
            user=self.creator,
            org_name='University of Moratuwa',
            org_type='university',
            user_role='Lead Researcher',
            country='Sri Lanka',
        )

        res = self.client.get('/api/v1/entities/')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['ok'])
        self.assertTrue(any(e['name'] == 'University of Moratuwa' for e in data['entities']))

        surveys_res = self.client.get(f'/api/v1/entities/{org.id}/surveys/')
        self.assertEqual(surveys_res.status_code, 200)
        s_data = surveys_res.json()
        self.assertTrue(s_data['ok'])
        self.assertEqual(len(s_data['surveys']), 1)
        self.assertEqual(s_data['surveys'][0]['title'], 'Empirical HMSAM Study')

    def test_entity_join_authenticated(self):
        """Respondent can join an organization, updating both Organization and RespondentProfile."""
        from accounts.models import Organization
        org = Organization.objects.create(
            user=self.creator,
            org_name='MIT Media Lab',
            org_type='research',
            country='USA',
        )

        # Anonymous request should return 401
        res_anon = self.client.post(f'/api/v1/entities/{org.id}/join/')
        self.assertEqual(res_anon.status_code, 401)

        # Authenticated respondent joins successfully
        user = User.objects.create_user(
            username='joiner@feedora.org',
            email='joiner@feedora.org',
            password='Password123!',
            role='respondent',
        )
        self.client.force_login(user)
        res_auth = self.client.post(f'/api/v1/entities/{org.id}/join/')
        self.assertEqual(res_auth.status_code, 200)
        self.assertTrue(res_auth.json()['ok'])
        self.assertEqual(res_auth.json()['organization']['orgName'], 'MIT Media Lab')

    def test_respondent_history_badges_and_points_by_organization(self):
        """History endpoint returns cumulative points, level info, badges, and per-organization aggregation."""
        from accounts.models import Organization
        Organization.objects.create(
            user=self.creator,
            org_name='University of Colombo',
            org_type='university',
            country='Sri Lanka',
        )
        user = User.objects.create_user(
            username='badge_earner@feedora.org',
            email='badge_earner@feedora.org',
            password='Password123!',
            role='respondent',
        )
        self.client.force_login(user)

        # Submit first response -> earns Bronze badge
        url = f'/api/v1/surveys/{self.survey.access_uuid}/submit/'
        res = self.client.post(
            url,
            data=json.dumps({'submissionId': str(uuid.uuid4()), 'answers': {'JOY1': 4}}),
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 200)

        history_res = self.client.get('/api/v1/respondent/history/')
        self.assertEqual(history_res.status_code, 200)
        h_data = history_res.json()
        self.assertTrue(h_data['ok'])
        self.assertEqual(h_data['submissionsCount'], 1)
        self.assertTrue(any(b['id'] == 'bronze' for b in h_data['badges']))
        self.assertTrue(any(o['organization'] == 'University of Colombo' for o in h_data['pointsByOrganization']))

    def test_arbitrary_rating_scale_analytics_dual_scoring(self):
        """Analytics engine handles 7-point scales with normalized interval scores without distortion."""
        from surveys.models import Survey, Question, Response, Answer
        from surveys.analytics import get_hmsam_scores

        s7 = Survey.objects.create(
            organization=self.creator,
            title='7-Point Likert Study',
            is_active=True,
        )
        q7 = Question.objects.create(
            survey=s7,
            code='JOY1',
            text='Enjoyment on 7-point scale',
            question_type='likert',
            hmsam_construct='joy',
            rating_max=7,
            order=1,
        )
        resp = Response.objects.create(survey=s7, user=self.creator)
        Answer.objects.create(response=resp, question=q7, likert_value=7)

        scores, overall = get_hmsam_scores(s7)
        self.assertIn('joy', scores)
        # Max on 7-point scale should normalize to 100%
        self.assertEqual(scores['joy']['normalized_score'], 100.0)
        self.assertEqual(scores['joy']['scale_max'], 7)
        self.assertEqual(scores['joy']['mean'], 7.0)
