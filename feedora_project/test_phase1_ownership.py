from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from accounts.models import Organization, OrganizationMembership
from surveys.models import Survey


User = get_user_model()


class PhaseOneOwnershipTests(TestCase):
    def setUp(self):
        self.creator = User.objects.create_user(username='creator', password='test', role='creator')
        self.owner = User.objects.create_user(username='owner', password='test', role='creator')

    def create_organization(self, name):
        organization = Organization.objects.create(created_by=self.creator, org_name=name)
        OrganizationMembership.objects.create(
            user=self.owner,
            organization=organization,
            role=OrganizationMembership.OWNER,
            status=OrganizationMembership.ACTIVE,
        )
        return organization

    def test_one_account_can_create_multiple_organizations_with_independent_owners(self):
        first = self.create_organization('First Lab')
        second = self.create_organization('Second Lab')

        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(Organization.objects.filter(created_by=self.creator).count(), 2)
        self.assertEqual(
            OrganizationMembership.objects.filter(
                user=self.owner,
                organization__in=[first, second],
                role=OrganizationMembership.OWNER,
                status=OrganizationMembership.ACTIVE,
            ).count(),
            2,
        )
        from django.test import Client
        client = Client()
        client.force_login(self.owner)
        account = client.get('/api/v1/auth/me/').json()['user']
        self.assertEqual({item['id'] for item in account['organizations']}, {str(first.pk), str(second.pk)})
        self.assertIsNone(account['organization'])

    def test_survey_creator_is_independent_of_organization_creator_and_scope(self):
        organization = self.create_organization('Research Lab')
        survey = Survey.objects.create(
            created_by=self.owner,
            organization_scope=organization,
            scope_kind='organization',
            title='Independent authorship',
        )

        survey.refresh_from_db()
        self.assertEqual(survey.created_by_id, self.owner.pk)
        self.assertEqual(survey.organization_scope_id, organization.pk)
        self.assertEqual(organization.created_by_id, self.creator.pk)

    def test_leaving_membership_does_not_reassign_survey_scope_or_creator(self):
        organization = self.create_organization('Stable Scope Lab')
        survey = Survey.objects.create(
            created_by=self.owner,
            organization_scope=organization,
            scope_kind='organization',
            title='Stable ownership',
        )
        membership = OrganizationMembership.objects.get(user=self.owner, organization=organization)
        membership.status = OrganizationMembership.LEFT
        membership.save(update_fields=['status'])

        survey.refresh_from_db()
        self.assertEqual(survey.organization_scope_id, organization.pk)
        self.assertEqual(survey.created_by_id, self.owner.pk)

    def test_organization_cannot_be_deleted_while_it_owns_surveys(self):
        organization = self.create_organization('Protected Research Scope')
        Survey.objects.create(
            created_by=self.owner,
            organization_scope=organization,
            scope_kind='organization',
            title='Must retain scope',
        )
        with self.assertRaises(ProtectedError):
            organization.delete()

    def test_membership_identity_cannot_be_duplicated_to_bypass_status(self):
        organization = self.create_organization('Unique Membership Lab')
        with self.assertRaises(IntegrityError), transaction.atomic():
            OrganizationMembership.objects.create(
                user=self.owner,
                organization=organization,
                role=OrganizationMembership.OWNER,
                status=OrganizationMembership.ACTIVE,
            )

    def test_scope_catalog_isolated_and_org_owner_sees_only_their_resources(self):
        from django.test import Client

        first = self.create_organization('First Scope')
        second = self.create_organization('Second Scope')
        Survey.objects.create(
            created_by=self.creator,
            organization_scope=first,
            scope_kind='organization',
            title='First only',
        )
        Survey.objects.create(
            created_by=self.creator,
            organization_scope=second,
            scope_kind='organization',
            title='Second only',
        )

        client = Client()
        self.assertEqual(
            [item['title'] for item in client.get(f'/api/v1/entities/{first.pk}/surveys/').json()['surveys']],
            ['First only'],
        )
        self.assertEqual(
            [item['title'] for item in client.get(f'/api/v1/entities/{second.pk}/surveys/').json()['surveys']],
            ['Second only'],
        )
        client.force_login(self.owner)
        mine = client.get('/api/v1/surveys/mine/').json()['surveys']
        self.assertEqual({item['title'] for item in mine}, {'First only', 'Second only'})

    def test_survey_creation_requires_active_owner_and_persists_explicit_scope(self):
        from django.test import Client

        organization = self.create_organization('Explicit Scope')
        client = Client()
        client.force_login(self.owner)
        payload = {'title': 'Owned by scope', 'organizationId': organization.pk, 'questions': []}
        response = client.post('/api/v1/surveys/create/', payload, content_type='application/json')
        self.assertEqual(response.status_code, 201)
        survey = Survey.objects.get(pk=response.json()['surveyId'])
        self.assertEqual(survey.created_by_id, self.owner.pk)
        self.assertEqual(survey.organization_scope_id, organization.pk)

        membership = OrganizationMembership.objects.get(user=self.owner, organization=organization)
        membership.status = OrganizationMembership.LEFT
        membership.save(update_fields=['status'])
        rejected = client.post(
            '/api/v1/surveys/create/',
            {'title': 'Must not cross', 'organizationId': organization.pk, 'questions': []},
            content_type='application/json',
        )
        self.assertEqual(rejected.status_code, 403)

    def test_global_creator_can_create_another_organization_scope(self):
        from django.test import Client

        client = Client()
        client.force_login(self.creator)
        response = client.post(
            '/api/v1/entities/create/',
            {'name': 'New research group', 'type': 'research', 'country': 'Sri Lanka'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 201)
        organization_id = response.json()['entity']['id']
        self.assertTrue(OrganizationMembership.objects.filter(
            user=self.creator,
            organization_id=organization_id,
            role=OrganizationMembership.OWNER,
            status=OrganizationMembership.ACTIVE,
        ).exists())

    def test_active_organization_admin_can_create_and_manage_scoped_surveys(self):
        from django.test import Client

        organization = self.create_organization('Admin Managed Lab')
        admin = User.objects.create_user(username='org-admin', password='test', role='respondent')
        OrganizationMembership.objects.create(
            user=admin,
            organization=organization,
            role=OrganizationMembership.ADMIN,
            status=OrganizationMembership.ACTIVE,
        )
        client = Client()
        client.force_login(admin)
        response = client.post(
            '/api/v1/surveys/create/',
            {'title': 'Admin authored survey', 'organizationId': organization.pk, 'questions': []},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 201)
        survey = Survey.objects.get(pk=response.json()['surveyId'])
        self.assertEqual(survey.organization_scope_id, organization.pk)
        self.assertIn('Admin authored survey', {
            item['title'] for item in client.get('/api/v1/surveys/mine/').json()['surveys']
        })

    def test_admin_does_not_receive_owner_only_aggregate_sharing_authority(self):
        from django.test import Client

        organization = self.create_organization('Owner Consent Lab')
        admin = User.objects.create_user(username='sharing-admin', password='test', role='respondent')
        OrganizationMembership.objects.create(
            user=admin,
            organization=organization,
            role=OrganizationMembership.ADMIN,
            status=OrganizationMembership.ACTIVE,
        )
        client = Client()
        client.force_login(admin)
        response = client.post(
            f'/api/v1/entities/{organization.pk}/platform-sharing/',
            {'shareAggregateStats': False},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)
        organization.refresh_from_db()
        self.assertTrue(organization.share_aggregate_stats_to_platform)

    def test_quarantined_unscoped_survey_is_not_public_or_submittable(self):
        from django.test import Client

        survey = Survey.objects.create(
            created_by=self.creator,
            scope_kind='organization',
            scope_review_required=True,
            title='Needs scope review',
        )
        client = Client()
        self.assertEqual(client.get(f'/api/v1/surveys/{survey.access_uuid}/meta/').status_code, 404)

    def test_platform_survey_cannot_also_claim_an_organization_scope(self):
        from django.core.exceptions import ValidationError

        organization = self.create_organization('Platform Separation Lab')
        with self.assertRaises(ValidationError):
            Survey.objects.create(
                created_by=self.owner,
                organization_scope=organization,
                scope_kind='platform',
                title='Must have exactly one scope kind',
            )
