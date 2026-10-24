"""Pure policy normalization and eligibility evaluation for research surveys.

This module deliberately has no Django imports. ORM/request adapters build a
context and persist the resulting decision; clients only render that decision.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any


POLICY_SCHEMA_VERSION = 1
MAX_PREREQUISITES = 25
MAX_SEQUENCE_STEPS = 20
MAX_ATTEMPTS = 1000


class PolicyValidationError(ValueError):
    def __init__(self, field: str, message: str, code: str = 'invalid_policy'):
        self.field = field
        self.code = code
        super().__init__(message)


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise PolicyValidationError(field, 'Must be true or false.')
    return value


def _integer(value: Any, field: str, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        raise PolicyValidationError(field, 'Must be a whole number.')
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise PolicyValidationError(field, 'Must be a whole number.') from None
    if str(parsed) != str(value).strip() and not isinstance(value, int):
        raise PolicyValidationError(field, 'Must be a whole number.')
    if parsed < minimum or maximum is not None and parsed > maximum:
        range_text = f'between {minimum} and {maximum}' if maximum is not None else f'at least {minimum}'
        raise PolicyValidationError(field, f'Must be {range_text}.')
    return parsed


def _normalize_prerequisite_ids(value: Any, field: str = 'prerequisites') -> list[dict[str, Any]]:
    if value in (None, ''):
        return []
    if not isinstance(value, list):
        value = [value]
    if len(value) > MAX_PREREQUISITES:
        raise PolicyValidationError(field, f'Use no more than {MAX_PREREQUISITES} prerequisites.')
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if isinstance(item, dict):
            if set(item) - {'surveyId', 'survey_id', 'completion', 'unlockDelayMs'}:
                raise PolicyValidationError(f'{field}[{index}]', 'Unsupported prerequisite condition.')
            if item.get('completion', True) is not True:
                raise PolicyValidationError(f'{field}[{index}].completion', 'Only completion prerequisites are currently supported.')
            delay_ms = _integer(item.get('unlockDelayMs', 0), f'{field}[{index}].unlockDelayMs', 0, 31_536_000_000)
            survey_id = str(item.get('surveyId', item.get('survey_id')) or '').strip()
        else:
            survey_id = str(item or '').strip()
            delay_ms = 0
        if not survey_id or len(survey_id) > 64:
            raise PolicyValidationError(f'{field}[{index}].surveyId', 'A valid survey UUID is required.')
        if survey_id not in seen:
            normalized.append({'surveyId': survey_id, **({'unlockDelayMs': delay_ms} if delay_ms else {})})
            seen.add(survey_id)
    return normalized


def validate_policy(document: Any) -> dict[str, Any]:
    """Validate and return the canonical schema-v1 policy document."""
    if not isinstance(document, dict):
        raise PolicyValidationError('policy', 'Policy must be an object.')
    allowed = {'schemaVersion', 'participation', 'attempts', 'prerequisites', 'sequence'}
    unknown = set(document) - allowed
    if unknown:
        key = sorted(unknown)[0]
        code = 'unsupported_policy' if key.lower() in {'minscorepercent', 'minimumscore', 'scorethreshold'} else 'invalid_policy'
        raise PolicyValidationError(key, 'This policy field is not supported.', code)
    schema_version = document.get('schemaVersion', POLICY_SCHEMA_VERSION)
    if isinstance(schema_version, bool) or schema_version != POLICY_SCHEMA_VERSION:
        raise PolicyValidationError('schemaVersion', 'Unsupported policy schema version.')

    participation = document.get('participation', {})
    attempts = document.get('attempts', {})
    if not isinstance(participation, dict) or not isinstance(attempts, dict):
        raise PolicyValidationError('policy', 'Participation and attempts must be objects.')
    unknown_participation = set(participation) - {'accountRequired', 'organizationMembershipRequired', 'accessCodeRequired'}
    if unknown_participation:
        key = sorted(unknown_participation)[0]
        raise PolicyValidationError(f'participation.{key}', 'This participation rule is not supported.')
    unknown_attempts = set(attempts) - {'maxAttempts', 'cooldownSeconds', 'repeatReward'}
    if unknown_attempts:
        key = sorted(unknown_attempts)[0]
        raise PolicyValidationError(f'attempts.{key}', 'This attempt rule is not supported.')

    maximum = attempts.get('maxAttempts', 1)
    if maximum is not None:
        maximum = _integer(maximum, 'attempts.maxAttempts', 1, MAX_ATTEMPTS)
    cooldown = _integer(attempts.get('cooldownSeconds', 0), 'attempts.cooldownSeconds', 0, 31_536_000)
    reward = attempts.get('repeatReward', 'first_attempt_only')
    if not isinstance(reward, str) or reward not in {'first_attempt_only', 'score_delta', 'unrestricted', 'blocked'}:
        raise PolicyValidationError('attempts.repeatReward', 'Choose a supported repeat reward strategy.')

    sequence = document.get('sequence')
    normalized_sequence = None
    if sequence is not None:
        if not isinstance(sequence, dict) or set(sequence) - {'groupId', 'order', 'enforce'}:
            raise PolicyValidationError('sequence', 'Sequence must contain groupId, order, and enforce only.')
        group_id = str(sequence.get('groupId') or '').strip()
        if not group_id or len(group_id) > 100:
            raise PolicyValidationError('sequence.groupId', 'A sequence name of 1 to 100 characters is required.')
        order = _integer(sequence.get('order'), 'sequence.order', 1, MAX_SEQUENCE_STEPS)
        enforce = _boolean(sequence.get('enforce', False), 'sequence.enforce')
        normalized_sequence = {'groupId': group_id, 'order': order, 'enforce': enforce}

    return {
        'schemaVersion': POLICY_SCHEMA_VERSION,
        'participation': {
            'accountRequired': _boolean(participation.get('accountRequired', False), 'participation.accountRequired'),
            'organizationMembershipRequired': _boolean(participation.get('organizationMembershipRequired', False), 'participation.organizationMembershipRequired'),
            'accessCodeRequired': _boolean(participation.get('accessCodeRequired', False), 'participation.accessCodeRequired'),
        },
        'attempts': {
            'maxAttempts': maximum,
            'cooldownSeconds': cooldown,
            'repeatReward': reward,
        },
        'prerequisites': _normalize_prerequisite_ids(document.get('prerequisites')),
        'sequence': normalized_sequence,
    }


def normalize_legacy_features(features: Any, title: str = '', is_hmsam: bool = False) -> tuple[dict[str, Any], list[str]]:
    """Map historical features to policy v1 without silently enabling score gates."""
    malformed_legacy_policy = not isinstance(features, dict)
    features = features if isinstance(features, dict) else {}
    access = features.get('accessPolicy') or features.get('access_policy') or {}
    if ('accessPolicy' in features and not isinstance(features.get('accessPolicy'), dict)) or (
        'access_policy' in features and not isinstance(features.get('access_policy'), dict)
    ):
        malformed_legacy_policy = True
    access = access if isinstance(access, dict) else {}
    collection = features.get('collection') if isinstance(features.get('collection'), dict) else {}
    if 'collection' in features and not isinstance(features.get('collection'), dict):
        malformed_legacy_policy = True

    boolean_aliases = (
        (access, ('allowRedo', 'allow_redo', 'requiresAccount', 'requires_account', 'organizationMembershipRequired')),
        (features, ('allow_multiple_submissions', 'allowRedo', 'requiresAccount', 'accessCodeRequired', 'access_code_required', 'enforceSequence')),
        (collection, ('allow_multiple_submissions',)),
    )
    if any(key in container and not isinstance(container[key], bool)
           for container, keys in boolean_aliases for key in keys):
        malformed_legacy_policy = True
    legacy_redo_present = any(key in features for key in ('allow_multiple_submissions', 'allowRedo')) or 'allow_multiple_submissions' in collection
    quiz_default = 'quiz' in title.lower() and not is_hmsam
    redo = access.get('allowRedo', access.get('allow_redo'))
    if redo is None:
        redo = bool(features.get('allow_multiple_submissions', features.get('allowRedo', collection.get('allow_multiple_submissions', False)))) if legacy_redo_present else quiz_default
    maximum = access.get('maxAttempts', access.get('max_attempts'))
    if maximum is not None:
        try:
            if (
                isinstance(maximum, bool)
                or int(maximum) != float(maximum)
                or int(maximum) < 1
                or int(maximum) > MAX_ATTEMPTS
            ):
                malformed_legacy_policy = True
        except (TypeError, ValueError, OverflowError):
            malformed_legacy_policy = True
    if maximum is None and not redo:
        maximum = 1
    elif maximum is None:
        maximum = None
    try:
        maximum = None if maximum is None else max(1, min(int(maximum), MAX_ATTEMPTS))
    except (TypeError, ValueError, OverflowError):
        maximum = 1 if not redo else None

    repeat_reward = access.get('rewardStrategy', access.get('reward_strategy'))
    supported_rewards = {'first_attempt_only', 'score_delta', 'unrestricted', 'blocked'}
    if repeat_reward is not None and (not isinstance(repeat_reward, str) or repeat_reward not in supported_rewards):
        malformed_legacy_policy = True
    if not isinstance(repeat_reward, str) or repeat_reward not in supported_rewards:
        repeat_reward = 'score_delta' if quiz_default else 'first_attempt_only'
    raw_cooldown = access.get('cooldownMinutes', access.get('cooldown_minutes', features.get('cooldownMinutes', 0)))
    try:
        cooldown_number = float(raw_cooldown)
        if isinstance(raw_cooldown, bool) or not math.isfinite(cooldown_number) or cooldown_number < 0:
            malformed_legacy_policy = True
    except (TypeError, ValueError, OverflowError):
        malformed_legacy_policy = True
    try:
        cooldown = max(0, min(round(float(raw_cooldown) * 60), 31_536_000))
    except (TypeError, ValueError, OverflowError):
        cooldown = 0

    prerequisites = features.get('prerequisites')
    if prerequisites is not None and not isinstance(prerequisites, (dict, list)):
        malformed_legacy_policy = True
    if isinstance(prerequisites, dict):
        prereq_ids = prerequisites.get('completedFormIds', prerequisites.get('completed_form_ids', []))
        raw_delay = prerequisites.get('unlockDelayMs', prerequisites.get('unlock_delay_ms', features.get('unlockDelayMs', 0)))
    else:
        prereq_ids = prerequisites or features.get('prerequisiteSurveyIds') or features.get('prerequisite_survey_ids') or []
        raw_delay = features.get('unlockDelayMs', 0)
    if not isinstance(prereq_ids, list):
        prereq_ids = [prereq_ids] if prereq_ids else []
    if len(prereq_ids) > MAX_PREREQUISITES:
        malformed_legacy_policy = True
    delay_candidates = [raw_delay]
    delay_candidates.extend(
        item.get('unlockDelayMs', raw_delay) for item in prereq_ids if isinstance(item, dict)
    )
    for delay in delay_candidates:
        if isinstance(delay, bool):
            malformed_legacy_policy = True
            continue
        try:
            if int(delay) != float(delay) or not 0 <= int(delay) <= 31_536_000_000:
                malformed_legacy_policy = True
        except (TypeError, ValueError, OverflowError):
            malformed_legacy_policy = True
    try:
        legacy_unlock_delay_ms = max(0, min(int(raw_delay or 0), 31_536_000_000))
    except (TypeError, ValueError, OverflowError):
        legacy_unlock_delay_ms = 0
    prereq_ids = [
        ({**item, 'unlockDelayMs': item.get('unlockDelayMs', legacy_unlock_delay_ms)} if isinstance(item, dict) else {'surveyId': item, 'unlockDelayMs': legacy_unlock_delay_ms})
        for item in prereq_ids
    ]

    sequence_value = features.get('sequence')
    if sequence_value is not None and not isinstance(sequence_value, dict):
        malformed_legacy_policy = True
    sequence = sequence_value if isinstance(sequence_value, dict) else {}
    if set(sequence) - {'groupId', 'group_id', 'order', 'sequenceOrder', 'enforce', 'enforceSequence'}:
        malformed_legacy_policy = True
    group_id = sequence.get('groupId', sequence.get('group_id', features.get('sequenceGroup', features.get('sequence_group'))))
    order = sequence.get('order', sequence.get('sequenceOrder', features.get('sequenceOrder', features.get('sequence_order'))))
    enforce_value = sequence.get('enforce', sequence.get('enforceSequence', features.get('enforceSequence', False)))
    sequence_declared = sequence_value is not None or any(
        key in features for key in ('sequenceGroup', 'sequence_group', 'sequenceOrder', 'sequence_order', 'enforceSequence')
    )
    if sequence_declared and (not group_id or order is None):
        malformed_legacy_policy = True
    if sequence_declared and (
        not isinstance(group_id, str) or not group_id.strip() or not isinstance(enforce_value, bool)
    ):
        malformed_legacy_policy = True
    if sequence_declared and order is not None:
        try:
            if isinstance(order, bool) or int(order) != float(order) or not 1 <= int(order) <= MAX_SEQUENCE_STEPS:
                malformed_legacy_policy = True
        except (TypeError, ValueError, OverflowError):
            malformed_legacy_policy = True
    try:
        order = int(order) if order is not None else None
    except (TypeError, ValueError, OverflowError):
        order = None
    normalized_sequence = {'groupId': str(group_id), 'order': order, 'enforce': enforce_value} if group_id and order else None

    unsupported = []
    prereq_config = features.get('prerequisites') if isinstance(features.get('prerequisites'), dict) else {}
    score_keys = ('minScorePercent', 'min_score_percent', 'minimumScore', 'minimum_score', 'minimumScorePercent', 'minimum_score_percent', 'scoreThreshold', 'score_threshold')
    if any(key in prereq_config or key in features for key in score_keys):
        unsupported.append('minimum_score')
    if malformed_legacy_policy:
        unsupported.append('invalid_legacy_policy')
        access_code_required = features.get('accessCodeRequired', features.get('access_code_required', False))
        safe = {
            'schemaVersion': POLICY_SCHEMA_VERSION,
            'participation': {
                'accountRequired': False,
                'organizationMembershipRequired': False,
                'accessCodeRequired': access_code_required if isinstance(access_code_required, bool) else False,
            },
            'attempts': {'maxAttempts': 1, 'cooldownSeconds': 0, 'repeatReward': 'first_attempt_only'},
            'prerequisites': [],
            'sequence': None,
        }
        return safe, list(dict.fromkeys(unsupported))

    policy = {
        'schemaVersion': POLICY_SCHEMA_VERSION,
        'participation': {
            'accountRequired': bool(access.get('requiresAccount', access.get('requires_account', features.get('requiresAccount', False)))),
            'organizationMembershipRequired': bool(access.get('organizationMembershipRequired', False)),
            'accessCodeRequired': bool(features.get('accessCodeRequired', features.get('access_code_required', False))),
        },
        'attempts': {'maxAttempts': maximum, 'cooldownSeconds': cooldown, 'repeatReward': repeat_reward},
        'prerequisites': prereq_ids,
        'sequence': normalized_sequence,
    }
    try:
        return validate_policy(policy), unsupported
    except PolicyValidationError:
        # Invalid legacy values must remain recognizable and fail closed at the
        # application boundary, rather than crashing catalog serialization.
        unsupported.append('invalid_legacy_policy')
        safe = {
            'schemaVersion': POLICY_SCHEMA_VERSION,
            'participation': {'accountRequired': False, 'organizationMembershipRequired': False, 'accessCodeRequired': bool(policy['participation']['accessCodeRequired'])},
            'attempts': {'maxAttempts': 1, 'cooldownSeconds': 0, 'repeatReward': 'first_attempt_only'},
            'prerequisites': [],
            'sequence': None,
        }
        return safe, list(dict.fromkeys(unsupported))


def evaluate_eligibility(policy: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Evaluate resolved evidence without I/O. `now` is injectable for boundaries."""
    policy = validate_policy(policy)
    reasons: list[dict[str, Any]] = []
    participation = policy['participation']
    attempts = policy['attempts']
    if context.get('unsupported'):
        reasons.append({'code': 'unsupported_policy', 'rules': list(context['unsupported'])})
    if context.get('surveyUnavailable'):
        reasons.append({'code': 'survey_unavailable'})
    if participation['accountRequired'] and not context.get('isAuthenticated', False):
        reasons.append({'code': 'account_required'})
    if participation['organizationMembershipRequired'] and not context.get('hasOrganizationMembership', False):
        reasons.append({'code': 'organization_membership_required', 'scopeId': context.get('scopeId', '')})
    if participation['accessCodeRequired'] and not context.get('hasAccessGrant', False):
        reasons.append({'code': 'access_code_required'})

    attempts_used = max(0, int(context.get('attemptsUsed', 0)))
    active_reservations = max(0, int(context.get('activeReservations', 0)))
    capacity_used = attempts_used + active_reservations
    maximum = attempts['maxAttempts']
    if maximum is not None and capacity_used >= maximum and not context.get('hasActiveAttempt', False):
        reasons.append({'code': 'attempt_limit_reached', 'maximum': maximum})
    latest = context.get('lastCompletedAt')
    cooldown = attempts['cooldownSeconds']
    if latest and cooldown:
        now = context.get('now') or datetime.now(timezone.utc)
        unlocks_at = latest.timestamp() + cooldown
        if now.timestamp() < unlocks_at:
            reasons.append({'code': 'cooldown_active', 'unlocksAt': datetime.fromtimestamp(unlocks_at, timezone.utc).isoformat()})

    completed = set(context.get('completedSurveyIds') or [])
    completed_at = context.get('completedAtBySurveyId') or {}
    prerequisite_titles = context.get('prerequisiteTitlesBySurveyId') or {}
    for prerequisite in policy['prerequisites']:
        if prerequisite['surveyId'] not in completed:
            reason = {'code': 'prerequisite_unmet', 'surveyId': prerequisite['surveyId']}
            title = prerequisite_titles.get(prerequisite['surveyId'])
            if isinstance(title, str) and title.strip():
                reason['title'] = title.strip()
            reasons.append(reason)
        elif prerequisite.get('unlockDelayMs', 0):
            completed_time = completed_at.get(prerequisite['surveyId'])
            if completed_time:
                now = context.get('now') or datetime.now(timezone.utc)
                unlocks_at = completed_time.timestamp() + prerequisite['unlockDelayMs'] / 1000
                if now.timestamp() < unlocks_at:
                    reasons.append({'code': 'time_locked', 'surveyId': prerequisite['surveyId'], 'unlocksAt': datetime.fromtimestamp(unlocks_at, timezone.utc).isoformat()})
    sequence = policy['sequence']
    if sequence and sequence['enforce']:
        missing = [str(value) for value in context.get('missingSequenceSurveyIds', [])]
        if missing:
            reasons.append({'code': 'sequence_locked', 'requiredSurveyIds': missing})
    return {
        'eligible': not reasons,
        'policyVersion': str(context.get('policyVersion', '')),
        'evaluatedAt': (context.get('now') or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
        'reasons': reasons,
    }
