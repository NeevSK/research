"""Export a read-only inventory of effective survey policy and legacy inputs."""

import csv
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from surveys.models import Survey
from surveys.policy import normalize_legacy_features


CSV_FIELDS = (
    'surveyUuid', 'title', 'scopeKind', 'scopeId', 'active', 'draft',
    'policySource', 'needsReview', 'derivedDefaults', 'legacyAliases',
    'unsupportedFields', 'policyConflicts', 'warnings', 'effectivePolicy',
)

ACCESS_KEYS = {
    'allowRedo', 'allow_redo', 'maxAttempts', 'max_attempts',
    'rewardStrategy', 'reward_strategy', 'requiresAccount', 'requires_account',
    'organizationMembershipRequired', 'organization_membership_required',
    'cooldownMinutes', 'cooldown_minutes',
}
PREREQUISITE_KEYS = {
    'completedFormIds', 'completed_form_ids', 'unlockDelayMs', 'unlock_delay_ms',
    'minScorePercent', 'min_score_percent', 'minimumScore', 'minimum_score',
    'minimumScorePercent', 'minimum_score_percent', 'scoreThreshold', 'score_threshold',
}
PREREQUISITE_ITEM_KEYS = {'surveyId', 'survey_id', 'completion', 'unlockDelayMs'}
SEQUENCE_KEYS = {
    'groupId', 'group_id', 'order', 'sequenceOrder', 'enforce', 'enforceSequence',
}


def _scope_id(survey):
    if survey.scope_kind == 'platform':
        return 'platform'
    if survey.organization_scope_id:
        return f'org-{survey.organization_scope_id}'
    return 'unscoped'


def _inventory_row(survey):
    raw_features = survey.features
    features = raw_features if isinstance(raw_features, dict) else {}
    access = features.get('accessPolicy') or features.get('access_policy') or {}
    access = access if isinstance(access, dict) else {}
    collection = features.get('collection') if isinstance(features.get('collection'), dict) else {}

    policy, warnings = normalize_legacy_features(raw_features, survey.title, survey.is_hmsam)
    quiz_default = 'quiz' in (survey.title or '').lower() and not survey.is_hmsam
    legacy_attempt_toggle = (
        any(key in features for key in ('allow_multiple_submissions', 'allowRedo'))
        or 'allow_multiple_submissions' in collection
    )
    has_explicit_redo = any(key in access for key in ('allowRedo', 'allow_redo'))
    has_explicit_maximum = any(key in access for key in ('maxAttempts', 'max_attempts'))
    has_explicit_reward = any(key in access for key in ('rewardStrategy', 'reward_strategy'))
    derived_defaults = []
    if quiz_default and not has_explicit_redo and not has_explicit_maximum and not legacy_attempt_toggle:
        derived_defaults.append('attempts.maxAttempts')
    if quiz_default and not has_explicit_reward:
        derived_defaults.append('attempts.repeatReward')

    aliases = []
    unsupported_fields = []
    if not isinstance(raw_features, dict):
        unsupported_fields.append('features')
    policy_conflicts = []
    for key in ('accessPolicy', 'access_policy'):
        if key not in features:
            continue
        value = features[key]
        if not isinstance(value, dict):
            unsupported_fields.append(key)
        else:
            unsupported_fields.extend(f'{key}.{field}' for field in set(value) - ACCESS_KEYS)
    if 'accessPolicy' in features and 'access_policy' in features:
        unsupported_fields.append('accessPolicy/access_policy')

    prerequisites = features.get('prerequisites')
    if isinstance(prerequisites, dict):
        unsupported_fields.extend(f'prerequisites.{field}' for field in set(prerequisites) - PREREQUISITE_KEYS)
        items = prerequisites.get('completedFormIds', prerequisites.get('completed_form_ids', []))
        if isinstance(items, list):
            for index, item in enumerate(items):
                if isinstance(item, dict):
                    unsupported_fields.extend(
                        f'prerequisites[{index}].{field}'
                        for field in set(item) - PREREQUISITE_ITEM_KEYS
                    )
    sequence = features.get('sequence')
    if isinstance(sequence, dict):
        unsupported_fields.extend(f'sequence.{field}' for field in set(sequence) - SEQUENCE_KEYS)
    elif 'sequence' in features and sequence is not None:
        unsupported_fields.append('sequence')

    if 'access_policy' in features:
        aliases.append('access_policy')
    for key in ('requires_account', 'allow_redo', 'max_attempts', 'reward_strategy', 'organization_membership_required', 'cooldown_minutes'):
        if key in access:
            aliases.append(f'accessPolicy.{key}')
    for key in ('allow_multiple_submissions', 'allowRedo', 'requiresAccount', 'prerequisiteSurveyIds', 'prerequisite_survey_ids', 'sequence_group', 'sequence_order', 'enforceSequence', 'access_code_required'):
        if key in features:
            aliases.append(key)
    if 'allow_multiple_submissions' in collection:
        aliases.append('collection.allow_multiple_submissions')

    raw_maximum = access.get('maxAttempts', access.get('max_attempts'))
    raw_repeat = access.get('allowRedo', access.get('allow_redo'))
    if raw_repeat is not None and raw_maximum not in (None, ''):
        try:
            maximum = int(raw_maximum)
            derived_repeat = maximum != 1
            if isinstance(raw_maximum, bool) or maximum != float(raw_maximum) or raw_repeat is not derived_repeat:
                policy_conflicts.append('accessPolicy.allowRedo conflicts with accessPolicy.maxAttempts')
        except (TypeError, ValueError, OverflowError):
            pass

    explicit_policy = isinstance(features.get('accessPolicy'), dict) or isinstance(features.get('access_policy'), dict)
    unsupported_fields = sorted(set(unsupported_fields))
    policy_conflicts = sorted(set(policy_conflicts))
    source = 'explicit' if explicit_policy else 'legacy_features' if aliases or unsupported_fields or policy_conflicts else 'defaults'
    return {
        'surveyUuid': str(survey.access_uuid),
        'title': survey.title,
        'scopeKind': survey.scope_kind,
        'scopeId': _scope_id(survey),
        'active': survey.is_active,
        'draft': survey.is_draft,
        'policySource': source,
        'needsReview': bool(derived_defaults or aliases or unsupported_fields or policy_conflicts or warnings),
        'derivedDefaults': derived_defaults,
        'legacyAliases': sorted(set(aliases)),
        'unsupportedFields': unsupported_fields,
        'policyConflicts': policy_conflicts,
        'warnings': sorted(set(warnings)),
        'effectivePolicy': policy,
    }


