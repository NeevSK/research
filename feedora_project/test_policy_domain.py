from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from surveys.policy import PolicyValidationError, evaluate_eligibility, normalize_legacy_features, validate_policy


class CanonicalSurveyPolicyTests(SimpleTestCase):
    def test_defaults_are_explicit_and_deny_second_attempt(self):
        policy = validate_policy({})
        self.assertEqual(policy['attempts']['maxAttempts'], 1)
        self.assertFalse(policy['participation']['accountRequired'])

    def test_unknown_score_gate_is_rejected(self):
        with self.assertRaises(PolicyValidationError) as raised:
            validate_policy({'minScorePercent': 75})
        self.assertEqual(raised.exception.code, 'unsupported_policy')

    def test_legacy_score_gate_is_reported_not_enabled(self):
        policy, warnings = normalize_legacy_features({'minScorePercent': 75})
        self.assertEqual(warnings, ['minimum_score'])
        self.assertNotIn('minScorePercent', policy)

    def test_malformed_legacy_attempt_and_reward_values_fail_closed_without_crashing(self):
        hostile_features = (
            ['unexpected', 'top-level', 'array'],
            None,
            {'allowRedo': 'false'},
            {'accessPolicy': {'allowRedo': True, 'maxAttempts': 'not-a-limit'}},
            {'accessPolicy': {'allowRedo': True, 'maxAttempts': 1001}},
            {'accessPolicy': {'rewardStrategy': []}},
            {'accessPolicy': {'cooldownMinutes': -1}},
            {'sequence': {'groupId': 'wave', 'order': 2, 'enforce': 'false'}},
            {'enforceSequence': True},
        )
        for features in hostile_features:
            with self.subTest(features=features):
                policy, warnings = normalize_legacy_features(features)
                self.assertIn('invalid_legacy_policy', warnings)
                self.assertEqual(policy['attempts']['maxAttempts'], 1)
                decision = evaluate_eligibility(policy, {'isAuthenticated': True, 'unsupported': warnings})
                self.assertFalse(decision['eligible'])
                self.assertEqual(decision['reasons'][0]['code'], 'unsupported_policy')

    def test_cooldown_boundary_uses_injected_server_time(self):
        policy = validate_policy({'attempts': {'maxAttempts': None, 'cooldownSeconds': 60}})
        last_completed = datetime(2026, 1, 1, tzinfo=timezone.utc)
        decision = evaluate_eligibility(policy, {
            'attemptsUsed': 1,
            'lastCompletedAt': last_completed,
            'now': last_completed + timedelta(seconds=59),
        })
        self.assertFalse(decision['eligible'])
        self.assertEqual(decision['reasons'][0]['code'], 'cooldown_active')
        boundary = evaluate_eligibility(policy, {
            'attemptsUsed': 1,
            'lastCompletedAt': last_completed,
            'now': last_completed + timedelta(seconds=60),
        })
        self.assertTrue(boundary['eligible'])

    def test_non_boolean_and_unbounded_attempts_do_not_coerce(self):
        with self.assertRaises(PolicyValidationError):
            validate_policy({'participation': {'accountRequired': 'false'}})
        with self.assertRaises(PolicyValidationError):
            validate_policy({'attempts': {'maxAttempts': 1001}})

    def test_non_string_reward_enum_returns_a_bounded_policy_error(self):
        with self.assertRaises(PolicyValidationError) as raised:
            validate_policy({'attempts': {'repeatReward': []}})
        self.assertEqual(raised.exception.field, 'attempts.repeatReward')

    def test_prerequisite_delay_has_an_exact_server_time_boundary(self):
        policy, _ = normalize_legacy_features({
            'prerequisites': {'completedFormIds': ['prior-survey-uuid'], 'unlockDelayMs': 30_000},
        })
        completed = datetime(2026, 1, 1, tzinfo=timezone.utc)
        before = evaluate_eligibility(policy, {
            'completedSurveyIds': {'prior-survey-uuid'},
            'completedAtBySurveyId': {'prior-survey-uuid': completed},
            'now': completed + timedelta(seconds=29),
        })
        self.assertEqual(before['reasons'][0]['code'], 'time_locked')
        boundary = evaluate_eligibility(policy, {
            'completedSurveyIds': {'prior-survey-uuid'},
            'completedAtBySurveyId': {'prior-survey-uuid': completed},
            'now': completed + timedelta(seconds=30),
        })
        self.assertTrue(boundary['eligible'])

    def test_capacity_counts_active_attempts_but_allows_their_resume(self):
        policy = validate_policy({'attempts': {'maxAttempts': 1}})
        blocked = evaluate_eligibility(policy, {'activeReservations': 1, 'attemptsUsed': 0})
        self.assertEqual(blocked['reasons'][0]['code'], 'attempt_limit_reached')
        resumed = evaluate_eligibility(policy, {
            'activeReservations': 1,
            'attemptsUsed': 0,
            'hasActiveAttempt': True,
        })
        self.assertTrue(resumed['eligible'])
