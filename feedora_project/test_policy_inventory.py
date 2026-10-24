import json
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.core.management import call_command
from django.test import TestCase

from accounts.models import Organization
from surveys.models import Survey


User = get_user_model()


class SurveyPolicyInventoryTests(TestCase):
    def setUp(self):
        self.creator = User.objects.create_user(
            username='inventory-owner@example.test', email='inventory-owner@example.test',
            password='Password123!', role='creator',
        )
        self.organization = Organization.objects.create(created_by=self.creator, org_name='Inventory Lab')

    def create_survey(self, title, features):
        return Survey.objects.create(
            created_by=self.creator,
            organization_scope=self.organization,
            scope_kind='organization',
            title=title,
            features=features,
        )

    def test_json_inventory_flags_title_defaults_and_reports_effective_policy(self):
        survey = self.create_survey('Legacy Quiz', {'points': {'enabled': True}})
        before = dict(survey.features)
        output = StringIO()

        call_command('survey_policy_inventory', stdout=output)

        rows = json.loads(output.getvalue())
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['surveyUuid'], str(survey.access_uuid))
        self.assertEqual(row['scopeId'], f'org-{self.organization.id}')
        self.assertTrue(row['needsReview'])
        self.assertEqual(row['derivedDefaults'], ['attempts.maxAttempts', 'attempts.repeatReward'])
        self.assertIsNone(row['effectivePolicy']['attempts']['maxAttempts'])
        self.assertEqual(row['effectivePolicy']['attempts']['repeatReward'], 'score_delta')
        survey.refresh_from_db()
        self.assertEqual(survey.features, before)

    def test_json_inventory_reports_aliases_and_unsupported_score_rules(self):
        survey = self.create_survey('Research form', {
            'access_policy': {'requires_account': True},
            'minScorePercent': 75,
        })
        output = StringIO()

        call_command('survey_policy_inventory', stdout=output)

        row = json.loads(output.getvalue())[0]
        self.assertEqual(row['surveyUuid'], str(survey.access_uuid))
        self.assertIn('access_policy', row['legacyAliases'])
        self.assertIn('minimum_score', row['warnings'])
        self.assertTrue(row['needsReview'])
        self.assertTrue(row['effectivePolicy']['participation']['accountRequired'])

    def test_json_inventory_flags_unknown_policy_fields_dropped_by_normalizer(self):
        survey = self.create_survey('Unrecognized rule', {
            'accessPolicy': {'requiresAccount': True, 'memberTier': 'researcher'},
            'prerequisites': {'completedFormIds': [], 'allAnswersMustMatch': True},
            'sequence': {'groupId': 'wave', 'order': 2, 'enforce': True, 'skipIfLate': True},
        })
        output = StringIO()

        call_command('survey_policy_inventory', stdout=output)

        row = json.loads(output.getvalue())[0]
        self.assertEqual(row['surveyUuid'], str(survey.access_uuid))
        self.assertEqual(row['unsupportedFields'], [
            'accessPolicy.memberTier',
            'prerequisites.allAnswersMustMatch',
            'sequence.skipIfLate',
        ])
        self.assertTrue(row['needsReview'])

    def test_json_inventory_flags_malformed_top_level_feature_document(self):
        survey = self.create_survey('Malformed feature document', ['legacy', 'array'])
        output = StringIO()

        call_command('survey_policy_inventory', stdout=output)

        row = json.loads(output.getvalue())[0]
        self.assertEqual(row['surveyUuid'], str(survey.access_uuid))
        self.assertIn('features', row['unsupportedFields'])
        self.assertIn('invalid_legacy_policy', row['warnings'])
        self.assertTrue(row['needsReview'])
        self.assertEqual(row['effectivePolicy']['attempts']['maxAttempts'], 1)

    def test_json_inventory_flags_contradictory_legacy_attempt_fields(self):
        survey = self.create_survey('Contradictory attempts', {
            'accessPolicy': {'allowRedo': False, 'maxAttempts': 3},
        })
        output = StringIO()

        call_command('survey_policy_inventory', stdout=output)

        row = json.loads(output.getvalue())[0]
        self.assertEqual(row['surveyUuid'], str(survey.access_uuid))
        self.assertEqual(row['policyConflicts'], [
            'accessPolicy.allowRedo conflicts with accessPolicy.maxAttempts',
        ])
        self.assertTrue(row['needsReview'])

    def test_csv_inventory_is_parseable_and_contains_no_response_payload(self):
        survey = self.create_survey('Current survey', {'accessPolicy': {'allowRedo': False, 'maxAttempts': 1}})
        output = StringIO()

        call_command('survey_policy_inventory', '--format', 'csv', stdout=output)

        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn(str(survey.access_uuid), lines[1])
        self.assertNotIn('answers', output.getvalue().lower())

    def test_invalid_format_is_rejected_before_inventory_output(self):
        output = StringIO()
        with self.assertRaises(CommandError):
            call_command('survey_policy_inventory', '--format', 'xml', stdout=output)
        self.assertEqual(output.getvalue(), '')