def _csv_row(row):
    return {
        **row,
        'derivedDefaults': json.dumps(row['derivedDefaults'], separators=(',', ':')),
        'legacyAliases': json.dumps(row['legacyAliases'], separators=(',', ':')),
        'unsupportedFields': json.dumps(row['unsupportedFields'], separators=(',', ':')),
        'policyConflicts': json.dumps(row['policyConflicts'], separators=(',', ':')),
        'warnings': json.dumps(row['warnings'], separators=(',', ':')),
        'effectivePolicy': json.dumps(row['effectivePolicy'], separators=(',', ':'), sort_keys=True),
    }


class Command(BaseCommand):
    help = 'Export effective survey policies and legacy compatibility signals without modifying data.'

    def add_arguments(self, parser):
        parser.add_argument('--format', choices=('json', 'csv'), default='json')
        parser.add_argument('--output', help='Optional output file; stdout is used when omitted.')

    def handle(self, *args, **options):
        surveys = Survey.objects.select_related('organization_scope').order_by('id').iterator(chunk_size=500)
        output_path = options.get('output')
        path = Path(output_path) if output_path else None
        try:
            if path:
                path.parent.mkdir(parents=True, exist_ok=True)
                stream = path.open('w', encoding='utf-8', newline='')
            else:
                stream = self.stdout
            try:
                count = 0
                if options['format'] == 'json':
                    stream.write('[\n')
                    for survey in surveys:
                        row = _inventory_row(survey)
                        if count:
                            stream.write(',\n')
                        rendered = json.dumps(row, ensure_ascii=False, indent=2, sort_keys=True)
                        stream.write('  ' + rendered.replace('\n', '\n  '))
                        count += 1
                    stream.write('\n]\n')
                else:
                    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction='ignore')
                    writer.writeheader()
                    for survey in surveys:
                        writer.writerow(_csv_row(_inventory_row(survey)))
                        count += 1
            finally:
                if path:
                    stream.close()
        except OSError as exc:
            raise CommandError(f'Unable to write policy inventory: {exc}') from exc
        if path:
            self.stdout.write(f'Wrote {count} survey policy records to {path}')
