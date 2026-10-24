import json
import hashlib
import hmac
import re
import uuid
import time
import random
import secrets
from functools import wraps
from datetime import date, timedelta
from django.conf import settings
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST, require_GET
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.middleware.csrf import get_token
from django.contrib.auth import authenticate, login, logout, get_user_model
from django.db import transaction, IntegrityError, OperationalError
from django.db.models import Count, F, Max, Q, Sum
from django.utils import timezone
from surveys.models import Survey, Response, Answer, Question, MCQOption, Experiment, Assignment, PlatformSharedAggregate, SurveyAdmissionGrant, SurveyAccessCodeAttempt, SurveyPolicyRevision, SurveyPolicyScopeLock, SurveyAttempt
from accounts.models import Organization, OrganizationMembership, CreatorProfile, RespondentProfile, GuestPrincipal
from surveys.validation import validate_survey_submission
from surveys.points import (
    calculate_question_points,
    calculate_variable_reward,
    evaluate_negative_reinforcement,
)

User = get_user_model()


def _retry_sqlite_locked_read(read_operation, attempts=10):
    """Retry only a read after SQLite contention; never replay a committed mutation."""
    for attempt in range(attempts):
        try:
            return read_operation()
        except OperationalError as exc:
            if 'locked' not in str(exc).lower() or attempt == attempts - 1:
                raise
            time.sleep(0.05 * (1.5 ** attempt) + random.uniform(0.01, 0.06))


def _count_with_sqlite_lock_retry(queryset, attempts=10):
    """Retry transient SQLite table locks during concurrent dev/test submissions."""
    return _retry_sqlite_locked_read(queryset.count, attempts)


def _retry_sqlite_locked_submission(view):
    """Restart the complete idempotent submit path after transient SQLite locks."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        max_attempts = 8
        for attempt in range(max_attempts):
            try:
                return view(request, *args, **kwargs)
            except OperationalError as exc:
                locked = 'locked' in str(exc).lower()
                if not locked:
                    raise
                if attempt == max_attempts - 1:
                    return JsonResponse({
                        'ok': False,
                        'code': 'temporarily_busy',
                        'error': 'The research service is busy. Retry with the same attempt or submission identifier.',
                    }, status=503)
                time.sleep(0.04 * (1.6 ** attempt) + random.uniform(0.01, 0.05))
    return wrapped


def _survey_scope(survey):
    """Resolve scope from server-owned ownership records, never creator JSON."""
    if survey.scope_kind == 'platform':
        return {'kind': 'platform', 'id': 'platform'}
    if survey.organization_scope_id:
        return {'kind': 'organization', 'id': f'org-{survey.organization_scope_id}'}
    # Quarantined legacy records are not available to respondents until scoped.
    return {'kind': 'organization', 'id': 'unscoped'}


def _lock_survey_policy_scope(scope_kind, scope_id):
    """Lock an existing tenant row; only the singleton platform needs a lock row."""
    if scope_kind == 'organization' and str(scope_id).startswith('org-'):
        organization_id = str(scope_id)[4:]
        return Organization.objects.select_for_update().get(pk=organization_id)
    if scope_kind == 'platform' and scope_id == 'platform':
        lock, _ = SurveyPolicyScopeLock.objects.get_or_create(scope_kind='platform', scope_id='platform')
        return SurveyPolicyScopeLock.objects.select_for_update().get(pk=lock.pk)
    raise ValueError('Cannot lock an invalid survey policy scope.')


def _survey_for_reference(value):
    """Resolve a legacy numeric ID or UUID without passing malformed data to the ORM."""
    key = str(value or '').strip()
    if not key:
        return None
    if key.isdigit():
        try:
            numeric_id = int(key)
        except (TypeError, ValueError, OverflowError):
            return None
        if numeric_id > 9_223_372_036_854_775_807:
            return None
        return Survey.objects.filter(pk=numeric_id).first()
    try:
        access_uuid = uuid.UUID(key)
    except (TypeError, ValueError, AttributeError):
        return None
    return Survey.objects.filter(access_uuid=access_uuid).first()


def _surveys_in_scope(survey):
    if survey.scope_review_required:
        return Survey.objects.none()
    if survey.scope_kind == 'platform':
        return Survey.objects.filter(scope_kind='platform', scope_review_required=False)
    if survey.organization_scope_id:
        return Survey.objects.filter(organization_scope_id=survey.organization_scope_id, scope_kind='organization', scope_review_required=False)
    return Survey.objects.none()


def _can_manage_survey(user, survey):
    """Creators retain authorship access; active org owners/admins manage surveys."""
    if not user or not user.is_authenticated:
        return False
    if user.is_staff or user.is_superuser or survey.created_by_id == user.id:
        return True
    return bool(
        survey.scope_kind == 'organization'
        and survey.organization_scope_id
        and OrganizationMembership.objects.filter(
            user=user,
            organization_id=survey.organization_scope_id,
            role__in=OrganizationMembership.SURVEY_MANAGER_ROLES,
            status=OrganizationMembership.ACTIVE,
        ).exists()
    )


def _has_survey_manager_membership(user):
    """Organization permissions are independent of a user's platform persona."""
    return bool(
        user and user.is_authenticated and OrganizationMembership.objects.filter(
            user=user,
            role__in=OrganizationMembership.SURVEY_MANAGER_ROLES,
            status=OrganizationMembership.ACTIVE,
        ).exists()
    )


def _policy_revision_document(survey):
    """Build the exact immutable document whose canonical JSON is its revision identity."""
    raw_features = survey.features
    features = raw_features if isinstance(raw_features, dict) else {}
    from surveys.policy import normalize_legacy_features
    normalized, warnings = normalize_legacy_features(raw_features, survey.title, survey.is_hmsam)
    reward_inputs = {
        key: features[key]
        for key in ('points', 'variableReward', 'negativeReinforcement')
        if key in features
    }
    score_keys = (
        'minScorePercent', 'min_score_percent', 'minimumScore', 'minimum_score',
        'minimumScorePercent', 'minimum_score_percent', 'scoreThreshold', 'score_threshold',
    )
    score_inputs = {key: features[key] for key in score_keys if key in features}
    prerequisites = features.get('prerequisites')
    if isinstance(prerequisites, dict):
        nested_score_inputs = {key: prerequisites[key] for key in score_keys if key in prerequisites}
        if nested_score_inputs:
            score_inputs['prerequisites'] = nested_score_inputs
    return {
        'schemaVersion': 1,
        'policy': normalized,
        'warnings': warnings,
        'rewardInputs': reward_inputs,
        'unsupportedScoreInputs': score_inputs,
    }


def _policy_revision_version(snapshot):
    encoded = json.dumps(snapshot, sort_keys=True, separators=(',', ':'), default=str).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()[:24]


def _survey_policy_version(survey):
    """Stable fingerprint of the exact immutable revision document."""
    return _policy_revision_version(_policy_revision_document(survey))


def _ensure_policy_revision(survey, actor=None):
    document = _policy_revision_document(survey)
    version = _policy_revision_version(document)
    revision, _ = SurveyPolicyRevision.objects.get_or_create(
        survey=survey,
        version=version,
        defaults={
            'document': document,
            'created_by': actor if actor and actor.is_authenticated else None,
        },
    )
    return revision


def _attempts_for_principal(user=None, guest_participant_id=''):
    query = SurveyAttempt.objects.all()
    if user is not None:
        return query.filter(Q(user=user) | Q(guest_principal__claimed_by=user))
    if guest_participant_id:
        return query.filter(user__isnull=True, guest_principal_id=guest_participant_id)
    return query.none()


def _evaluate_survey_eligibility(request, survey, *, now=None, access_grant=None):
    """Resolve tenant-bound evidence once and evaluate with the pure policy core."""
    from surveys.policy import evaluate_eligibility, normalize_legacy_features

    now = now or timezone.now()
    user = request.user if request.user.is_authenticated else None
    guest = None if user else _guest_from_request(request)
    guest_id = str(guest.id) if guest and not guest.claimed_by_id else ''
    policy, warnings = normalize_legacy_features(survey.features, survey.title, survey.is_hmsam)
    version = _survey_policy_version(survey)
    scope = _survey_scope(survey)

    canonical_prerequisites = []
    prerequisite_titles = {}
    for item in policy['prerequisites']:
        key = str(item['surveyId']).strip()
        prerequisite = _survey_for_reference(key)
        if not prerequisite or not prerequisite.is_active or prerequisite.is_draft:
            warnings.append('invalid_prerequisite_configuration')
            canonical_prerequisites.append(item)
            continue
        if _survey_scope(prerequisite) != scope:
            warnings.append('invalid_prerequisite_scope')
            canonical_prerequisites.append(item)
            continue
        prerequisite_uuid = str(prerequisite.access_uuid)
        canonical_prerequisites.append({**item, 'surveyId': prerequisite_uuid})
        prerequisite_titles[prerequisite_uuid] = prerequisite.title
    policy['prerequisites'] = canonical_prerequisites

    principal_responses = _responses_for_principal(user, guest_id)
    survey_responses = principal_responses.filter(survey=survey)
    attempts_used = survey_responses.count()
    latest = survey_responses.order_by('-completed_at').values_list('completed_at', flat=True).first()

    scoped_responses = principal_responses.filter(
        survey__scope_kind=survey.scope_kind,
        survey__organization_scope_id=survey.organization_scope_id,
        survey__scope_review_required=False,
    )
    completed_rows = scoped_responses.values('survey__access_uuid').annotate(latest_completed=Max('completed_at'))
    completed_at = {str(row['survey__access_uuid']): row['latest_completed'] for row in completed_rows}
    completed_ids = set(completed_at)

    active_reservations = _attempts_for_principal(user, guest_id).filter(
        survey=survey,
        status=SurveyAttempt.IN_PROGRESS,
        policy_revision__version=version,
    )
    active_attempt_count = active_reservations.count()

    missing_sequence = []
    sequence = policy.get('sequence')
    if sequence and sequence.get('enforce'):
        for candidate in _surveys_in_scope(survey).filter(is_active=True, is_draft=False).exclude(pk=survey.pk).only('id', 'access_uuid', 'title', 'features', 'is_hmsam'):
            candidate_policy, _ = normalize_legacy_features(candidate.features, candidate.title, candidate.is_hmsam)
            candidate_sequence = candidate_policy.get('sequence')
            if (
                candidate_sequence
                and candidate_sequence['groupId'] == sequence['groupId']
                and candidate_sequence['order'] < sequence['order']
                and str(candidate.access_uuid) not in completed_ids
            ):
                missing_sequence.append(str(candidate.access_uuid))

    has_membership = bool(user and survey.organization_scope_id and OrganizationMembership.objects.filter(
        user=user,
        organization_id=survey.organization_scope_id,
        status=OrganizationMembership.ACTIVE,
    ).exists())
    decision = evaluate_eligibility(policy, {
        'isAuthenticated': bool(user),
        'hasOrganizationMembership': has_membership,
        'hasAccessGrant': _has_survey_admission_grant(request, survey, access_grant),
        'attemptsUsed': attempts_used,
        'activeReservations': active_attempt_count,
        'hasActiveAttempt': active_attempt_count > 0,
        'lastCompletedAt': latest,
        'completedSurveyIds': completed_ids,
        'completedAtBySurveyId': completed_at,
        'prerequisiteTitlesBySurveyId': prerequisite_titles,
        'missingSequenceSurveyIds': missing_sequence,
        'unsupported': warnings,
        'scopeId': scope['id'],
        'policyVersion': version,
        'now': now,
    })
    return decision, policy


def _eligibility_denial(decision):
    if decision['eligible']:
        return None
    reason = decision['reasons'][0]
    code = reason['code']
    status = 401 if code == 'account_required' else 403 if code in ('organization_membership_required', 'access_code_required') else 429 if code in ('cooldown_active', 'time_locked') else 409
    messages = {
        'unsupported_policy': 'This survey has a rule the service cannot safely evaluate.',
        'survey_unavailable': 'This survey is not available.',
        'account_required': 'Sign in to continue with this survey.',
        'organization_membership_required': 'Join this organization to continue.',
        'access_code_required': 'Verify the survey access code to continue.',
        'attempt_limit_reached': 'You have reached the allowed number of attempts for this survey.',
        'cooldown_active': 'This survey is temporarily unavailable after your previous attempt.',
        'prerequisite_unmet': 'Complete the required survey before starting this one.',
        'time_locked': 'This survey unlocks after the waiting period.',
        'sequence_locked': 'Complete the earlier survey in this sequence first.',
    }
    return JsonResponse({'ok': False, 'code': code, 'error': messages.get(code, 'This survey is not available.'), **{key: value for key, value in reason.items() if key != 'code'}, 'policyVersion': decision.get('policyVersion')}, status=status)


def _normalized_survey_policy(survey):
    """Project the canonical policy into the legacy API response shape."""
    from surveys.policy import normalize_legacy_features

    policy, _warnings = normalize_legacy_features(survey.features, survey.title, survey.is_hmsam)
    attempts = policy['attempts']
    participation = policy['participation']
    maximum = attempts['maxAttempts']
    prerequisites = policy['prerequisites']
    sequence = policy['sequence']
    return {
        'access': {
            'allowRedo': maximum is None or maximum > 1,
            'maxAttempts': maximum,
            'rewardStrategy': attempts['repeatReward'],
            'requiresAccount': participation['accountRequired'],
            'organizationMembershipRequired': participation['organizationMembershipRequired'],
            'cooldownMinutes': attempts['cooldownSeconds'] / 60,
        },
        'prerequisites': [item['surveyId'] for item in prerequisites],
        'sequence': {
            'group': sequence['groupId'] if sequence else None,
            'order': sequence['order'] if sequence else None,
            'enforce': sequence['enforce'] if sequence else False,
        },
        'unlockDelayMs': max((item.get('unlockDelayMs', 0) for item in prerequisites), default=0),
    }


def _guest_from_request(request):
    token = request.headers.get('X-Feedora-Guest-Token', '').strip()
    if token:
        digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
        return _retry_sqlite_locked_read(lambda: GuestPrincipal.objects.filter(token_hash=digest).first())
    # Django's signed server-side session is the browser equivalent of the
    # mobile bearer token. It lets the legacy HTML responder share the same
    # durable guest identity and eligibility history without exposing a token.
    session_guest_id = request.session.get('feedora_guest_principal_id') if hasattr(request, 'session') else None
    if session_guest_id:
        try:
            return _retry_sqlite_locked_read(lambda: GuestPrincipal.objects.filter(pk=session_guest_id).first())
        except (TypeError, ValueError):
            return None
    return None


def _requires_access_code(survey):
    features = survey.features if isinstance(survey.features, dict) else {}
    return bool(features.get('accessCodeRequired', features.get('access_code_required', False)))


def _policy_has_unsupported_score_rule(survey):
    features = survey.features if isinstance(survey.features, dict) else {}
    prerequisites = features.get('prerequisites') if isinstance(features.get('prerequisites'), dict) else {}
    keys = ('minScorePercent', 'min_score_percent', 'minimumScore', 'minimum_score', 'minimumScorePercent', 'minimum_score_percent', 'scoreThreshold', 'score_threshold')
    return any(key in features or key in prerequisites for key in keys)


def _sequence_position_conflicts(survey, group_id, order, *, organization_scope_id=None):
    if survey is not None:
        candidates = _surveys_in_scope(survey).filter(is_active=True).exclude(pk=survey.pk).select_for_update()
    else:
        candidates = Survey.objects.filter(is_active=True, scope_review_required=False)
        if organization_scope_id is None:
            candidates = candidates.filter(scope_kind='platform', organization_scope__isnull=True)
        else:
            candidates = candidates.filter(scope_kind='organization', organization_scope_id=organization_scope_id)
        candidates = candidates.select_for_update()
    for candidate in candidates.only('features'):
        features = candidate.features if isinstance(candidate.features, dict) else {}
        sequence = features.get('sequence') if isinstance(features.get('sequence'), dict) else {}
        candidate_group = sequence.get('groupId', sequence.get('group_id', features.get('sequenceGroup', features.get('sequence_group'))))
        candidate_order = sequence.get('order', sequence.get('sequenceOrder', features.get('sequenceOrder', features.get('sequence_order'))))
        try:
            if str(candidate_group or '').strip() == group_id and int(candidate_order) == order:
                return True
        except (TypeError, ValueError, OverflowError):
            continue
    return False


def _admission_principal(request):
    if request.user.is_authenticated:
        return {'user': request.user, 'guest_principal': None}
    guest = _guest_from_request(request)
    if guest and not guest.claimed_by_id:
        return {'user': None, 'guest_principal': guest}
    return None


def _issue_survey_admission_grant(request, survey):
    principal = _admission_principal(request)
    if not principal:
        return None
    raw_token = secrets.token_urlsafe(32)
    SurveyAdmissionGrant.objects.create(
        survey=survey,
        token_hash=hashlib.sha256(raw_token.encode('utf-8')).hexdigest(),
        policy_version=_survey_policy_version(survey),
        expires_at=timezone.now() + timedelta(hours=12),
        **principal,
    )
    return raw_token


def _consume_access_code_attempt(request):
    now = timezone.now()
    SurveyAccessCodeAttempt.objects.filter(updated_at__lt=now - timedelta(hours=24)).delete()
    SurveyAdmissionGrant.objects.filter(expires_at__lt=now - timedelta(days=1)).delete()
    window_started_at = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    remote_address = request.META.get('REMOTE_ADDR', '')
    # A source-wide key prevents random UUIDs or code paths amplifying storage.
    bucket_hash = hmac.new(str(settings.SECRET_KEY).encode('utf-8'), remote_address.encode('utf-8'), hashlib.sha256).hexdigest()
    bucket, created = SurveyAccessCodeAttempt.objects.get_or_create(
        bucket_hash=bucket_hash,
        defaults={'window_started_at': window_started_at, 'attempts': 1},
    )
    if not created:
        if bucket.window_started_at < window_started_at:
            SurveyAccessCodeAttempt.objects.filter(pk=bucket.pk, window_started_at__lt=window_started_at).update(
                window_started_at=window_started_at, attempts=1, updated_at=now,
            )
        else:
            SurveyAccessCodeAttempt.objects.filter(pk=bucket.pk, window_started_at=window_started_at).update(
                attempts=F('attempts') + 1, updated_at=now,
            )
        bucket.refresh_from_db(fields=['window_started_at', 'attempts'])
    return bucket.window_started_at == window_started_at and bucket.attempts <= 10


def _has_survey_admission_grant(request, survey, token=None):
    token = (token or request.headers.get('X-Feedora-Access-Grant', '')).strip()
    if not token and hasattr(request, 'session'):
        session_grants = request.session.get('feedora_survey_admission_grants', {})
        if isinstance(session_grants, dict):
            token = str(session_grants.get(str(survey.access_uuid), '')).strip()
    principal = _admission_principal(request)
    if not token or not principal:
        return False
    digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
    grants = SurveyAdmissionGrant.objects.filter(
        survey=survey, token_hash=digest, expires_at__gt=timezone.now(),
        policy_version=_survey_policy_version(survey),
    )
    if principal['user']:
        return grants.filter(
            Q(user=principal['user'])
            | Q(guest_principal__claimed_by=principal['user'])
        ).exists()
    return grants.filter(guest_principal=principal['guest_principal']).exists()


@require_POST
@csrf_exempt
def api_survey_verify_access_code(request, access_uuid):
    """Exchange a submitted code for a short-lived survey/principal-bound grant."""
    try:
        payload = json.loads(request.body or '{}')
    except (TypeError, ValueError):
        payload = {}
    code = str(payload.get('accessCode') or '').strip().upper()[:32]
    if not _consume_access_code_attempt(request):
        return JsonResponse({'ok': False, 'code': 'access_code_invalid', 'error': 'The access code could not be verified.'}, status=403)
    survey = Survey.objects.filter(access_uuid=access_uuid, is_active=True, scope_review_required=False).first()
    principal = _admission_principal(request)
    # Same response for a missing survey, invalid code, and absent principal.
    if not survey or not _requires_access_code(survey) or not principal or not secrets.compare_digest(code, survey.access_code.upper() if survey else ''):
        return JsonResponse({'ok': False, 'code': 'access_code_invalid', 'error': 'The access code could not be verified.'}, status=403)
    raw_token = _issue_survey_admission_grant(request, survey)
    if raw_token is None:
        return JsonResponse({'ok': False, 'code': 'access_code_invalid', 'error': 'The access code could not be verified.'}, status=403)
    response = JsonResponse({'ok': True, 'accessGrant': raw_token, 'expiresInSeconds': 43200})
    response['Cache-Control'] = 'no-store'
    return response


def _claimed_guest_ids(user):
    if not user or not getattr(user, 'is_authenticated', False):
        return []
    return _retry_sqlite_locked_read(
        lambda: list(GuestPrincipal.objects.filter(claimed_by=user).values_list('id', flat=True)),
    )


def _responses_for_principal(user=None, guest_participant_id=''):
    if user is not None:
        return Response.objects.filter(Q(user=user) | Q(user__isnull=True, guest_participant_id__in=_claimed_guest_ids(user)))
    return Response.objects.filter(user__isnull=True, guest_participant_id=guest_participant_id)


@require_POST
@csrf_exempt
@_retry_sqlite_locked_submission
def api_guest_session(request):
    """Issue a random bearer credential; client-provided participant IDs are not accepted."""
    raw_token = secrets.token_urlsafe(32)
    guest = GuestPrincipal.objects.create(token_hash=hashlib.sha256(raw_token.encode('utf-8')).hexdigest())
    return JsonResponse({'ok': True, 'guestParticipantId': str(guest.id), 'guestToken': raw_token})


def _claim_guest_principal(guest, user):
    """Link a guest identity and consolidate its active survey reservations."""
    with transaction.atomic():
        guest = GuestPrincipal.objects.select_for_update().get(pk=guest.pk)
        if guest.claimed_by_id and guest.claimed_by_id != user.id:
            return None

        # Guest and account reservations can be started independently before a
        # claim. Once linked, they represent one eligibility principal, so keep
        # only one active reservation per survey and move a retained guest
        # reservation onto the account. Otherwise either draft could submit and
        # bypass a one-attempt policy.
        active_attempts = list(
            _attempts_for_principal(user).filter(status=SurveyAttempt.IN_PROGRESS)
            .select_for_update().order_by('survey_id', 'started_at', 'pk')
        )
        guest_attempts = list(
            SurveyAttempt.objects.select_for_update().filter(
                guest_principal=guest,
                status=SurveyAttempt.IN_PROGRESS,
            ).order_by('survey_id', 'started_at', 'pk')
        )
        attempts_by_survey = {}
        for attempt in active_attempts + guest_attempts:
            attempts_by_survey.setdefault(attempt.survey_id, {})[attempt.pk] = attempt
        for survey_attempts in attempts_by_survey.values():
            ordered = sorted(survey_attempts.values(), key=lambda item: (item.started_at, item.pk))
            retained = ordered[0]
            for duplicate in ordered[1:]:
                duplicate.status = SurveyAttempt.STALE
                duplicate.save(update_fields=['status', 'updated_at'])
            if retained.user_id is None:
                retained.user = user
                retained.guest_principal = None
                retained.save(update_fields=['user', 'guest_principal', 'updated_at'])

        guest.claimed_by = user
        guest.claimed_at = guest.claimed_at or timezone.now()
        guest.save(update_fields=['claimed_by', 'claimed_at'])
    return guest


@require_POST
@csrf_exempt
def api_guest_claim(request):
    """Attach all responses for a verified guest credential to the signed-in account."""
    if not request.user.is_authenticated:
        return JsonResponse({'ok': False, 'code': 'authentication_required', 'error': 'Sign in to save this progress.'}, status=401)
    guest = _guest_from_request(request)
    if not guest:
        return JsonResponse({'ok': False, 'code': 'invalid_guest_credential', 'error': 'This guest session could not be verified.'}, status=401)
    if not _claim_guest_principal(guest, request.user):
        return JsonResponse({'ok': False, 'code': 'guest_already_claimed', 'error': 'This guest session belongs to another account.'}, status=409)
    return JsonResponse({'ok': True, 'claimed': True, 'user': _serialize_user(request.user)})


def _scope_total_points(survey, scope, user=None, guest_participant_id=''):
    scoped = Response.objects.filter(scope_kind=scope['kind'], scope_id=scope['id'])
    if user is not None:
        scoped = scoped.filter(Q(user=user) | Q(user__isnull=True, guest_participant_id__in=_claimed_guest_ids(user)))
    else:
        scoped = scoped.filter(user__isnull=True, guest_participant_id=guest_participant_id)
    return scoped.aggregate(total=Sum('points_earned'))['total'] or 0


def _principal_total_points(user=None, guest_participant_id=''):
    return _responses_for_principal(user, guest_participant_id).aggregate(total=Sum('points_earned'))['total'] or 0


def _platform_total_points(user=None, guest_participant_id=''):
    return _retry_sqlite_locked_read(lambda: _responses_for_principal(user, guest_participant_id).filter(
        scope_kind='platform', scope_id='platform',
    ).aggregate(total=Sum('points_earned'))['total'] or 0)


def _record_platform_aggregate_completion(survey, response):
    """Record one opted-in completion count without copying points, answers, or identity."""
    if survey.scope_kind != 'organization':
        return
    organization = survey.organization_scope
    if organization is None or not organization.share_aggregate_stats_to_platform:
        return
    period_start = date(response.completed_at.year, response.completed_at.month, 1)
    aggregate, _ = PlatformSharedAggregate.objects.get_or_create(
        organization=organization,
        survey=survey,
        period_start=period_start,
        defaults={'completion_count': 0},
    )
    PlatformSharedAggregate.objects.filter(pk=aggregate.pk).update(
        completion_count=F('completion_count') + 1,
    )


def _response_receipt(response, survey, principal_id, scope, replayed=False, total_points=None, account_total_points=None, user_data=None, participant_user=None, guest_participant_id=''):
    receipt_scope = {
        'kind': response.scope_kind or scope['kind'],
        'id': response.scope_id or scope['id'],
    }
    return {
        'ok': True,
        'idempotent': replayed,
        'replayed': replayed,
        'responseId': response.id,
        'submissionId': response.submission_id,
        'principalId': principal_id,
        'scopeId': receipt_scope['id'],
        'scopeKind': receipt_scope['kind'],
        'policyVersion': response.policy_version or _survey_policy_version(survey),
        'acceptedAt': response.completed_at.isoformat(),
        'pointsEarned': response.points_earned,
        # Keep the old account-wide field for existing clients. New clients should
        # use scopeTotalPoints for this scope and platformPoints for platform rewards.
        'totalPoints': account_total_points if account_total_points is not None else total_points if total_points is not None else response.points_earned,
        'accountTotalPoints': account_total_points if account_total_points is not None else total_points if total_points is not None else response.points_earned,
        'scopeTotalPoints': total_points if total_points is not None else response.points_earned,
        'platformPoints': _platform_total_points(participant_user, guest_participant_id),
        'surveyUuid': str(survey.access_uuid),
        'user': user_data,
    }


def _serialize_user(user, persona=None):
    """Serialize Django User instance with role, organization, and live score context."""
    from accounts.utils import get_level_info

    resolved_persona = persona or getattr(user, 'role', None)
    if not resolved_persona:
        resolved_persona = 'creator' if (user.is_staff or user.is_superuser) else 'respondent'

    display_name = user.get_full_name() or user.username
    org_data = None
    organizations_data = []
    avatar = '🔬' if resolved_persona == 'creator' else '🎙️'
    total_points = 0
    platform_points = 0
    platform_level_info = None
    points_by_scope = []
    surveys_completed = 0
    level_info = None

    try:
        active_memberships = OrganizationMembership.objects.filter(
            user=user,
            status=OrganizationMembership.ACTIVE,
        ).select_related('organization').order_by('joined_at', 'id')
        organizations_data = [{
            'id': str(membership.organization_id),
            'entityId': f'org-{membership.organization_id}',
            'name': membership.organization.org_name or 'Research organization',
            'type': membership.organization.org_type or 'university',
            'userRole': membership.organization.user_role or '',
            'country': membership.organization.country or '',
            'membershipRole': membership.role,
            'membershipStatus': membership.status,
        } for membership in active_memberships]
        # Keep the old singular field only when it is unambiguous.
        if len(organizations_data) == 1:
            org_data = organizations_data[0]
    except Exception:
        pass

    try:
        if resolved_persona == 'creator' and hasattr(user, 'creator_profile'):
            cp = user.creator_profile
            display_name = cp.display_name or display_name
            avatar = cp.avatar or avatar
        elif hasattr(user, 'respondent_profile'):
            rp = user.respondent_profile
            display_name = rp.display_name or display_name
            avatar = rp.avatar or avatar
            total_points = rp.total_points or 0
    except Exception:
        pass

    try:
        from surveys.models import Response
        user_responses = _responses_for_principal(user)
        surveys_completed = user_responses.count()
        from django.db.models import Sum
        canonical_points = user_responses.aggregate(Sum('points_earned'))['points_earned__sum']
        if canonical_points is not None:
            total_points = canonical_points
        platform_points = user_responses.filter(scope_kind='platform', scope_id='platform').aggregate(
            Sum('points_earned'),
        )['points_earned__sum'] or 0
        points_by_scope = [
            {
                'scopeId': item['scope_id'],
                'scopeKind': item['scope_kind'],
                'points': item['points'] or 0,
                'completedResponses': item['completed_responses'],
                'levelInfo': get_level_info(item['points'] or 0),
            }
            for item in user_responses.values('scope_id', 'scope_kind').annotate(
                points=Sum('points_earned'),
                completed_responses=Count('id'),
            )
        ]
        level_info = get_level_info(total_points)
        platform_level_info = get_level_info(platform_points)
    except Exception:
        pass

    badges = []
    if surveys_completed >= 1:
        badges.append({'id': 'bronze', 'label': 'Bronze', 'icon': '🥉', 'rule': 'First-time respondent', 'earned': True})
    if surveys_completed >= 5:
        badges.append({'id': 'silver', 'label': 'Silver', 'icon': '🥈', 'rule': '5 responses total', 'earned': True})
    if surveys_completed > 5:
        badges.append({'id': 'gold', 'label': 'Gold', 'icon': '🥇', 'rule': 'More than 5 responses', 'earned': True})

    return {
        'id': user.id,
        'username': user.username,
        'email': user.email,
        'persona': resolved_persona,
        'role': getattr(user, 'role', None),
        'isStaff': bool(user.is_staff or user.is_superuser),
        'isSuperuser': bool(user.is_superuser),
        'displayName': display_name,
        'avatar': avatar,
        'totalPoints': total_points,
        'accountTotalPoints': total_points,
        'platformPoints': platform_points,
        'accountLevelInfo': level_info,
        'platformLevelInfo': platform_level_info,
        'scopePoints': points_by_scope,
        'surveysCompleted': surveys_completed,
        'badges': badges,
        'levelInfo': level_info,
        'organization': org_data,
        'organizations': organizations_data,
    }


@csrf_exempt
@require_POST
def api_register(request):
    """JSON/REST registration endpoint creating active user + profile and establishing session."""
    try:
        if request.content_type == 'application/json' and request.body:
            payload = json.loads(request.body)
        else:
            payload = request.POST
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON body'}, status=400)

    email = str(payload.get('email') or '').lower().strip()
    password = str(payload.get('password') or '').strip()
    persona = str(payload.get('persona') or payload.get('role') or 'respondent').strip().lower()
    display_name = str(payload.get('displayName') or payload.get('display_name') or '').strip()

    if not email or '@' not in email:
        return JsonResponse({'ok': False, 'error': 'A valid email address is required.'}, status=400)
    if not password or len(password) < 6:
        return JsonResponse({'ok': False, 'error': 'Password must be at least 6 characters.'}, status=400)

    if User.objects.filter(username__iexact=email).exists() or User.objects.filter(email__iexact=email).exists():
        return JsonResponse({'ok': False, 'error': 'An account with this email already exists.'}, status=409)

    with transaction.atomic():
        user = User.objects.create_user(
            username=email,
            email=email,
            password=password,
            role=persona,
            is_active=True,
        )

        if persona == 'creator':
            first_name = str(payload.get('firstName') or payload.get('first_name') or '').strip()
            last_name = str(payload.get('lastName') or payload.get('last_name') or '').strip()
            user.first_name = first_name
            user.last_name = last_name
            user.save(update_fields=['first_name', 'last_name'])

            country = str(payload.get('country') or 'Sri Lanka').strip()
            bio = str(payload.get('bio') or '').strip()

            CreatorProfile.objects.create(
                user=user,
                display_name=display_name or f"{first_name} {last_name}".strip() or email.split('@')[0],
                bio=bio,
                avatar='🔬',
            )
        else:
            gender = str(payload.get('gender') or 'NS').strip()
            age_group = str(payload.get('ageGroup') or payload.get('age_group') or '18-24').strip()
            avatar = str(payload.get('avatar') or '🎙️').strip()

            RespondentProfile.objects.create(
                user=user,
                display_name=display_name or email.split('@')[0],
                gender=gender,
                age_group=age_group,
                avatar=avatar,
                total_points=0,
            )

    login(request, user, backend='django.contrib.auth.backends.ModelBackend')

    return JsonResponse({
        'ok': True,
        'user': _serialize_user(user, persona=persona),
        'csrfToken': get_token(request),
    }, status=201)


@csrf_exempt
@require_POST
def api_login(request):
    """JSON/REST login endpoint for mobile and web client authentication."""
    try:
        if request.content_type == 'application/json' and request.body:
            payload = json.loads(request.body)
        else:
            payload = request.POST
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON body'}, status=400)

    identity = str(payload.get('identity') or payload.get('email') or payload.get('username') or '').strip()
    password = str(payload.get('password') or '').strip()
    requested_persona = str(payload.get('persona') or payload.get('role') or 'respondent').strip().lower()

    if not identity or not password:
        return JsonResponse({'ok': False, 'error': 'Identity and password are required.'}, status=400)

    # 1. Try authenticating directly with the identity as username
    user = authenticate(request, username=identity, password=password)

    # 2. If not matched, try looking up by email (case-insensitive)
    if not user:
        candidate = User.objects.filter(email__iexact=identity).first()
        if candidate:
            user = authenticate(request, username=candidate.username, password=password)

    if not user:
        return JsonResponse({'ok': False, 'error': 'Invalid credentials.'}, status=401)

    # 3. Role/persona verification with superuser bypass
    is_staff_or_admin = user.is_staff or user.is_superuser
    if requested_persona == 'creator':
        if not (user.role == 'creator' or is_staff_or_admin):
            return JsonResponse({'ok': False, 'error': 'Account does not have creator/researcher permissions.'}, status=403)
    elif requested_persona == 'respondent':
        # Creators and staff can also browse in respondent persona if desired
        pass

    login(request, user, backend='django.contrib.auth.backends.ModelBackend')

    return JsonResponse({
        'ok': True,
        'user': _serialize_user(user, persona=requested_persona),
        'csrfToken': get_token(request),
    })


@require_GET
def api_me(request):
    """Return currently authenticated user profile or unauthenticated status."""
    csrf_token = get_token(request)
    if not request.user.is_authenticated:
        return JsonResponse({
            'ok': True,
            'authenticated': False,
            'user': None,
            'csrfToken': csrf_token,
        })

    return JsonResponse({
        'ok': True,
        'authenticated': True,
        'user': _serialize_user(request.user),
        'csrfToken': csrf_token,
    })


@require_POST
def api_logout(request):
    """Log out current session."""
    logout(request)
    return JsonResponse({'ok': True, 'message': 'Logged out successfully.'})


@require_GET
def api_respondent_history(request):
    """Return account history or history for the verified guest credential."""

    from surveys.models import Response
    from accounts.utils import get_level_info
    from django.db.models import Sum

    user = request.user if request.user.is_authenticated else None
    guest = None if user else _guest_from_request(request)
    if guest and guest.claimed_by_id:
        return JsonResponse({'ok': False, 'code': 'guest_session_claimed', 'error': 'Sign in to view this saved response history.'}, status=401)
    if not user and not guest:
        return JsonResponse({'ok': False, 'code': 'authentication_required', 'error': 'Sign in or provide a guest session.'}, status=401)
    responses_qs = _responses_for_principal(user, str(guest.id) if guest else '').select_related(
        'survey', 'survey__created_by', 'survey__organization_scope',
    ).order_by('-completed_at')
    # Preserve the pre-scope API field as an account-wide compatibility total.
    # Scope-specific balances are returned separately and are the only values
    # suitable for rewards or leaderboards.
    total_points = responses_qs.aggregate(t=Sum('points_earned'))['t'] or 0
    platform_points = responses_qs.filter(scope_kind='platform', scope_id='platform').aggregate(t=Sum('points_earned'))['t'] or 0
    organizations_by_scope = {
        f'org-{organization.id}': organization.org_name or 'Research organization'
        for organization in Organization.objects.all()
    }
    points_by_scope = {}
    completions_by_scope = {}
    history_items = []
    for r in responses_qs:
        survey = r.survey
        entity_id = r.scope_id or _survey_scope(survey)['id']
        scope_kind = r.scope_kind or _survey_scope(survey)['kind']
        org_name = 'Feedora platform' if scope_kind == 'platform' else organizations_by_scope.get(entity_id, 'Independent research')
        points_by_scope[entity_id] = points_by_scope.get(entity_id, 0) + r.points_earned
        completions_by_scope[entity_id] = completions_by_scope.get(entity_id, 0) + 1

        history_items.append({
            'responseId': r.id,
            'submissionId': r.submission_id,
            'surveyTitle': survey.title,
            'surveyUuid': str(survey.access_uuid),
            'isHmsam': survey.is_hmsam,
            'entityId': entity_id,
            'scopeId': entity_id,
            'scopeKind': scope_kind,
            'organization': org_name,
            'pointsEarned': r.points_earned,
            'completedAt': r.completed_at.isoformat(),
        })

    scope_points = [
        {
            'scopeId': scope_id,
            'scopeKind': 'platform' if scope_id == 'platform' else 'organization',
            'organization': 'Feedora platform' if scope_id == 'platform' else organizations_by_scope.get(scope_id, 'Independent research'),
            'points': points,
            'completedResponses': completions_by_scope[scope_id],
            'levelInfo': get_level_info(points),
        }
        for scope_id, points in sorted(points_by_scope.items(), key=lambda item: item[1], reverse=True)
    ]
    sorted_org_points = [item for item in scope_points if item['scopeKind'] == 'organization']

    submissions_count = len(history_items)
    badges = []
    if submissions_count >= 1:
        badges.append({'id': 'bronze', 'label': 'Bronze', 'icon': '🥉', 'rule': 'First-time respondent', 'earned': True})
    if submissions_count >= 5:
        badges.append({'id': 'silver', 'label': 'Silver', 'icon': '🥈', 'rule': '5 responses total', 'earned': True})
    if submissions_count > 5:
        badges.append({'id': 'gold', 'label': 'Gold', 'icon': '🥇', 'rule': 'More than 5 responses', 'earned': True})

    return JsonResponse({
        'ok': True,
        'totalPoints': total_points,
        'accountTotalPoints': total_points,
        'platformPoints': platform_points,
        'levelInfo': get_level_info(total_points),
        'platformLevelInfo': get_level_info(platform_points),
        'submissionsCount': submissions_count,
        'badges': badges,
        'history': history_items,
        'pointsByOrganization': sorted_org_points,
        'scopePoints': scope_points,
    })


@require_GET
def api_entities_list(request):
    """List organizations and institutions hosting surveys on Feedora."""
    from accounts.models import Organization

    orgs = Organization.objects.all()
    entities = []
    owner_membership_ids = set()
    if request.user.is_authenticated:
        owner_membership_ids = set(OrganizationMembership.objects.filter(
            user=request.user,
            role=OrganizationMembership.OWNER,
            status=OrganizationMembership.ACTIVE,
        ).values_list('organization_id', flat=True))

    for org in orgs:
        active_surveys_count = Survey.objects.filter(
            organization_scope=org,
            scope_kind='organization',
            scope_review_required=False,
            is_active=True,
        ).count()
        entities.append({
            'id': org.id,
            'entityId': f"org-{org.id}",
            'scopeKind': 'organization',
            'name': org.org_name or 'Research organization',
            'type': org.org_type or 'university',
            'userRole': org.user_role or '',
            'country': org.country or '',
            'activeSurveysCount': active_surveys_count,
            'canManageAggregateSharing': bool(
                request.user.is_authenticated and
                (request.user.is_staff or request.user.is_superuser or org.id in owner_membership_ids)
            ),
            'aggregateStatsSharingEnabled': org.share_aggregate_stats_to_platform,
        })

    entities.append({
        'id': 'platform',
        'entityId': 'platform',
        'scopeKind': 'platform',
        'name': 'Feedora Platform',
        'type': 'research',
        'userRole': 'Platform community',
        'country': '',
        'activeSurveysCount': Survey.objects.filter(scope_kind='platform', is_active=True).count(),
        'canManageAggregateSharing': False,
        'aggregateStatsSharingEnabled': False,
    })

    return JsonResponse({'ok': True, 'entities': entities})


@require_POST
def api_entity_create(request):
    """Create an organization scope and its first active owner membership."""
    if not request.user.is_authenticated:
        return JsonResponse({'ok': False, 'error': 'Authentication required.'}, status=401)
    if request.user.role != 'creator' and not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({'ok': False, 'error': 'Creator permissions required.'}, status=403)
    try:
        payload = json.loads(request.body or b'{}')
    except (TypeError, ValueError):
        return JsonResponse({'ok': False, 'error': 'Invalid JSON body.'}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({'ok': False, 'error': 'Organization details must be an object.'}, status=400)

    name = str(payload.get('name') or payload.get('orgName') or '').strip()
    org_type = str(payload.get('type') or payload.get('orgType') or 'other').strip()
    if not name or len(name) > 200:
        return JsonResponse({'ok': False, 'error': 'Organization name is required and must be 200 characters or fewer.'}, status=400)
    allowed_types = {key for key, _label in Organization.ORG_TYPES}
    if org_type not in allowed_types:
        return JsonResponse({'ok': False, 'error': 'Choose a supported organization type.'}, status=400)

    with transaction.atomic():
        organization = Organization.objects.create(
            created_by=request.user,
            org_name=name,
            org_type=org_type,
            user_role=str(payload.get('userRole') or '').strip()[:100],
            country=str(payload.get('country') or '').strip()[:100],
        )
        membership = OrganizationMembership.objects.get(
            user=request.user,
            organization=organization,
            role=OrganizationMembership.OWNER,
            status=OrganizationMembership.ACTIVE,
        )

    return JsonResponse({
        'ok': True,
        'entity': {
            'id': organization.id,
            'entityId': f'org-{organization.id}',
            'scopeKind': 'organization',
            'name': organization.org_name,
            'type': organization.org_type,
            'membershipRole': membership.role,
            'membershipStatus': membership.status,
        },
    }, status=201)


@require_POST
def api_entity_platform_sharing(request, entity_id):
    """Let an organization owner change aggregate completion-count sharing preference."""
    if not request.user.is_authenticated:
        return JsonResponse({'ok': False, 'error': 'Authentication required.'}, status=401)
    try:
        payload = json.loads(request.body or b'{}')
    except (TypeError, ValueError):
        return JsonResponse({'ok': False, 'error': 'Invalid JSON body.'}, status=400)
    enabled = payload.get('shareAggregateStats')
    if not isinstance(enabled, bool):
        return JsonResponse({'ok': False, 'error': 'shareAggregateStats must be a boolean.'}, status=400)

    with transaction.atomic():
        organization = Organization.objects.select_for_update().filter(pk=entity_id).first()
        if organization is None:
            return JsonResponse({'ok': False, 'error': 'Organization not found.'}, status=404)
        can_manage = (
            request.user.is_staff or request.user.is_superuser or
            OrganizationMembership.objects.filter(
                organization=organization,
                user=request.user,
                role=OrganizationMembership.OWNER,
                status=OrganizationMembership.ACTIVE,
            ).exists()
        )
        if not can_manage:
            return JsonResponse({'ok': False, 'error': 'Only an organization owner can approve aggregate sharing.'}, status=403)

        organization.share_aggregate_stats_to_platform = enabled
        organization.aggregate_stats_approved_at = timezone.now() if enabled else None
        organization.save(update_fields=['share_aggregate_stats_to_platform', 'aggregate_stats_approved_at'])
        if not enabled:
            PlatformSharedAggregate.objects.filter(organization=organization).delete()

    return JsonResponse({
        'ok': True,
        'shareAggregateStats': organization.share_aggregate_stats_to_platform,
        'approvedAt': organization.aggregate_stats_approved_at.isoformat() if organization.aggregate_stats_approved_at else None,
    })


@require_GET
def api_platform_aggregate_stats(request):
    """Return platform-owned monthly aggregates from organizations that opted in."""
    if not request.user.is_authenticated or not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({'ok': False, 'error': 'Platform administrator access is required.'}, status=403)

    aggregates = PlatformSharedAggregate.objects.filter(
        organization__share_aggregate_stats_to_platform=True,
    ).values('period_start').annotate(
        completion_count=Sum('completion_count'),
        contributing_organizations=Count('organization', distinct=True),
    ).order_by('period_start')
    return JsonResponse({
        'ok': True,
        'metrics': ['monthly_completion_count'],
        'aggregates': [{
            'periodStart': item['period_start'].isoformat(),
            'completedResponses': item['completion_count'] or 0,
            'contributingOrganizations': item['contributing_organizations'],
        } for item in aggregates],
    })


@require_GET
def api_my_entities(request):
    """List active organization memberships without changing organization ownership."""
    if not request.user.is_authenticated:
        return JsonResponse({'ok': False, 'error': 'Authentication required.'}, status=401)

    memberships = OrganizationMembership.objects.filter(
        user=request.user,
        status=OrganizationMembership.ACTIVE,
    ).select_related('organization')
    entities = [{
        'id': membership.organization_id,
        'entityId': f"org-{membership.organization_id}",
        'name': membership.organization.org_name or 'Research organization',
        'type': membership.organization.org_type or 'university',
        'userRole': membership.organization.user_role or '',
        'country': membership.organization.country or '',
        'membershipRole': membership.role,
        'joinedAt': membership.joined_at.isoformat(),
    } for membership in memberships]
    return JsonResponse({'ok': True, 'entities': entities})


@require_GET
def api_entity_surveys(request, entity_id):
    """List active surveys within one explicit organization or platform scope."""
    from accounts.models import Organization

    if str(entity_id) == 'platform':
        org_name = 'Feedora Platform'
        surveys = Survey.objects.filter(
            scope_kind='platform', scope_review_required=False, is_active=True,
        ).order_by('-created_at')
    else:
        raw_org_id = str(entity_id).removeprefix('org-')
        org_obj = Organization.objects.filter(id=raw_org_id).first()
        if not org_obj:
            return JsonResponse({'ok': False, 'error': 'Organization not found.'}, status=404)
        org_name = org_obj.org_name or 'Research organization'
        surveys = Survey.objects.filter(
            organization_scope=org_obj,
            scope_kind='organization',
            scope_review_required=False,
            is_active=True,
        ).order_by('-created_at')
    items = []
    for s in surveys:
        items.append({
            'id': s.id,
            'uuid': str(s.access_uuid),
            'title': s.title,
            'description': s.description or '',
            'isHmsam': s.is_hmsam,
            'questionsCount': s.questions.count(),
        })

    return JsonResponse({
        'ok': True,
        'entity': {
            'id': entity_id,
            'name': org_name,
        },
        'surveys': items,
    })


@csrf_exempt
@require_POST
def api_entity_join(request, entity_id):
    """Create or restore a user's membership without editing the organization."""
    if not request.user.is_authenticated:
        return JsonResponse({'ok': False, 'error': 'Authentication required.'}, status=401)

    target_org = Organization.objects.filter(id=str(entity_id).removeprefix('org-')).first()
    if not target_org:
        return JsonResponse({'ok': False, 'error': 'Organization not found.'}, status=404)

    membership, created = OrganizationMembership.objects.get_or_create(
        user=request.user,
        organization=target_org,
        defaults={
            'role': OrganizationMembership.MEMBER,
            'status': OrganizationMembership.ACTIVE,
        },
    )
    if not created and membership.status != OrganizationMembership.ACTIVE:
        membership.status = OrganizationMembership.ACTIVE
        membership.save(update_fields=['status'])

    return JsonResponse({
        'ok': True,
        'message': f'Successfully joined {target_org.org_name}.',
        'organization': {
            'id': target_org.id,
            'entityId': f"org-{target_org.id}",
            'orgName': target_org.org_name,
            'orgType': target_org.org_type,
            'country': target_org.country,
            'membershipRole': membership.role,
        },
    })



@require_GET
def api_leaderboard(request, entity_id=None):
    """Return live ranked leaderboard of contributors across an entity or globally."""
    from accounts.models import RespondentProfile, Organization
    from accounts.utils import get_level_info
    from django.db.models import Sum

    scope = request.GET.get('scope', 'org')  # 'org' (default) or 'global'
    requested_entity = str(entity_id or request.GET.get('entity_id') or request.GET.get('entityId') or 'org-1').strip()
    if requested_entity == 'platform':
        scope = 'global'
    raw_id = requested_entity.removeprefix('org-').strip()
    if scope not in {'org', 'global'}:
        return JsonResponse({'ok': False, 'error': 'Invalid leaderboard scope.'}, status=400)
    # Derive both membership and points from responses inside the requested scope.
    # RespondentProfile.company and total_points are global display/cache fields and
    # must never be used to infer organization membership or scoped standings.
    scoped_kind = 'organization'
    scoped_entity_id = 'platform'
    if scope == 'org':
        if raw_id.isdigit():
            org_obj = Organization.objects.filter(id=raw_id).first()
            if org_obj:
                scoped_entity_id = f'org-{org_obj.id}'
            else:
                return JsonResponse({'ok': False, 'error': 'Organization not found.'}, status=404)
        else:
            return JsonResponse({'ok': False, 'error': 'Organization not found.'}, status=404)
    resp_filter = {'scope_kind': scoped_kind, 'scope_id': scoped_entity_id}
    if scope == 'global':
        scoped_kind = 'platform'
        resp_filter['scope_kind'] = scoped_kind
        resp_filter['scope_id'] = scoped_entity_id

    scoped_guest_ids = set(Response.objects.filter(user__isnull=True, **resp_filter).values_list('guest_participant_id', flat=True))
    claimed_guest_owners = {
        str(guest_id): owner_id
        for guest_id, owner_id in GuestPrincipal.objects.filter(claimed_by__isnull=False).values_list('id', 'claimed_by_id')
        if str(guest_id) in scoped_guest_ids
    }
    claimed_ids_by_user = {}
    for guest_id, owner_id in claimed_guest_owners.items():
        claimed_ids_by_user.setdefault(owner_id, []).append(str(guest_id))
    user_ids_with_responses = set(Response.objects.filter(user__isnull=False, **resp_filter).values_list('user_id', flat=True))
    user_ids_with_responses.update(claimed_ids_by_user)
    profiles_qs = RespondentProfile.objects.select_related('user').filter(user_id__in=user_ids_with_responses)
    users_with_responses = User.objects.filter(id__in=user_ids_with_responses)
    LEVEL_COLORS = {
        1: '#64748B',
        2: '#0D9488',
        3: '#2563EB',
        4: '#7C3AED',
        5: '#D97706',
    }

    contributors = []
    seen_user_ids = set()

    for p in profiles_qs:
        user = p.user
        seen_user_ids.add(user.id)
        scoped_responses = Response.objects.filter(**resp_filter).filter(Q(user=user) | Q(user__isnull=True, guest_participant_id__in=claimed_ids_by_user.get(user.id, [])))
        responses_count = scoped_responses.count()
        points = scoped_responses.aggregate(Sum('points_earned'))['points_earned__sum'] or 0

        if points > 0 or responses_count > 0:
            level_info = get_level_info(points)
            badges = []
            if responses_count >= 1:
                badges.append('bronze')
            if responses_count >= 5:
                badges.append('silver')
            if responses_count > 5:
                badges.append('gold')

            contributors.append({
                'userId': f"user-{user.id}",
                'displayName': p.display_name or user.get_full_name() or user.username.split('@')[0],
                'avatar': p.avatar or '🎙️',
                'entityId': scoped_entity_id,
                'totalPoints': points,
                'surveysCompleted': responses_count,
                'badges': badges,
                'standing': {
                    'level': level_info['level'],
                    'title': level_info['title'],
                    'points': points,
                    'progress': level_info['progress'],
                    'next_title': level_info['next_title'],
                    'points_needed': level_info['points_needed'],
                    'color': LEVEL_COLORS.get(level_info['level'], '#64748B'),
                },
            })

    # Also capture registered users with responses who don't have a separate profile row yet
    for u in users_with_responses:
        if u.id in seen_user_ids:
            continue
        scoped_responses = Response.objects.filter(**resp_filter).filter(Q(user=u) | Q(user__isnull=True, guest_participant_id__in=claimed_ids_by_user.get(u.id, [])))
        calc_pts = scoped_responses.aggregate(Sum('points_earned'))['points_earned__sum'] or 0
        responses_count = scoped_responses.count()
        if calc_pts > 0 or responses_count > 0:
            level_info = get_level_info(calc_pts)
            badges = []
            if responses_count >= 1:
                badges.append('bronze')
            if responses_count >= 5:
                badges.append('silver')
            if responses_count > 5:
                badges.append('gold')

            contributors.append({
                'userId': f"user-{u.id}",
                'displayName': u.get_full_name() or u.username.split('@')[0],
                'avatar': '🎙️',
                'entityId': scoped_entity_id,
                'totalPoints': calc_pts,
                'surveysCompleted': responses_count,
                'badges': badges,
                'standing': {
                    'level': level_info['level'],
                    'title': level_info['title'],
                    'points': calc_pts,
                    'progress': level_info['progress'],
                    'next_title': level_info['next_title'],
                    'points_needed': level_info['points_needed'],
                    'color': LEVEL_COLORS.get(level_info['level'], '#64748B'),
                },
            })

    contributors.sort(key=lambda c: c['totalPoints'], reverse=True)

    return JsonResponse({
        'ok': True,
        'scopeId': scoped_entity_id,
        'scopeKind': scoped_kind,
        'contributors': contributors,
        'totalContributors': len(contributors),
    })


@require_GET
def api_surveys_list(request):
    """List active surveys available for participation, enriched with archetypes, access policies, and organization scope."""
    from django.db.models import Q
    from surveys.policy import evaluate_eligibility, normalize_legacy_features
    guest = None if request.user.is_authenticated else _guest_from_request(request)
    guest_participant_id = str(guest.id) if guest and not guest.claimed_by_id else ''
    entity_id_param = request.GET.get('entity_id') or request.GET.get('entityId')

    surveys_qs = Survey.objects.filter(
        is_active=True,
        scope_review_required=False,
    ).select_related('created_by', 'organization_scope').annotate(
        _catalog_question_count=Count('questions', distinct=True),
    ).order_by('-created_at')

    if entity_id_param:
        raw_id = str(entity_id_param).strip()
        if raw_id == 'platform':
            surveys_qs = surveys_qs.filter(scope_kind='platform')
        else:
            org_id = raw_id.removeprefix('org-')
            org_obj = Organization.objects.filter(id=org_id).first()
            if org_obj:
                surveys_qs = surveys_qs.filter(organization_scope=org_obj, scope_kind='organization')
            else:
                surveys_qs = surveys_qs.none()

    surveys = list(surveys_qs[:50])
    canonical_catalog_policies = {}
    for survey in surveys:
        canonical_catalog_policies[survey.id] = normalize_legacy_features(survey.features, survey.title, survey.is_hmsam)
    prerequisite_keys = set()
    for survey in surveys:
        survey_features = survey.features if isinstance(survey.features, dict) else {}
        prereq_config = survey_features.get('prerequisites')
        if isinstance(prereq_config, dict):
            raw_ids = prereq_config.get('completedFormIds', prereq_config.get('completed_form_ids', []))
        else:
            raw_ids = prereq_config or survey_features.get('prerequisiteSurveyIds') or survey_features.get('prerequisite_survey_ids') or []
        if not isinstance(raw_ids, list):
            raw_ids = [raw_ids]
        prerequisite_keys.update(str(value).strip() for value in raw_ids if str(value).strip())
    prerequisite_lookup = {}
    uuid_keys = [key for key in prerequisite_keys if not key.isdigit()]
    numeric_keys = [int(key) for key in prerequisite_keys if key.isdigit()]
    # Chunk to stay below SQLite's bind-variable limit even for legacy rows.
    for offset in range(0, max(len(uuid_keys), len(numeric_keys)), 400):
        uuid_chunk = uuid_keys[offset:offset + 400]
        numeric_chunk = numeric_keys[offset:offset + 400]
        candidates = Survey.objects.filter(Q(access_uuid__in=uuid_chunk) | Q(id__in=numeric_chunk)).select_related('organization_scope')
        for target in candidates:
            prerequisite_lookup[str(target.access_uuid)] = target
            prerequisite_lookup[str(target.id)] = target
    response_summary = {}
    completed_by_scope = {}
    completed_at_by_scope = {}
    if request.user.is_authenticated or guest_participant_id:
        completed_rows = _responses_for_principal(
            request.user if request.user.is_authenticated else None,
            guest_participant_id,
        ).values('survey__access_uuid', 'survey__scope_kind', 'survey__organization_scope_id').annotate(latest_completed=Max('completed_at'))
        for row in completed_rows:
            completed_uuid = row['survey__access_uuid']
            completed_scope_kind = row['survey__scope_kind']
            completed_scope_id = f"org-{row['survey__organization_scope_id']}" if row['survey__organization_scope_id'] else 'platform'
            completed_by_scope.setdefault((completed_scope_kind, completed_scope_id), set()).add(str(completed_uuid))
            completed_at_by_scope.setdefault((completed_scope_kind, completed_scope_id), {})[str(completed_uuid)] = row['latest_completed']
        response_summary = {
            row['survey_id']: row
            for row in _responses_for_principal(
                request.user if request.user.is_authenticated else None,
                guest_participant_id,
            ).filter(survey_id__in=[survey.id for survey in surveys]).values('survey_id').annotate(
                attempts_used=Count('id'), latest_completed=Max('completed_at'),
            )
        }
    active_membership_ids = set()
    if request.user.is_authenticated:
        active_membership_ids = set(OrganizationMembership.objects.filter(
            user=request.user,
            status=OrganizationMembership.ACTIVE,
        ).values_list('organization_id', flat=True))
    grant_rows = SurveyAdmissionGrant.objects.filter(
        survey_id__in=[survey.id for survey in surveys], expires_at__gt=timezone.now(),
    )
    if request.user.is_authenticated:
        grant_rows = grant_rows.filter(Q(user=request.user) | Q(guest_principal__claimed_by=request.user))
    elif guest:
        grant_rows = grant_rows.filter(guest_principal=guest)
    else:
        grant_rows = SurveyAdmissionGrant.objects.none()
    active_grants = set(grant_rows.values_list('survey_id', 'policy_version'))
    evaluation_time = timezone.now()
    sequence_requirements = {}
    sequence_query = Q()
    for survey in surveys:
        policy = canonical_catalog_policies[survey.id][0]
        sequence = policy.get('sequence')
        if not sequence or not sequence.get('enforce'):
            continue
        key = (survey.scope_kind, survey.organization_scope_id, sequence['groupId'])
        sequence_requirements[key] = sequence
        scope_query = Q(
            scope_kind=survey.scope_kind,
            organization_scope_id=survey.organization_scope_id,
            scope_review_required=False,
            is_active=True,
            is_draft=False,
        )
        group_query = (
            Q(features__sequence__groupId=sequence['groupId'])
            | Q(features__sequence__group_id=sequence['groupId'])
            | Q(features__sequenceGroup=sequence['groupId'])
            | Q(features__sequence_group=sequence['groupId'])
        )
        sequence_query |= scope_query & group_query

    sequence_candidates_by_key = {}
    if sequence_requirements:
        candidates = Survey.objects.filter(sequence_query).only(
            'id', 'access_uuid', 'scope_kind', 'organization_scope_id', 'title', 'is_hmsam', 'features',
        )
        for candidate in candidates.iterator(chunk_size=200):
            candidate_policy, _ = normalize_legacy_features(
                candidate.features,
                candidate.title,
                candidate.is_hmsam,
            )
            candidate_sequence = candidate_policy.get('sequence')
            if not candidate_sequence:
                continue
            key = (candidate.scope_kind, candidate.organization_scope_id, candidate_sequence['groupId'])
            if key in sequence_requirements:
                sequence_candidates_by_key.setdefault(key, []).append((
                    candidate_sequence['order'], str(candidate.access_uuid),
                ))

    surveys_by_id = {str(survey.id): survey for survey in surveys}
    surveys_by_uuid = {str(survey.access_uuid): survey for survey in surveys}
    items = []
    for s in surveys:
        # Determine archetype kind
        kind = 'survey'
        if s.is_hmsam:
            kind = 'survey'
        elif 'quiz' in (s.title or '').lower():
            kind = 'quiz'
        elif 'exam' in (s.title or '').lower():
            kind = 'exam'
        elif 'intake' in (s.title or '').lower():
            kind = 'intake'

        features = s.features if isinstance(s.features, dict) else {}
        resolved_policy = _normalized_survey_policy(s)
        access_policy = resolved_policy['access']
        raw_prerequisites = resolved_policy['prerequisites']
        prerequisite_ids = []
        prerequisite_titles = []
        for prerequisite in raw_prerequisites:
            prereq_key = str(prerequisite).strip()
            target = prerequisite_lookup.get(prereq_key) or surveys_by_uuid.get(prereq_key) or surveys_by_id.get(prereq_key)
            # Never reveal a prerequisite's title across organization/platform scope.
            if target and _survey_scope(target) != _survey_scope(s):
                target = None
            if target and target.id == s.id:
                continue
            prerequisite_ids.append(str(target.access_uuid) if target else prereq_key)
            if target:
                prerequisite_titles.append(target.title)
        prerequisite_ids = list(dict.fromkeys(prerequisite_ids))
        prerequisite_titles = list(dict.fromkeys(prerequisite_titles))
        prerequisites_config = features.get('prerequisites') if isinstance(features.get('prerequisites'), dict) else {}
        if not isinstance(prerequisites_config, dict):
            prerequisites_config = {}
        unlock_delay_ms = prerequisites_config.get('unlockDelayMs', prerequisites_config.get('unlock_delay_ms', features.get('unlockDelayMs')))
        min_score_percent = prerequisites_config.get('minScorePercent', prerequisites_config.get('min_score_percent', features.get('minScorePercent')))
        try:
            unlock_delay_ms = max(0, int(unlock_delay_ms)) if unlock_delay_ms is not None else None
        except (TypeError, ValueError):
            unlock_delay_ms = None
        try:
            min_score_percent = min(100, max(0, float(min_score_percent))) if min_score_percent is not None else None
        except (TypeError, ValueError):
            min_score_percent = None
        sequence_order = features.get('sequenceOrder', features.get('sequence_order'))
        sequence_config = features.get('sequence') if isinstance(features.get('sequence'), dict) else {}
        if not isinstance(sequence_config, dict):
            sequence_config = {}
        sequence_group = sequence_config.get('groupId', sequence_config.get('group_id', features.get('sequenceGroup', features.get('sequence_group'))))
        sequence_order = sequence_config.get('order', sequence_config.get('sequenceOrder', sequence_order))
        enforce_sequence = bool(sequence_config.get('enforce', sequence_config.get('enforceSequence', features.get('enforceSequence', False))))
        try:
            sequence_order = int(sequence_order) if sequence_order is not None else None
        except (TypeError, ValueError):
            sequence_order = None

        summary = response_summary.get(s.id, {})
        attempts_used = summary.get('attempts_used', 0)
        latest_completed = summary.get('latest_completed')
        survey_scope = _survey_scope(s)
        scope_key = (s.scope_kind, f'org-{s.organization_scope_id}' if s.organization_scope_id else 'platform')
        canonical_policy, policy_warnings = canonical_catalog_policies[s.id]
        catalog_prerequisites = []
        prerequisite_titles_by_id = {}
        for prerequisite in canonical_policy['prerequisites']:
            key = str(prerequisite['surveyId']).strip()
            target = prerequisite_lookup.get(key)
            if target is None and key.isdigit():
                target = surveys_by_id.get(key)
            if not target or not target.is_active or target.is_draft:
                policy_warnings.append('invalid_prerequisite_configuration')
                catalog_prerequisites.append(prerequisite)
                continue
            if _survey_scope(target) != survey_scope:
                policy_warnings.append('invalid_prerequisite_scope')
                catalog_prerequisites.append(prerequisite)
                continue
            prerequisite_uuid = str(target.access_uuid)
            catalog_prerequisites.append({**prerequisite, 'surveyId': prerequisite_uuid})
            prerequisite_titles_by_id[prerequisite_uuid] = target.title
        canonical_policy['prerequisites'] = catalog_prerequisites
        sequence_missing = []
        canonical_sequence = canonical_policy.get('sequence')
        if canonical_sequence and canonical_sequence.get('enforce'):
            key = (s.scope_kind, s.organization_scope_id, canonical_sequence['groupId'])
            completed_in_scope = completed_by_scope.get(scope_key, set())
            sequence_missing = [
                survey_id
                for order, survey_id in sequence_candidates_by_key.get(key, ())
                if order < canonical_sequence['order'] and survey_id not in completed_in_scope
            ]
        has_grant = (s.id, _survey_policy_version(s)) in active_grants
        eligibility = evaluate_eligibility(canonical_policy, {
            'isAuthenticated': request.user.is_authenticated,
            'hasOrganizationMembership': s.organization_scope_id in active_membership_ids,
            'hasAccessGrant': has_grant,
            'attemptsUsed': attempts_used,
            'lastCompletedAt': latest_completed,
            'completedSurveyIds': completed_by_scope.get(scope_key, set()),
            'completedAtBySurveyId': completed_at_by_scope.get(scope_key, {}),
            'prerequisiteTitlesBySurveyId': prerequisite_titles_by_id,
            'missingSequenceSurveyIds': sequence_missing,
            'unsupported': policy_warnings,
            'scopeId': survey_scope['id'],
            'policyVersion': _survey_policy_version(s),
            'now': evaluation_time,
        })

        if survey_scope['kind'] == 'platform':
            org_name = 'Feedora platform'
        else:
            organization = s.organization_scope
            org_name = (organization.org_name if organization else '') or 'Research organization'

        items.append({
            'id': s.id,
            'uuid': str(s.access_uuid),
            'title': s.title,
            'description': s.description or '',
            'isHmsam': s.is_hmsam,
            'isGlobal': survey_scope['kind'] == 'platform',
            'organizationId': survey_scope['id'],
            'scopeId': survey_scope['id'],
            'scopeKind': survey_scope['kind'],
            'policyVersion': _survey_policy_version(s),
            'organizationName': org_name,
            'kind': kind,
            'accessPolicy': access_policy,
            'requiresAccount': access_policy['requiresAccount'],
            'attemptsUsed': attempts_used,
            'lastCompletedAt': latest_completed.isoformat() if latest_completed else None,
            'prerequisites': prerequisite_ids,
            'prerequisiteTitles': prerequisite_titles,
            'unlockDelayMs': unlock_delay_ms,
            # A client-side threshold without trusted server scoring is not policy.
            'policyWarnings': ['minimum_score_unsupported'] if min_score_percent is not None else [],
            'accessCodeRequired': bool(features.get('accessCodeRequired', features.get('access_code_required', False))),
            'eligibility': eligibility,
            'sequenceOrder': sequence_order,
            'sequenceGroup': str(sequence_group) if sequence_group else None,
            'enforceSequence': enforce_sequence,
            'questionsCount': s._catalog_question_count,
            'createdAt': s.created_at.isoformat() if s.created_at else None,
        })
    return JsonResponse({'ok': True, 'surveys': items})


def assign_participant_arm(survey, participant_id, requested_arm=None):
    """
    Deterministic Salted Experimental Arm Assignment Engine.
    Uses salted cryptographic hashing: h = sha256(salt + participant_id) % total_weight.
    Persists assignment to Assignment model for longitudinal participant consistency.
    Supports researcher overrides via requested_arm for testing.
    """
    import hashlib
    experiment, _ = Experiment.objects.get_or_create(
        survey=survey,
        key='hmsam-gamification-study',
        defaults={
            'salt': 'feedora_exp_salt_v1',
            'arms': [
                {'name': 'control', 'weight': 50},
                {'name': 'gamified', 'weight': 50},
            ],
            'active': True,
        }
    )

    if requested_arm in ('gamified', 'control', 'variable', 'fixed'):
        assignment, _ = Assignment.objects.get_or_create(
            experiment=experiment,
            participant_id=participant_id,
            defaults={'arm': requested_arm}
        )
        if assignment.arm != requested_arm:
            assignment.arm = requested_arm
            assignment.save(update_fields=['arm'])
        return assignment.arm, experiment

    existing = Assignment.objects.filter(experiment=experiment, participant_id=participant_id).first()
    if existing:
        return existing.arm, experiment

    arms = experiment.arms or [
        {'name': 'control', 'weight': 50},
        {'name': 'gamified', 'weight': 50},
    ]

    total_weight = sum(a.get('weight', 0) for a in arms) or 100
    salt = experiment.salt or 'feedora_exp_salt_v1'
    digest = hashlib.sha256(f"{salt}:{participant_id}".encode('utf-8')).hexdigest()
    slot = int(digest[:8], 16) % total_weight

    cumulative = 0
    assigned_arm = 'gamified'
    for a in arms:
        cumulative += a.get('weight', 0)
        if slot < cumulative:
            assigned_arm = a.get('name', 'gamified')
            break

    assignment = Assignment.objects.create(
        experiment=experiment,
        participant_id=participant_id,
        arm=assigned_arm
    )
    return assignment.arm, experiment


def _serialize_survey_meta(survey, request=None):
    questions = []
    for q in survey.questions.all().order_by('order'):
        questions.append({
            'id': q.id,
            'text': q.text,
            'code': q.code,
            'type': q.question_type,
            'construct': q.hmsam_construct or '',
            'required': q.is_required,
            'options': [{'id': o.id, 'text': o.text, 'order': o.order} for o in q.options.all().order_by('order')],
            'ratingMax': q.rating_max,
            'ratingStyle': q.rating_style,
            'sectionTitle': q.section_title,
            'sectionNum': q.section_num,
        })

    csrf_token = get_token(request) if request else None

    # Deterministic or requested experimental arm assignment via salted hashing
    participant_id = None
    req_arm = None
    if request:
        req_arm = request.GET.get('variant') or request.GET.get('arm')
        participant_id = (
            request.GET.get('participant_id') or
            request.GET.get('participantId') or
            request.headers.get('X-Participant-ID')
        )
        if not participant_id and request.user.is_authenticated:
            participant_id = request.user.email or str(request.user.id)
        if not participant_id:
            if hasattr(request, 'session') and request.session.session_key:
                participant_id = f"sess_{request.session.session_key}"
            else:
                participant_id = f"client_{request.META.get('REMOTE_ADDR', 'anon')}"
    else:
        participant_id = 'default_participant'

    variant, experiment_obj = assign_participant_arm(survey, participant_id, requested_arm=req_arm)

    resolved_features = json.loads(json.dumps(survey.features or {}))
    if not isinstance(resolved_features, dict):
        resolved_features = {}
    resolved_policy = _normalized_survey_policy(survey)
    canonical_policy = resolved_policy['access']
    resolved_features['accessPolicy'] = canonical_policy

    prerequisite_config = resolved_features.get('prerequisites')
    raw_prerequisites = resolved_policy['prerequisites']
    prerequisite_uuids = []
    prerequisite_titles = []
    for prerequisite_id in raw_prerequisites:
        key = str(prerequisite_id).strip()
        prerequisite = _survey_for_reference(key)
        if (
            prerequisite
            and prerequisite.id != survey.id
            and prerequisite.is_active
            and not prerequisite.is_draft
            and not prerequisite.scope_review_required
            and _survey_scope(prerequisite) == _survey_scope(survey)
        ):
            prerequisite_uuids.append(str(prerequisite.access_uuid))
            prerequisite_titles.append(prerequisite.title)

    sequence_policy = resolved_policy['sequence']
    sequence_group = sequence_policy['group']
    sequence_order = sequence_policy['order']
    enforce_sequence = sequence_policy['enforce']
    sequence_prerequisite_uuids = []
    sequence_prerequisite_titles = []
    try:
        current_sequence_order = int(sequence_order) if sequence_order is not None else None
    except (TypeError, ValueError):
        current_sequence_order = None
    if enforce_sequence and sequence_group and current_sequence_order is not None:
        for candidate in _surveys_in_scope(survey).filter(is_active=True, is_draft=False).exclude(id=survey.id):
            candidate_features = candidate.features if isinstance(candidate.features, dict) else {}
            candidate_sequence = candidate_features.get('sequence') if isinstance(candidate_features.get('sequence'), dict) else {}
            candidate_group = candidate_sequence.get('groupId', candidate_sequence.get('group_id', candidate_features.get('sequenceGroup', candidate_features.get('sequence_group'))))
            candidate_order = candidate_sequence.get('order', candidate_sequence.get('sequenceOrder', candidate_features.get('sequenceOrder', candidate_features.get('sequence_order'))))
            try:
                if candidate_group == sequence_group and candidate_order is not None and int(candidate_order) < current_sequence_order:
                    sequence_prerequisite_uuids.append(str(candidate.access_uuid))
                    sequence_prerequisite_titles.append(candidate.title)
            except (TypeError, ValueError):
                continue
    resolved_features['prerequisiteSurveyUuids'] = list(dict.fromkeys(prerequisite_uuids + sequence_prerequisite_uuids))
    resolved_features['prerequisiteSurveyTitles'] = list(dict.fromkeys(prerequisite_titles + sequence_prerequisite_titles))
    resolved_features['sequenceGroup'] = str(sequence_group) if sequence_group else None
    resolved_features['sequenceOrder'] = current_sequence_order
    resolved_features['enforceSequence'] = enforce_sequence
    if variant == 'control':
        for feat_key in ['points', 'mascot', 'badges', 'levels', 'leaderboard', 'rewards', 'negative_reinforcement']:
            if feat_key in resolved_features and isinstance(resolved_features[feat_key], dict):
                resolved_features[feat_key]['enabled'] = False
            else:
                resolved_features[feat_key] = {'enabled': False}
    elif variant == 'variable':
        if 'rewards' not in resolved_features or not isinstance(resolved_features['rewards'], dict):
            resolved_features['rewards'] = {}
        resolved_features['rewards']['enabled'] = True
        resolved_features['rewards']['mode'] = 'variable'

    survey_scope = _survey_scope(survey)
    policy_version = _survey_policy_version(survey)

    return {
        'ok': True,
        'csrfToken': csrf_token,
        'experiment': {
            'experimentKey': experiment_obj.key if experiment_obj else 'hmsam-gamification-study',
            'variant': variant,
            'arm': variant,
            'participantId': participant_id,
        },
        'survey': {
            'id': survey.id,
            'uuid': str(survey.access_uuid),
            'scopeId': survey_scope['id'],
            'scopeKind': survey_scope['kind'],
            'policyVersion': policy_version,
            'title': survey.title,
            'description': survey.description,
            'isHmsam': survey.is_hmsam,
            'themeAccent': survey.theme_accent,
            'themeBg': survey.theme_bg,
            'submitLabel': survey.submit_label,
            'btnColor': survey.btn_color,
            'btnRadius': survey.btn_radius,
            'qFont': survey.q_font,
            'headerFont': survey.header_font,
        },
        'features': resolved_features,
        'questions': questions,
    }


@require_GET
def api_survey_meta(request, access_uuid):
    """Fetch survey metadata and ordered questions with choice options by UUID."""
    try:
        survey = _retry_sqlite_locked_read(
            lambda: Survey.objects.get(access_uuid=access_uuid, is_active=True, scope_review_required=False),
        )
    except Survey.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'Survey not found'}, status=404)

    can_manage = request.user.is_authenticated and _can_manage_survey(request.user, survey)
    if not can_manage:
        if _policy_has_unsupported_score_rule(survey):
            return JsonResponse({'ok': False, 'code': 'unsupported_policy', 'error': 'This survey has a rule the service cannot safely evaluate.'}, status=409)
        if _requires_access_code(survey) and not _has_survey_admission_grant(request, survey):
            code = 'access_grant_invalid' if request.headers.get('X-Feedora-Access-Grant') else 'access_code_required'
            response = JsonResponse({'ok': False, 'code': code, 'error': 'Verify the survey access code to continue.'}, status=403)
            response['Cache-Control'] = 'private, no-store'
            return response
        decision, _ = _evaluate_survey_eligibility(request, survey)
        denial = _eligibility_denial(decision)
        if denial:
            response = denial
            response['Cache-Control'] = 'private, no-store'
            return response

    response = JsonResponse(_serialize_survey_meta(survey, request=request))
    response['Cache-Control'] = 'private, no-store'
    return response


@require_POST
@_retry_sqlite_locked_submission
def api_survey_attempt_start(request, access_uuid):
    """Reserve one eligible attempt and return its durable resumable identity."""
    try:
        payload = json.loads(request.body or b'{}')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'ok': False, 'code': 'invalid_json', 'error': 'Invalid JSON body.'}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({'ok': False, 'code': 'invalid_json', 'error': 'The request body must be a JSON object.'}, status=400)
    try:
        requested_id = uuid.UUID(str(payload.get('attemptId') or payload.get('attempt_id') or ''))
    except (ValueError, TypeError, AttributeError):
        return JsonResponse({'ok': False, 'code': 'invalid_attempt_id', 'error': 'A valid attempt identifier is required.'}, status=400)
    try:
        survey = Survey.objects.get(access_uuid=access_uuid, is_active=True, scope_review_required=False)
    except Survey.DoesNotExist:
        return JsonResponse({'ok': False, 'code': 'survey_unavailable', 'error': 'This survey is not available.'}, status=404)

    user = request.user if request.user.is_authenticated else None
    guest = _guest_from_request(request)
    if user is not None and guest is not None:
        guest = _claim_guest_principal(guest, user)
        if guest is None:
            return JsonResponse({'ok': False, 'code': 'guest_already_claimed', 'error': 'This guest session belongs to another account.'}, status=409)
    if user is None and (not guest or guest.claimed_by_id):
        code = 'guest_session_required' if not guest else 'guest_session_claimed'
        return JsonResponse({'ok': False, 'code': code, 'error': 'Start a guest session or sign in to continue.'}, status=401 if not guest else 409)
    guest_id = str(guest.id) if guest and not guest.claimed_by_id else ''
    scope = _survey_scope(survey)
    supplied_scope = str(payload.get('scopeId') or payload.get('scope_id') or '').strip()
    if supplied_scope and supplied_scope != scope['id']:
        return JsonResponse({'ok': False, 'code': 'scope_mismatch', 'error': 'This survey belongs to a different participation scope.', 'scopeId': scope['id']}, status=409)

    with transaction.atomic():
        survey = Survey.objects.select_for_update().get(pk=survey.pk, is_active=True)
        if _policy_has_unsupported_score_rule(survey):
            return JsonResponse({'ok': False, 'code': 'unsupported_policy', 'error': 'This survey has a rule the service cannot safely evaluate.'}, status=409)
        if _requires_access_code(survey) and not _has_survey_admission_grant(request, survey, payload.get('accessGrant')):
            code = 'access_grant_invalid' if payload.get('accessGrant') or request.headers.get('X-Feedora-Access-Grant') else 'access_code_required'
            return JsonResponse({'ok': False, 'code': code, 'error': 'Verify the survey access code to continue.'}, status=403)
        revision = _ensure_policy_revision(survey, user)
        principal_attempts = _attempts_for_principal(user, guest_id).filter(survey=survey)
        principal_attempts.filter(status=SurveyAttempt.IN_PROGRESS).exclude(policy_revision=revision).update(
            status=SurveyAttempt.STALE,
            active_key=None,
            updated_at=timezone.now(),
        )

        existing_by_id = SurveyAttempt.objects.filter(attempt_id=requested_id).first()
        if existing_by_id:
            belongs = existing_by_id.survey_id == survey.id and (
                existing_by_id.user_id == user.id if user else existing_by_id.guest_principal_id == guest.id
            )
            if user and not belongs and existing_by_id.guest_principal_id:
                belongs = GuestPrincipal.objects.filter(pk=existing_by_id.guest_principal_id, claimed_by=user).exists()
            if not belongs:
                return JsonResponse({'ok': False, 'code': 'attempt_id_conflict', 'error': 'This attempt identifier is already in use.'}, status=409)
            if existing_by_id.status == SurveyAttempt.COMPLETED:
                return JsonResponse({'ok': False, 'code': 'attempt_completed', 'error': 'This attempt has already been completed.', 'attemptId': str(existing_by_id.attempt_id)}, status=409)
            if existing_by_id.policy_revision_id != revision.id:
                return JsonResponse({'ok': False, 'code': 'policy_changed', 'error': 'The survey rules changed. Review the current rules before continuing.', 'policyVersion': revision.version}, status=409)
            if existing_by_id.status != SurveyAttempt.IN_PROGRESS:
                return JsonResponse({'ok': False, 'code': 'attempt_not_active', 'error': 'This saved attempt is no longer active. Refresh the survey rules and start an available attempt.', 'attemptId': str(existing_by_id.attempt_id)}, status=409)
            return JsonResponse({'ok': True, 'attemptId': str(existing_by_id.attempt_id), 'policyVersion': revision.version, 'scopeId': scope['id'], 'scopeKind': scope['kind'], 'resumed': True})

        active = principal_attempts.filter(status=SurveyAttempt.IN_PROGRESS, policy_revision=revision).first()
        if active:
            return JsonResponse({'ok': True, 'attemptId': str(active.attempt_id), 'policyVersion': revision.version, 'scopeId': scope['id'], 'scopeKind': scope['kind'], 'resumed': True})
        decision, _ = _evaluate_survey_eligibility(request, survey)
        denial = _eligibility_denial(decision)
        if denial:
            return denial
        try:
            with transaction.atomic():
                attempt = SurveyAttempt.objects.create(
                    attempt_id=requested_id, survey=survey, policy_revision=revision, user=user,
                    guest_principal=guest if not user else None, scope_kind=scope['kind'], scope_id=scope['id'],
                )
        except IntegrityError:
            # Another request for this principal may have won the partial unique
            # active-attempt constraint while both saw an empty reservation set.
            winner = principal_attempts.filter(status=SurveyAttempt.IN_PROGRESS, policy_revision=revision).first()
            if not winner:
                collided = SurveyAttempt.objects.filter(attempt_id=requested_id).first()
                if collided:
                    return JsonResponse({'ok': False, 'code': 'attempt_id_conflict', 'error': 'This attempt identifier is already in use.'}, status=409)
                raise
            return JsonResponse({'ok': True, 'attemptId': str(winner.attempt_id), 'policyVersion': revision.version, 'scopeId': scope['id'], 'scopeKind': scope['kind'], 'resumed': True})
    return JsonResponse({'ok': True, 'attemptId': str(attempt.attempt_id), 'policyVersion': revision.version, 'scopeId': scope['id'], 'scopeKind': scope['kind'], 'resumed': False}, status=201)


@require_GET
def api_survey_meta_by_code(request, access_code):
    """Fetch survey metadata and ordered questions by 6-char access code."""
    if not _consume_access_code_attempt(request):
        return JsonResponse({'ok': False, 'code': 'access_code_invalid', 'error': 'The access code could not be verified.'}, status=403)
    try:
        survey = Survey.objects.get(access_code=access_code.strip().upper(), is_active=True, scope_review_required=False)
    except Survey.DoesNotExist:
        return JsonResponse({'ok': False, 'code': 'access_code_invalid', 'error': 'The access code could not be verified.'}, status=403)

    can_manage = request.user.is_authenticated and _can_manage_survey(request.user, survey)
    if not can_manage and _policy_has_unsupported_score_rule(survey):
        return JsonResponse({'ok': False, 'code': 'unsupported_policy', 'error': 'This survey has a rule the service cannot safely evaluate.'}, status=409)
    raw_grant = None
    if _requires_access_code(survey) and not can_manage:
        if not _admission_principal(request):
            return JsonResponse({'ok': False, 'code': 'access_code_invalid', 'error': 'The access code could not be verified.'}, status=403)
        raw_grant = _issue_survey_admission_grant(request, survey)
        if raw_grant is None:
            return JsonResponse({'ok': False, 'code': 'access_code_invalid', 'error': 'The access code could not be verified.'}, status=403)

    if not can_manage:
        decision, _ = _evaluate_survey_eligibility(request, survey, access_grant=raw_grant)
        denial = _eligibility_denial(decision)
        if denial:
            if raw_grant:
                digest = hashlib.sha256(raw_grant.encode('utf-8')).hexdigest()
                SurveyAdmissionGrant.objects.filter(survey=survey, token_hash=digest).delete()
            denial['Cache-Control'] = 'private, no-store'
            return denial

    meta = _serialize_survey_meta(survey, request=request)
    if raw_grant:
        meta['accessGrant'] = raw_grant
    response = JsonResponse(meta)
    response['Cache-Control'] = 'private, no-store'
    return response


@require_POST
def api_survey_submit(request, access_uuid):
    """Submit respondent survey answers via JSON API and award points."""
    try:
        survey = _retry_sqlite_locked_read(
            lambda: Survey.objects.get(access_uuid=access_uuid, is_active=True, scope_review_required=False),
        )
    except Survey.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'Survey not found'}, status=404)

    try:
        if request.content_type == 'application/json' and request.body:
            payload = json.loads(request.body)
        else:
            payload = request.POST
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON body'}, status=400)

    raw_answers = payload.get('answers') or {}
    try:
        answers_fingerprint = hashlib.sha256(
            json.dumps(raw_answers, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str).encode('utf-8')
        ).hexdigest()
    except (TypeError, ValueError):
        return JsonResponse({'ok': False, 'error': 'Answers must be valid JSON data.'}, status=400)
    user = request.user if request.user.is_authenticated else None
    guest_participant_id = ''
    if user is not None:
        guest_credential = _guest_from_request(request)
        if guest_credential:
            guest_credential = _claim_guest_principal(guest_credential, user)
            if guest_credential is None:
                return JsonResponse({'ok': False, 'code': 'guest_already_claimed', 'error': 'This guest session belongs to another account.'}, status=409)
    else:
        guest = _guest_from_request(request)
        if not guest:
            return JsonResponse({'ok': False, 'code': 'guest_session_required', 'error': 'Start a guest session before submitting.'}, status=401)
        if guest.claimed_by_id:
            return JsonResponse({'ok': False, 'code': 'guest_session_claimed', 'error': 'Sign in to continue with this saved response history.'}, status=409)
        guest_participant_id = str(guest.id)
    scope = _survey_scope(survey)
    principal_id = f'user-{user.pk}' if user is not None else guest_participant_id
    supplied_principal_id = str(payload.get('principalId') or payload.get('principal_id') or '').strip()
    if supplied_principal_id and supplied_principal_id != principal_id:
        status = 400 if user is None else 409
        return JsonResponse({
            'ok': False,
            'code': 'principal_mismatch',
            'error': 'This pending response belongs to a different participant.',
        }, status=status)
    supplied_scope_id = str(payload.get('scopeId') or payload.get('scope_id') or '').strip()
    survey_features = survey.features if isinstance(survey.features, dict) else {}
    submission_id = str(payload.get('submissionId') or payload.get('submission_id') or '').strip() or None
    if submission_id and (len(submission_id) > 64 or not re.fullmatch(r'[A-Za-z0-9_-]+', submission_id)):
        return JsonResponse({'ok': False, 'code': 'invalid_submission_id', 'error': 'Invalid submission identifier.'}, status=400)
    requested_attempt_id = str(payload.get('attemptId') or payload.get('attempt_id') or '').strip()
    try:
        requested_attempt_id = uuid.UUID(requested_attempt_id) if requested_attempt_id else None
    except (TypeError, ValueError, AttributeError):
        return JsonResponse({'ok': False, 'code': 'invalid_attempt_id', 'error': 'Invalid attempt identifier.'}, status=400)
    if requested_attempt_id and not submission_id:
        return JsonResponse({'ok': False, 'code': 'submission_id_required', 'error': 'A stable submission identifier is required for a reserved attempt.'}, status=400)

    # Idempotent re-entry check via client submissionId
    if submission_id:
        existing_query = Response.objects.filter(survey=survey, submission_id=submission_id)
        existing_query = existing_query.filter(Q(user=user) | Q(user__isnull=True, guest_participant_id__in=_claimed_guest_ids(user))) if user else existing_query.filter(user__isnull=True, guest_participant_id=guest_participant_id)
        existing_resp = _retry_sqlite_locked_read(existing_query.first)
        if existing_resp:
            existing_scope = {
                'id': existing_resp.scope_id or scope['id'],
                'kind': existing_resp.scope_kind or scope['kind'],
            }
            if supplied_scope_id and supplied_scope_id != existing_scope['id']:
                return JsonResponse({
                    'ok': False,
                    'code': 'scope_mismatch',
                    'error': 'This pending response belongs to a different participation scope.',
                    'scopeId': existing_scope['id'],
                }, status=409)
            if existing_resp.request_hash and existing_resp.request_hash != answers_fingerprint:
                return JsonResponse({
                    'ok': False,
                    'code': 'submission_id_conflict',
                    'error': 'This submission identifier was already used for different answers.',
                }, status=409)
            user_data = _retry_sqlite_locked_read(lambda: _serialize_user(user)) if user else None
            return JsonResponse(_response_receipt(
                existing_resp,
                survey,
                principal_id,
                scope,
                replayed=True,
                total_points=_retry_sqlite_locked_read(
                    lambda: _scope_total_points(survey, existing_scope, user, guest_participant_id),
                ),
                account_total_points=_retry_sqlite_locked_read(
                    lambda: _principal_total_points(user, guest_participant_id),
                ),
                user_data=user_data,
                participant_user=user,
                guest_participant_id=guest_participant_id,
            ), status=200)

    # Return an already accepted receipt before checking a short-lived admission
    # grant. This is required for safe retries after an app resumes offline.
    if _policy_has_unsupported_score_rule(survey):
        return JsonResponse({'ok': False, 'code': 'unsupported_policy', 'error': 'This survey has a rule the service cannot safely evaluate.'}, status=409)
    if _requires_access_code(survey) and not _retry_sqlite_locked_read(
        lambda: _has_survey_admission_grant(request, survey, payload.get('accessGrant')),
    ):
        code = 'access_grant_invalid' if payload.get('accessGrant') or request.headers.get('X-Feedora-Access-Grant') else 'access_code_required'
        return JsonResponse({'ok': False, 'code': code, 'error': 'Verify the survey access code to continue.'}, status=403)

    if supplied_scope_id and supplied_scope_id != scope['id']:
        return JsonResponse({
            'ok': False,
            'code': 'scope_mismatch',
            'error': 'This survey belongs to a different participation scope.',
            'scopeId': scope['id'],
        }, status=409)
    resolved_policy = _normalized_survey_policy(survey)
    policy_version = _survey_policy_version(survey)
    requested_policy_version = str(payload.get('policyVersion') or payload.get('policy_version') or '').strip()
    if requested_policy_version and requested_policy_version != policy_version:
        return JsonResponse({
            'ok': False,
            'code': 'policy_changed',
            'error': 'The survey rules changed while this response was in progress. Review the current rules and try again.',
            'policyVersion': policy_version,
        }, status=409)

    # Eligibility is authoritatively re-evaluated under the survey lock below.
    # Keep only the evidence needed by the scoring and receipt projection here.
    features = survey.features if isinstance(survey.features, dict) else {}
    reward_strategy = resolved_policy['access']['rewardStrategy']
    principal_responses = _responses_for_principal(user, guest_participant_id).filter(survey=survey)
    attempts_used = _count_with_sqlite_lock_retry(principal_responses)
    prior_responses = principal_responses

    # Validate submission invariants against survey definition
    is_valid, validation_errors = _retry_sqlite_locked_read(
        lambda: validate_survey_submission(survey, raw_answers),
    )
    if not is_valid:
        return JsonResponse({
            'ok': False,
            'error': 'Validation failed',
            'errors': validation_errors,
        }, status=400)

    variant = payload.get('variant') or 'gamified'
    experiment_key = payload.get('experimentKey') or payload.get('experiment_key') or 'hmsam-gamification-study'
    platform = payload.get('platform') or ('mobile' if 'mobile' in request.headers.get('User-Agent', '').lower() else 'web')
    app_version = payload.get('appVersion') or payload.get('app_version') or '1.0.0'
    duration_ms = payload.get('durationMs') or payload.get('duration_ms')
    dwell_ms = payload.get('dwellMs') or payload.get('dwell_ms') or {}
    quality_flags = payload.get('qualityFlags') or payload.get('quality_flags') or []
    consent_version = payload.get('consentVersion') or payload.get('consent_version') or '1.0'
    consented_at = timezone.now() if consent_version else None

    response_obj = None
    total_points = 0
    variable_bonus, multiplier, bonus_label = 0, 1.0, None
    nr_eval = {'enabled': False, 'friction_removed': False, 'streak_shielded': False}
    response_replayed = False

    max_db_attempts = 10
    for db_attempt in range(max_db_attempts):
        try:
            with transaction.atomic():
                # Serialize attempts for a survey where the database supports row locks.
                # SQLite falls back to its write lock; the retry path repeats these checks.
                survey = Survey.objects.select_for_update().get(pk=survey.pk, is_active=True)
                if submission_id:
                    locked_existing = Response.objects.filter(survey=survey, submission_id=submission_id)
                    locked_existing = locked_existing.filter(Q(user=user) | Q(user__isnull=True, guest_participant_id__in=_claimed_guest_ids(user))) if user else locked_existing.filter(
                        user__isnull=True,
                        guest_participant_id=guest_participant_id,
                    )
                    locked_existing = locked_existing.first()
                    if locked_existing:
                        existing_scope = {
                            'id': locked_existing.scope_id or _survey_scope(survey)['id'],
                            'kind': locked_existing.scope_kind or _survey_scope(survey)['kind'],
                        }
                        if supplied_scope_id and supplied_scope_id != existing_scope['id']:
                            return JsonResponse({
                                'ok': False,
                                'code': 'scope_mismatch',
                                'error': 'This pending response belongs to a different participation scope.',
                                'scopeId': existing_scope['id'],
                            }, status=409)
                        if locked_existing.request_hash and locked_existing.request_hash != answers_fingerprint:
                            return JsonResponse({
                                'ok': False,
                                'code': 'submission_id_conflict',
                                'error': 'This submission identifier was already used for different answers.',
                            }, status=409)
                        locked_user_data = _serialize_user(user) if user else None
                        return JsonResponse(_response_receipt(
                            locked_existing,
                            survey,
                            principal_id,
                            existing_scope,
                            replayed=True,
                            total_points=_retry_sqlite_locked_read(
                                lambda: _scope_total_points(survey, existing_scope, user, guest_participant_id),
                            ),
                            account_total_points=_retry_sqlite_locked_read(
                                lambda: _principal_total_points(user, guest_participant_id),
                            ),
                            user_data=locked_user_data,
                            participant_user=user,
                            guest_participant_id=guest_participant_id,
                        ), status=200)

                if _policy_has_unsupported_score_rule(survey):
                    return JsonResponse({'ok': False, 'code': 'unsupported_policy', 'error': 'This survey has a rule the service cannot safely evaluate.'}, status=409)
                if _requires_access_code(survey) and not _has_survey_admission_grant(request, survey, payload.get('accessGrant')):
                    code = 'access_grant_invalid' if payload.get('accessGrant') or request.headers.get('X-Feedora-Access-Grant') else 'access_code_required'
                    return JsonResponse({'ok': False, 'code': code, 'error': 'Verify the survey access code to continue.'}, status=403)
                scope = _survey_scope(survey)
                current_policy_version = _survey_policy_version(survey)
                if supplied_scope_id and supplied_scope_id != scope['id']:
                    return JsonResponse({
                        'ok': False,
                        'code': 'scope_mismatch',
                        'error': 'This survey belongs to a different participation scope.',
                        'scopeId': scope['id'],
                    }, status=409)
                if requested_policy_version and requested_policy_version != current_policy_version:
                    return JsonResponse({
                        'ok': False,
                        'code': 'policy_changed',
                        'error': 'The survey rules changed while this response was in progress. Review the current rules and try again.',
                        'policyVersion': current_policy_version,
                    }, status=409)

                current_decision, _ = _evaluate_survey_eligibility(request, survey, access_grant=payload.get('accessGrant'))
                eligibility_error = _eligibility_denial(current_decision)
                if eligibility_error:
                    return eligibility_error

                reserved_attempt = None
                if requested_attempt_id:
                    reserved_attempt = SurveyAttempt.objects.select_for_update().filter(attempt_id=requested_attempt_id, survey=survey).first()
                    owns_attempt = reserved_attempt and (
                        reserved_attempt.user_id == user.id if user else str(reserved_attempt.guest_principal_id) == guest_participant_id
                    )
                    if user and reserved_attempt and not owns_attempt and reserved_attempt.guest_principal_id:
                        owns_attempt = GuestPrincipal.objects.filter(pk=reserved_attempt.guest_principal_id, claimed_by=user).exists()
                    if not owns_attempt:
                        return JsonResponse({'ok': False, 'code': 'attempt_not_found', 'error': 'This saved attempt could not be found for this participant.'}, status=404)
                    if reserved_attempt.status != SurveyAttempt.IN_PROGRESS:
                        return JsonResponse({'ok': False, 'code': 'attempt_not_active', 'error': 'This attempt is no longer active.'}, status=409)
                    if reserved_attempt.policy_revision.version != current_policy_version:
                        reserved_attempt.status = SurveyAttempt.STALE
                        reserved_attempt.save(update_fields=['status', 'updated_at'])
                        return JsonResponse({'ok': False, 'code': 'policy_changed', 'error': 'The survey rules changed. Review the current rules before continuing.', 'policyVersion': current_policy_version}, status=409)
                    if reserved_attempt.scope_kind != scope['kind'] or reserved_attempt.scope_id != scope['id']:
                        return JsonResponse({'ok': False, 'code': 'scope_mismatch', 'error': 'This attempt belongs to a different participation scope.'}, status=409)
                else:
                    active_attempt = _attempts_for_principal(user, guest_participant_id).filter(survey=survey, status=SurveyAttempt.IN_PROGRESS).first()
                    if active_attempt:
                        return JsonResponse({'ok': False, 'code': 'attempt_reservation_required', 'error': 'Resume the saved attempt before submitting.'}, status=409)

                locked_policy = _normalized_survey_policy(survey)['access']
                locked_principal_responses = _responses_for_principal(user, guest_participant_id).filter(survey=survey)
                locked_principal_responses = locked_principal_responses if user else locked_principal_responses.filter(
                    user__isnull=True,
                    guest_participant_id=guest_participant_id,
                )
                locked_attempts_used = locked_principal_responses.count()
                attempts_used = locked_attempts_used
                prior_responses = locked_principal_responses
                reward_strategy = locked_policy['rewardStrategy']

                response_obj = Response.objects.create(
                    survey=survey,
                    user=user,
                    guest_participant_id=guest_participant_id,
                    submission_id=submission_id,
                    request_hash=answers_fingerprint,
                    scope_kind=scope['kind'],
                    scope_id=scope['id'],
                    policy_version=current_policy_version,
                    attempt=reserved_attempt,
                    variant=variant,
                    experiment_key=experiment_key,
                    platform=platform,
                    app_version=app_version,
                    duration_ms=duration_ms,
                    dwell_ms=dwell_ms if isinstance(dwell_ms, dict) else {},
                    quality_flags=quality_flags if isinstance(quality_flags, list) else [str(quality_flags)],
                    consent_version=consent_version,
                    consented_at=consented_at,
                    points_earned=0
                )
                total_points = 0

                # Normalise raw_answers: can be dict { "questionId": val } or list [ { "questionId": ..., "val": ... } ]
                answers_dict = {}
                if isinstance(raw_answers, dict):
                    answers_dict = raw_answers
                elif isinstance(raw_answers, list):
                    for item in raw_answers:
                        qid = item.get('questionId') or item.get('id') or item.get('code')
                        if qid is not None:
                            answers_dict[str(qid)] = item.get('val') if 'val' in item else item.get('value')

                for question in survey.questions.all().order_by('order'):
                    key = str(question.id)
                    alt_key = f'q_{question.id}'
                    code_key = question.code
                    val = answers_dict.get(key)
                    if val is None:
                        val = answers_dict.get(alt_key)
                    if val is None and code_key:
                        val = answers_dict.get(code_key)
                    if val is None and hasattr(request, 'POST'):
                        val = request.POST.get(alt_key) or request.POST.get(key)

                    if val is None:
                        continue

                    if question.question_type in ('likert', 'rating'):
                        try:
                            num_val = int(val)
                            Answer.objects.create(response=response_obj, question=question, likert_value=num_val)
                            total_points += calculate_question_points(question.question_type, survey.features)
                        except (ValueError, TypeError):
                            pass
                    elif question.question_type in ('short', 'open', 'date'):
                        str_val = str(val).strip()
                        if str_val:
                            if len(str_val) > 10000:
                                str_val = str_val[:10000]
                            Answer.objects.create(response=response_obj, question=question, text_value=str_val)
                            total_points += calculate_question_points(question.question_type, survey.features, str_val)
                    elif question.question_type in ('mcq', 'dropdown'):
                        opt_obj = None
                        try:
                            opt_obj = MCQOption.objects.filter(question=question, id=int(val)).first()
                        except (ValueError, TypeError):
                            pass
                        if not opt_obj and isinstance(val, str):
                            opt_obj = MCQOption.objects.filter(question=question, text__iexact=val.strip()).first()
                        if opt_obj:
                            Answer.objects.create(response=response_obj, question=question, selected_option=opt_obj)
                            total_points += calculate_question_points(question.question_type, survey.features)
                    elif question.question_type == 'checkbox':
                        vals = val if isinstance(val, list) else [val]
                        opt_count = 0
                        for v in vals:
                            opt = None
                            try:
                                opt = MCQOption.objects.filter(question=question, id=int(v)).first()
                            except (ValueError, TypeError):
                                pass
                            if not opt and isinstance(v, str):
                                opt = MCQOption.objects.filter(question=question, text__iexact=v.strip()).first()
                            if opt:
                                Answer.objects.create(response=response_obj, question=question, selected_option=opt)
                                opt_count += 1
                        if opt_count > 0:
                            total_points += calculate_question_points('checkbox', survey.features)

                if variant == 'control':
                    total_points = 0

                if {'STRAIGHT_LINING', 'SPEEDER'}.issubset(set(quality_flags if isinstance(quality_flags, list) else [])):
                    total_points = 0

                # Optional Variable Rewards bonus (Skinnerian Variable-Ratio)
                variable_bonus, multiplier, bonus_label = 0, 1.0, None
                if variant != 'control' and total_points > 0:
                    variable_bonus, multiplier, bonus_label = calculate_variable_reward(total_points, survey.features)
                    total_points += variable_bonus

                if attempts_used > 0:
                    previous_best = (
                        prior_responses.order_by('-points_earned')
                        .values_list('points_earned', flat=True)
                        .first() or 0
                    ) if prior_responses is not None else 0
                    if reward_strategy in ('first_attempt_only', 'blocked'):
                        total_points = 0
                    elif reward_strategy == 'score_delta':
                        total_points = max(0, total_points - previous_best)
                    if total_points == 0:
                        variable_bonus = 0
                        bonus_label = None

                # Optional Negative Reinforcement perks (Friction Removal & Streak Shielding)
                # Use the principal-scoped count computed under the survey lock. This
                # includes claimed guest history and remains correct for anonymous
                # participants, unlike counting only responses attached to a User.
                responses_count = locked_attempts_used + 1
                nr_eval = evaluate_negative_reinforcement(
                    survey.features,
                    responses_count=responses_count,
                    completed_clean=len(quality_flags) == 0
                )

                response_obj.points_earned = total_points
                response_obj.save(update_fields=['points_earned'])
                if reserved_attempt:
                    reserved_attempt.status = SurveyAttempt.COMPLETED
                    reserved_attempt.completed_at = timezone.now()
                    reserved_attempt.save(update_fields=['status', 'completed_at', 'updated_at'])
                _record_platform_aggregate_completion(survey, response_obj)

                # Increment respondent profile total points atomically via database F() expression
                if user:
                    profile, _ = RespondentProfile.objects.get_or_create(user=user)
                    if total_points > 0:
                        RespondentProfile.objects.filter(pk=profile.pk).update(
                            total_points=F('total_points') + total_points
                        )
                        profile.refresh_from_db()
            break
        except IntegrityError:
            # A concurrent replay may win the unique constraint after our early lookup.
            existing_query = Response.objects.filter(survey=survey, submission_id=submission_id)
            existing_query = existing_query.filter(Q(user=user) | Q(user__isnull=True, guest_participant_id__in=_claimed_guest_ids(user))) if user else existing_query.filter(
                user__isnull=True,
                guest_participant_id=guest_participant_id,
            )
            existing_resp = existing_query.first() if submission_id else None
            if existing_resp is None:
                raise
            if existing_resp.request_hash and existing_resp.request_hash != answers_fingerprint:
                return JsonResponse({
                    'ok': False,
                    'code': 'submission_id_conflict',
                    'error': 'This submission identifier was already used for different answers.',
                }, status=409)
            existing_scope = {
                'id': existing_resp.scope_id or scope['id'],
                'kind': existing_resp.scope_kind or scope['kind'],
            }
            if supplied_scope_id and supplied_scope_id != existing_scope['id']:
                return JsonResponse({
                    'ok': False,
                    'code': 'scope_mismatch',
                    'error': 'This pending response belongs to a different participation scope.',
                    'scopeId': existing_scope['id'],
                }, status=409)
            response_obj = existing_resp
            total_points = existing_resp.points_earned
            variable_bonus = 0
            bonus_label = None
            response_replayed = True
            break
        except OperationalError as db_err:
            if 'locked' in str(db_err).lower() and db_attempt < max_db_attempts - 1:
                time.sleep(0.05 * (1.5 ** db_attempt) + random.uniform(0.02, 0.08))
                continue
            raise

    user_data = _retry_sqlite_locked_read(lambda: _serialize_user(user)) if user else None
    receipt_scope = {
        'kind': response_obj.scope_kind or scope['kind'],
        'id': response_obj.scope_id or scope['id'],
    }
    receipt = _response_receipt(
        response_obj,
        survey,
        principal_id,
        scope,
        replayed=response_replayed,
        total_points=_retry_sqlite_locked_read(
            lambda: _scope_total_points(survey, receipt_scope, user, guest_participant_id),
        ),
        account_total_points=_retry_sqlite_locked_read(
            lambda: _principal_total_points(user, guest_participant_id),
        ),
        user_data=user_data,
        participant_user=user,
        guest_participant_id=guest_participant_id,
    )
    receipt.update({
        'variant': response_obj.variant,
        'arm': response_obj.variant,
        'platform': response_obj.platform,
        'qualityFlags': response_obj.quality_flags,
        'variableBonus': variable_bonus,
        'bonusLabel': bonus_label,
        'negativeReinforcement': nr_eval,
    })
    return JsonResponse(receipt)


@require_GET
def api_creator_surveys(request):
    """List surveys owned by the authenticated creator or all surveys for superuser/admin."""
    if not request.user.is_authenticated:
        return JsonResponse({'ok': False, 'error': 'Authentication required.'}, status=401)

    is_staff_or_admin = request.user.is_staff or request.user.is_superuser
    if not (request.user.role == 'creator' or _has_survey_manager_membership(request.user) or is_staff_or_admin):
        return JsonResponse({'ok': False, 'error': 'Creator permissions required.'}, status=403)

    if is_staff_or_admin:
        surveys = Survey.objects.all().order_by('-created_at')
    else:
        managed_scope_ids = OrganizationMembership.objects.filter(
            user=request.user,
            role__in=OrganizationMembership.SURVEY_MANAGER_ROLES,
            status=OrganizationMembership.ACTIVE,
        ).values_list('organization_id', flat=True)
        surveys = Survey.objects.filter(
            Q(created_by=request.user)
            | Q(organization_scope_id__in=managed_scope_ids),
        ).distinct().order_by('-created_at')

    items = []
    for s in surveys:
        scope = _survey_scope(s)
        items.append({
            'id': s.id,
            'uuid': str(s.access_uuid),
            'title': s.title,
            'description': s.description or '',
            'isHmsam': s.is_hmsam,
            'isActive': s.is_active,
            'isDraft': s.is_draft,
            'questionsCount': s.questions.count(),
            'responsesCount': s.responses.count(),
            'createdAt': s.created_at.isoformat(),
            'scopeId': scope['id'],
            'scopeKind': scope['kind'],
        })

    return JsonResponse({'ok': True, 'surveys': items})


@require_POST
@transaction.atomic
def api_creator_create_survey(request):
    """Scaffold and publish or draft a new survey from mobile/web Studio."""
    from surveys.policy import PolicyValidationError, validate_policy

    if not request.user.is_authenticated:
        return JsonResponse({'ok': False, 'error': 'Authentication required.'}, status=401)

    is_staff_or_admin = request.user.is_staff or request.user.is_superuser
    if not (request.user.role == 'creator' or _has_survey_manager_membership(request.user) or is_staff_or_admin):
        return JsonResponse({'ok': False, 'error': 'Creator permissions required.'}, status=403)

    try:
        if request.content_type == 'application/json' and request.body:
            data = json.loads(request.body)
        else:
            data = request.POST
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON body'}, status=400)

    title = str(data.get('title') or '').strip()
    if not title:
        return JsonResponse({'ok': False, 'error': 'Survey title is required.'}, status=400)

    description = str(data.get('description') or '').strip()
    is_hmsam = bool(data.get('is_hmsam') or data.get('isHmsam', False))
    is_draft = bool(data.get('is_draft') or data.get('isDraft', False))
    questions_data = data.get('questions') or []
    requested_org = data.get('organizationId') or data.get('organization_id')
    if isinstance(requested_org, str):
        requested_org = requested_org.removeprefix('org-')
    if requested_org:
        membership = OrganizationMembership.objects.filter(
            user=request.user, organization_id=requested_org, status=OrganizationMembership.ACTIVE,
            role__in=OrganizationMembership.SURVEY_MANAGER_ROLES,
        ).select_related('organization').first()
        if not membership and not is_staff_or_admin:
            return JsonResponse({'ok': False, 'code': 'organization_forbidden', 'error': 'You need an active organization owner or admin role to create a survey in it.'}, status=403)
        organization_scope = membership.organization if membership else Organization.objects.filter(pk=requested_org).first()
        if not organization_scope:
            return JsonResponse({'ok': False, 'code': 'organization_not_found', 'error': 'Organization not found.'}, status=404)
    else:
        managed_scopes = Organization.objects.filter(
            memberships__user=request.user,
            memberships__role__in=OrganizationMembership.SURVEY_MANAGER_ROLES,
            memberships__status=OrganizationMembership.ACTIVE,
        ).distinct()
        if managed_scopes.count() == 1:
            organization_scope = managed_scopes.first()
        elif managed_scopes.exists():
            return JsonResponse({
                'ok': False,
                'code': 'organization_required',
                'error': 'Choose which organization will own this survey.',
            }, status=400)
        else:
            return JsonResponse({
                'ok': False,
                'code': 'organization_required',
                'error': 'Create or join an organization before creating an organization survey.',
            }, status=400)

    create_features = data.get('features', {})
    if not isinstance(create_features, dict):
        return JsonResponse({'ok': False, 'code': 'invalid_policy', 'error': 'Survey features must be an object.'}, status=400)
    create_access = create_features.get('accessPolicy') or create_features.get('access_policy') or {}
    if not isinstance(create_access, dict):
        return JsonResponse({'ok': False, 'code': 'invalid_policy', 'field': 'accessPolicy', 'error': 'Access rules must be an object.'}, status=400)
    allowed_create_access_keys = {
        'requiresAccount', 'requires_account', 'organizationMembershipRequired',
        'organization_membership_required', 'allowRedo', 'allow_redo',
        'rewardStrategy', 'reward_strategy', 'maxAttempts', 'max_attempts',
        'cooldownMinutes', 'cooldown_minutes',
    }
    unknown_create_access_keys = set(create_access) - allowed_create_access_keys
    if unknown_create_access_keys:
        key = sorted(unknown_create_access_keys)[0]
        return JsonResponse({'ok': False, 'code': 'invalid_policy', 'field': f'accessPolicy.{key}', 'error': 'This access rule is not supported.'}, status=400)
    score_keys = {'minScorePercent', 'min_score_percent', 'minimumScore', 'minimum_score', 'minimumScorePercent', 'minimum_score_percent', 'scoreThreshold', 'score_threshold'}
    create_prerequisites = create_features.get('prerequisites') if isinstance(create_features.get('prerequisites'), dict) else {}
    allowed_create_prerequisite_keys = {'completedFormIds', 'completed_form_ids', 'unlockDelayMs', 'unlock_delay_ms'}
    unknown_create_prerequisite_keys = set(create_prerequisites) - allowed_create_prerequisite_keys
    if unknown_create_prerequisite_keys:
        key = sorted(unknown_create_prerequisite_keys)[0]
        return JsonResponse({'ok': False, 'code': 'invalid_policy', 'field': f'prerequisites.{key}', 'error': 'This prerequisite rule is not supported.'}, status=400)
    if score_keys.intersection(create_features) or score_keys.intersection(create_access) or score_keys.intersection(create_prerequisites):
        return JsonResponse({'ok': False, 'code': 'unsupported_policy', 'error': 'Minimum-score rules are unavailable until scoring is verified by the research service.'}, status=400)
    for key in ('requiresAccount', 'requires_account', 'organizationMembershipRequired', 'organization_membership_required', 'allowRedo', 'allow_redo'):
        if key in create_access and not isinstance(create_access[key], bool):
            return JsonResponse({'ok': False, 'code': 'invalid_policy', 'field': f'accessPolicy.{key}', 'error': 'This setting must be true or false.'}, status=400)
    if create_access.get('organizationMembershipRequired', create_access.get('organization_membership_required', False)) and organization_scope is None:
        return JsonResponse({'ok': False, 'code': 'invalid_policy', 'error': 'Organization membership rules require an organization survey.'}, status=400)
    if 'accessCodeRequired' in create_features and not isinstance(create_features['accessCodeRequired'], bool):
        return JsonResponse({'ok': False, 'code': 'invalid_policy', 'field': 'accessCodeRequired', 'error': 'This setting must be true or false.'}, status=400)
    try:
        max_attempts = create_access.get('maxAttempts', create_access.get('max_attempts'))
        if max_attempts not in (None, '') and (isinstance(max_attempts, bool) or int(max_attempts) != float(max_attempts) or not 1 <= int(max_attempts) <= 1000):
            raise ValueError
        cooldown = create_access.get('cooldownMinutes', create_access.get('cooldown_minutes', 0))
        if cooldown not in (None, '') and (isinstance(cooldown, bool) or not 0 <= float(cooldown) <= 525600):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        return JsonResponse({'ok': False, 'code': 'invalid_policy', 'error': 'Attempt and cooldown limits are outside the supported range.'}, status=400)
    raw_allow_redo = create_access.get('allowRedo', create_access.get('allow_redo'))
    raw_max_attempts = create_access.get('maxAttempts', create_access.get('max_attempts'))
    parsed_max_attempts = int(raw_max_attempts) if raw_max_attempts not in (None, '') else None
    if raw_allow_redo is not None and parsed_max_attempts is not None and raw_allow_redo != (parsed_max_attempts > 1):
        return JsonResponse({
            'ok': False, 'code': 'invalid_policy', 'field': 'accessPolicy.allowRedo',
            'error': 'Repeat attempts are determined by the maximum attempt limit.',
        }, status=400)
    raw_create_prereqs = create_prerequisites.get('completedFormIds', create_prerequisites.get('completed_form_ids', create_features.get('prerequisites', [])))
    if not isinstance(raw_create_prereqs, list) or len(raw_create_prereqs) > 25:
        return JsonResponse({'ok': False, 'code': 'invalid_policy', 'field': 'prerequisites', 'error': 'Use a list of at most 25 prerequisite surveys.'}, status=400)
    for prereq_value in raw_create_prereqs:
        prereq_key = str(prereq_value).strip()
        prerequisite = _survey_for_reference(prereq_key)
        if not prerequisite or not prerequisite.is_active or prerequisite.is_draft or _survey_scope(prerequisite) != (
            {'kind': 'organization', 'id': f'org-{organization_scope.id}'} if organization_scope else {'kind': 'platform', 'id': 'platform'}
        ):
            return JsonResponse({'ok': False, 'code': 'invalid_policy', 'field': 'prerequisites', 'error': 'Prerequisite surveys must be published in the same organization scope.'}, status=400)
    create_sequence = create_features.get('sequence')
    if create_sequence is not None:
        if not isinstance(create_sequence, dict):
            return JsonResponse({'ok': False, 'code': 'invalid_policy', 'field': 'sequence', 'error': 'Sequence rules must be an object.'}, status=400)
        unknown_create_sequence_keys = set(create_sequence) - {'groupId', 'group_id', 'order', 'sequenceOrder', 'enforce', 'enforceSequence'}
        if unknown_create_sequence_keys:
            key = sorted(unknown_create_sequence_keys)[0]
            return JsonResponse({'ok': False, 'code': 'invalid_policy', 'field': f'sequence.{key}', 'error': 'This sequence rule is not supported.'}, status=400)
        group_id = str(create_sequence.get('groupId', create_sequence.get('group_id', '')) or '').strip()
        raw_order = create_sequence.get('order', create_sequence.get('sequenceOrder'))
        enforce = create_sequence.get('enforce', create_sequence.get('enforceSequence', False))
        try:
            if not group_id or len(group_id) > 100 or isinstance(raw_order, bool) or int(raw_order) != float(raw_order) or not 1 <= int(raw_order) <= 20:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            return JsonResponse({'ok': False, 'code': 'invalid_policy', 'field': 'sequence', 'error': 'Sequence needs a name and a whole step between 1 and 20.'}, status=400)
        if not isinstance(enforce, bool):
            return JsonResponse({'ok': False, 'code': 'invalid_policy', 'field': 'sequence.enforce', 'error': 'This setting must be true or false.'}, status=400)
        _lock_survey_policy_scope('organization', f'org-{organization_scope.id}')
        if _sequence_position_conflicts(None, group_id, int(raw_order), organization_scope_id=organization_scope.id if organization_scope else None):
            return JsonResponse({'ok': False, 'code': 'invalid_policy', 'field': 'sequence.order', 'error': 'Another survey in this scope already uses that sequence step.'}, status=400)

    raw_allow_redo = create_access.get('allowRedo', create_access.get('allow_redo'))
    raw_max_attempts = create_access.get('maxAttempts', create_access.get('max_attempts'))
    canonical_max_attempts = (
        (None if raw_allow_redo is True else 1)
        if raw_max_attempts in (None, '') else int(raw_max_attempts)
    )
    canonical_allow_redo = canonical_max_attempts is None or canonical_max_attempts > 1
    raw_cooldown_minutes = create_access.get('cooldownMinutes', create_access.get('cooldown_minutes', 0))
    raw_prerequisite_delay = create_prerequisites.get('unlockDelayMs', create_prerequisites.get('unlock_delay_ms', 0))
    canonical_sequence = None
    if create_sequence:
        canonical_sequence = {
            'groupId': str(create_sequence.get('groupId', create_sequence.get('group_id', ''))).strip(),
            'order': int(create_sequence.get('order', create_sequence.get('sequenceOrder'))),
            'enforce': create_sequence.get('enforce', create_sequence.get('enforceSequence', False)),
        }
    try:
        validate_policy({
            'schemaVersion': 1,
            'participation': {
                'accountRequired': create_access.get('requiresAccount', create_access.get('requires_account', False)),
                'organizationMembershipRequired': create_access.get('organizationMembershipRequired', create_access.get('organization_membership_required', False)),
                'accessCodeRequired': create_features.get('accessCodeRequired', False),
            },
            'attempts': {
                'maxAttempts': canonical_max_attempts,
                'cooldownSeconds': round(float(raw_cooldown_minutes or 0) * 60),
                'repeatReward': create_access.get('rewardStrategy', create_access.get('reward_strategy', 'first_attempt_only')),
            },
            'prerequisites': [
                {'surveyId': str(value), **({'unlockDelayMs': raw_prerequisite_delay} if raw_prerequisite_delay not in (None, 0, '0', '') else {})}
                for value in raw_create_prereqs
            ],
            'sequence': canonical_sequence,
        })
    except (PolicyValidationError, TypeError, ValueError, OverflowError) as exc:
        field = exc.field if isinstance(exc, PolicyValidationError) else 'policy'
        code = exc.code if isinstance(exc, PolicyValidationError) else 'invalid_policy'
        return JsonResponse({'ok': False, 'code': code, 'field': field, 'error': str(exc)}, status=400)

    canonical_create_features = dict(create_features)
    canonical_create_policy = dict(create_access)
    canonical_create_policy.update({
        'allowRedo': canonical_allow_redo,
        'maxAttempts': canonical_max_attempts,
    })
    canonical_create_policy.pop('allow_redo', None)
    canonical_create_policy.pop('max_attempts', None)
    canonical_create_features['accessPolicy'] = canonical_create_policy
    canonical_create_features.pop('access_policy', None)

    with transaction.atomic():
        survey = Survey.objects.create(
            created_by=request.user,
            organization_scope=organization_scope,
            scope_kind='organization',
            title=title,
            description=description,
            is_hmsam=is_hmsam,
            is_draft=is_draft,
            is_active=not is_draft,
            theme_accent=data.get('theme_accent', '#14140f'),
            theme_bg=data.get('theme_bg', '#fafaf7'),
            submit_label=data.get('submit_label', 'Submit'),
            btn_color=data.get('btn_color', '#14140f'),
            btn_radius=data.get('btn_radius', '6'),
            q_font=data.get('q_font', "'Inter',sans-serif"),
            header_font=data.get('header_font', "'Instrument Serif',serif"),
            features={key: value for key, value in canonical_create_features.items() if key not in ('scope', 'is_global')}
        )
        _ensure_policy_revision(survey, request.user)

        for i, q in enumerate(questions_data):
            q_text = str(q.get('text') or '').strip()
            if not q_text:
                continue
            question = Question.objects.create(
                survey=survey,
                text=q_text,
                code=str(q.get('code') or ''),
                question_type=str(q.get('type') or 'likert'),
                hmsam_construct=str(q.get('construct') or '') or None,
                order=i,
                is_required=bool(q.get('required', True)),
                rating_style=str(q.get('ratingStyle', 'stars')),
                rating_max=int(q.get('ratingMax', 5)),
                section_title=str(q.get('sectionTitle', '')),
                section_num=int(q.get('sectionNum', 1)),
            )

            raw_options = q.get('options') or []
            for j, opt in enumerate(raw_options):
                opt_text = opt if isinstance(opt, str) else opt.get('text', '')
                if opt_text:
                    MCQOption.objects.create(question=question, text=str(opt_text).strip(), order=j)

    return JsonResponse({
        'ok': True,
        'uuid': str(survey.access_uuid),
        'accessCode': survey.access_code,
        'surveyId': survey.id,
        'isDraft': survey.is_draft,
        'isActive': survey.is_active,
        'questionsCount': survey.questions.count(),
    }, status=201)


@require_POST
@transaction.atomic
def api_creator_update_survey_policy(request, access_uuid):
    """Update participation rules while preserving the survey's other features."""
    from surveys.policy import PolicyValidationError, validate_policy

    if not request.user.is_authenticated:
        return JsonResponse({'ok': False, 'error': 'Authentication required.'}, status=401)
    is_staff_or_admin = request.user.is_staff or request.user.is_superuser
    if not (request.user.role == 'creator' or _has_survey_manager_membership(request.user) or is_staff_or_admin):
        return JsonResponse({'ok': False, 'error': 'Creator permissions required.'}, status=403)
    survey = Survey.objects.filter(access_uuid=access_uuid).first()
    if not survey:
        return JsonResponse({'ok': False, 'error': 'Survey not found.'}, status=404)
    if not _can_manage_survey(request.user, survey):
        return JsonResponse({'ok': False, 'error': 'You do not own this survey.'}, status=403)
    scope = _survey_scope(survey)
    _lock_survey_policy_scope(scope['kind'], scope['id'])
    survey = Survey.objects.select_for_update().get(pk=survey.pk)
    if not _can_manage_survey(request.user, survey):
        return JsonResponse({'ok': False, 'error': 'You do not own this survey.'}, status=403)
    try:
        data = json.loads(request.body) if request.content_type == 'application/json' and request.body else request.POST
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON body.'}, status=400)

    incoming = data.get('features') or {}
    if not isinstance(incoming, dict):
        return JsonResponse({'ok': False, 'error': 'Access rules must be an object.'}, status=400)
    features = survey.features if isinstance(survey.features, dict) else {}
    policy = incoming.get('accessPolicy') or incoming.get('access_policy') or {}
    if not isinstance(policy, dict):
        return JsonResponse({'ok': False, 'error': 'Access policy must be an object.'}, status=400)
    allowed_access_keys = {
        'requiresAccount', 'requires_account', 'organizationMembershipRequired',
        'organization_membership_required', 'allowRedo', 'allow_redo',
        'rewardStrategy', 'reward_strategy', 'maxAttempts', 'max_attempts',
        'cooldownMinutes', 'cooldown_minutes',
    }
    unknown_access_keys = set(policy) - allowed_access_keys
    if unknown_access_keys:
        key = sorted(unknown_access_keys)[0]
        return JsonResponse({'ok': False, 'code': 'invalid_policy', 'field': f'accessPolicy.{key}', 'error': 'This access rule is not supported.'}, status=400)
    incoming_prerequisites = incoming.get('prerequisites') if isinstance(incoming.get('prerequisites'), dict) else {}
    unsupported_score_keys = {'minScorePercent', 'min_score_percent', 'minimumScore', 'minimum_score', 'minimumScorePercent', 'minimum_score_percent', 'scoreThreshold', 'score_threshold'}
    if unsupported_score_keys.intersection(policy) or unsupported_score_keys.intersection(incoming) or unsupported_score_keys.intersection(incoming_prerequisites):
        return JsonResponse({'ok': False, 'code': 'unsupported_policy', 'error': 'Minimum-score rules are unavailable until scoring is verified by the research service.'}, status=400)
    for key in ('allowRedo', 'allow_redo', 'requiresAccount', 'requires_account', 'organizationMembershipRequired'):
        if key in policy and not isinstance(policy[key], bool):
            return JsonResponse({'ok': False, 'code': 'invalid_policy', 'field': f'accessPolicy.{key}', 'error': 'This setting must be true or false.'}, status=400)
    for key in ('accessCodeRequired', 'access_code_required'):
        if key in incoming and not isinstance(incoming[key], bool):
            return JsonResponse({'ok': False, 'code': 'invalid_policy', 'field': key, 'error': 'This setting must be true or false.'}, status=400)
    raw_allow_redo = policy.get('allowRedo', policy.get('allow_redo'))
    reward_strategy = policy.get('rewardStrategy', policy.get('reward_strategy', 'first_attempt_only'))
    if reward_strategy not in ('first_attempt_only', 'score_delta', 'unrestricted', 'blocked'):
        return JsonResponse({'ok': False, 'error': 'Invalid repeat reward strategy.'}, status=400)
    canonical_policy = {
        'requiresAccount': bool(policy.get('requiresAccount', policy.get('requires_account', False))),
        'organizationMembershipRequired': bool(policy.get('organizationMembershipRequired', policy.get('organization_membership_required', False))),
        'rewardStrategy': reward_strategy,
    }
    raw_max_attempts = policy.get('maxAttempts', policy.get('max_attempts'))
    if raw_max_attempts not in (None, ''):
        try:
            if isinstance(raw_max_attempts, bool) or int(raw_max_attempts) != float(raw_max_attempts) or not 1 <= int(raw_max_attempts) <= 1000:
                raise ValueError
            canonical_policy['maxAttempts'] = int(raw_max_attempts)
        except (TypeError, ValueError, OverflowError):
            return JsonResponse({'ok': False, 'code': 'invalid_policy', 'error': 'Maximum attempts must be a whole number between 1 and 1000.'}, status=400)
    if raw_allow_redo is not None and 'maxAttempts' in canonical_policy and raw_allow_redo != (canonical_policy['maxAttempts'] > 1):
        return JsonResponse({
            'ok': False, 'code': 'invalid_policy', 'field': 'accessPolicy.allowRedo',
            'error': 'Repeat attempts are determined by the maximum attempt limit.',
        }, status=400)
    effective_max_attempts = canonical_policy.get('maxAttempts')
    if 'maxAttempts' not in canonical_policy:
        effective_max_attempts = None if raw_allow_redo is True else 1
    allow_redo = effective_max_attempts is None or effective_max_attempts > 1
    canonical_policy['allowRedo'] = allow_redo
    canonical_policy['maxAttempts'] = effective_max_attempts
    raw_cooldown = policy.get('cooldownMinutes', policy.get('cooldown_minutes'))
    if raw_cooldown not in (None, ''):
        try:
            cooldown_minutes = float(raw_cooldown)
            if isinstance(raw_cooldown, bool) or not (0 <= cooldown_minutes <= 525600):
                raise ValueError
            if cooldown_minutes:
                canonical_policy['cooldownMinutes'] = cooldown_minutes
        except (TypeError, ValueError):
            return JsonResponse({'ok': False, 'error': 'Cooldown must be between 0 and 525600 minutes.'}, status=400)

    raw_prerequisites = incoming.get('prerequisites', [])
    prerequisites = raw_prerequisites if isinstance(raw_prerequisites, dict) else {'completedFormIds': raw_prerequisites}
    if not isinstance(prerequisites, dict):
        return JsonResponse({'ok': False, 'error': 'Prerequisites must be a list or object.'}, status=400)
    allowed_prerequisite_keys = {'completedFormIds', 'completed_form_ids', 'unlockDelayMs', 'unlock_delay_ms'}
    unknown_prerequisite_keys = set(prerequisites) - allowed_prerequisite_keys
    if unknown_prerequisite_keys:
        key = sorted(unknown_prerequisite_keys)[0]
        return JsonResponse({'ok': False, 'code': 'invalid_policy', 'field': f'prerequisites.{key}', 'error': 'This prerequisite rule is not supported.'}, status=400)
    access_code_required = incoming.get('accessCodeRequired', features.get('accessCodeRequired', False))
    if not isinstance(access_code_required, bool):
        return JsonResponse({'ok': False, 'code': 'invalid_policy', 'field': 'accessCodeRequired', 'error': 'This setting must be true or false.'}, status=400)
    if canonical_policy['organizationMembershipRequired'] and survey.scope_kind != 'organization':
        return JsonResponse({'ok': False, 'code': 'invalid_policy', 'field': 'accessPolicy.organizationMembershipRequired', 'error': 'Organization membership rules require an organization survey.'}, status=400)
    prerequisite_ids = prerequisites.get('completedFormIds', prerequisites.get('completed_form_ids', []))
    if not isinstance(prerequisite_ids, list) or len(prerequisite_ids) > 25:
        return JsonResponse({'ok': False, 'code': 'invalid_policy', 'error': 'Use a list of at most 25 prerequisite surveys.'}, status=400)
    normalized_ids = []
    for value in prerequisite_ids:
        key = str(value).strip()
        candidate = _survey_for_reference(key)
        if candidate is None or not candidate.is_active or candidate.is_draft:
            return JsonResponse({'ok': False, 'error': 'Each prerequisite must be a published survey.'}, status=400)
        if candidate.id != survey.id and _survey_scope(candidate) != _survey_scope(survey):
            return JsonResponse({'ok': False, 'error': 'Prerequisites must belong to this survey’s participation scope.'}, status=400)
        if candidate.id == survey.id:
            return JsonResponse({'ok': False, 'error': 'A survey cannot require itself.'}, status=400)
        normalized_ids.append(str(candidate.access_uuid))
    normalized_ids = list(dict.fromkeys(normalized_ids))
    unlock_delay = prerequisites.get('unlockDelayMs', prerequisites.get('unlock_delay_ms', 0))
    try:
        if isinstance(unlock_delay, bool) or int(unlock_delay) != float(unlock_delay) or not 0 <= int(unlock_delay) <= 31_536_000_000:
            raise ValueError
        unlock_delay = int(unlock_delay)
    except (TypeError, ValueError, OverflowError):
        return JsonResponse({'ok': False, 'code': 'invalid_policy', 'field': 'prerequisites.unlockDelayMs', 'error': 'Prerequisite wait must be a whole number of milliseconds between 0 and one year.'}, status=400)
    updated_prerequisites = {'completedFormIds': normalized_ids}
    if unlock_delay:
        updated_prerequisites['unlockDelayMs'] = unlock_delay

    sequence = incoming.get('sequence', {})
    if not isinstance(sequence, dict):
        return JsonResponse({'ok': False, 'code': 'invalid_policy', 'field': 'sequence', 'error': 'Sequence must be an object.'}, status=400)
    unknown_sequence_keys = set(sequence) - {'groupId', 'group_id', 'order', 'sequenceOrder', 'enforce', 'enforceSequence'}
    if unknown_sequence_keys:
        key = sorted(unknown_sequence_keys)[0]
        return JsonResponse({'ok': False, 'code': 'invalid_policy', 'field': f'sequence.{key}', 'error': 'This sequence rule is not supported.'}, status=400)
    group_id = str(sequence.get('groupId', sequence.get('group_id', '')) or '').strip()
    if group_id and len(group_id) > 100:
        return JsonResponse({'ok': False, 'error': 'Sequence name must be 100 characters or fewer.'}, status=400)
    raw_order = sequence.get('order', sequence.get('sequenceOrder'))
    if group_id:
        try:
            if isinstance(raw_order, bool) or int(raw_order) != float(raw_order) or not 1 <= int(raw_order) <= 20:
                raise ValueError
            order = int(raw_order)
        except (TypeError, ValueError, OverflowError):
            return JsonResponse({'ok': False, 'code': 'invalid_policy', 'error': 'Sequence step must be a whole number between 1 and 20.'}, status=400)
        enforce = sequence.get('enforce', sequence.get('enforceSequence', False))
        if not isinstance(enforce, bool):
            return JsonResponse({'ok': False, 'code': 'invalid_policy', 'field': 'sequence.enforce', 'error': 'This setting must be true or false.'}, status=400)
        if _sequence_position_conflicts(survey, group_id, order):
            return JsonResponse({'ok': False, 'code': 'invalid_policy', 'field': 'sequence.order', 'error': 'Another survey in this scope already uses that sequence step.'}, status=400)
        updated_sequence = {'groupId': group_id, 'order': order, 'enforce': enforce}
    else:
        updated_sequence = {}

    canonical_max_attempts = canonical_policy.get('maxAttempts')
    if canonical_max_attempts is None and not allow_redo:
        canonical_max_attempts = 1
    canonical_cooldown_seconds = round(float(canonical_policy.get('cooldownMinutes', 0)) * 60)
    canonical_document = {
        'schemaVersion': 1,
        'participation': {
            'accountRequired': canonical_policy['requiresAccount'],
            'organizationMembershipRequired': canonical_policy['organizationMembershipRequired'],
            'accessCodeRequired': access_code_required,
        },
        'attempts': {
            'maxAttempts': canonical_max_attempts,
            'cooldownSeconds': canonical_cooldown_seconds,
            'repeatReward': reward_strategy,
        },
        'prerequisites': [
            {'surveyId': survey_id, **({'unlockDelayMs': unlock_delay} if unlock_delay else {})}
            for survey_id in normalized_ids
        ],
        'sequence': updated_sequence or None,
    }
    try:
        validate_policy(canonical_document)
    except PolicyValidationError as exc:
        return JsonResponse({'ok': False, 'code': exc.code, 'field': exc.field, 'error': str(exc)}, status=400)

    features['accessPolicy'] = canonical_policy
    features.pop('access_policy', None)
    features['accessCodeRequired'] = access_code_required
    for unsupported_key in unsupported_score_keys:
        features.pop(unsupported_key, None)
    features['prerequisites'] = updated_prerequisites
    features.pop('prerequisiteSurveyIds', None)
    features.pop('prerequisite_survey_ids', None)
    if updated_sequence:
        features['sequence'] = updated_sequence
    else:
        features.pop('sequence', None)
        features.pop('sequenceGroup', None)
        features.pop('sequence_group', None)
        features.pop('sequenceOrder', None)
        features.pop('sequence_order', None)
        features.pop('enforceSequence', None)
    with transaction.atomic():
        survey.features = features
        survey.save(update_fields=['features'])
        _ensure_policy_revision(survey, request.user)
    return JsonResponse({'ok': True, 'uuid': str(survey.access_uuid), 'features': features})


@require_POST
def api_creator_toggle_publish(request, access_uuid):
    """Toggle survey between draft (private) and published (active/public)."""
    if not request.user.is_authenticated:
        return JsonResponse({'ok': False, 'error': 'Authentication required.'}, status=401)

    is_staff_or_admin = request.user.is_staff or request.user.is_superuser
    if not (request.user.role == 'creator' or _has_survey_manager_membership(request.user) or is_staff_or_admin):
        return JsonResponse({'ok': False, 'error': 'Creator permissions required.'}, status=403)

    survey = Survey.objects.filter(access_uuid=access_uuid).first()
    if not survey:
        return JsonResponse({'ok': False, 'error': 'Survey not found.'}, status=404)

    if not _can_manage_survey(request.user, survey):
        return JsonResponse({'ok': False, 'error': 'Not authorized to modify this survey.'}, status=403)

    try:
        data = json.loads(request.body) if request.body else {}
    except Exception:
        data = {}

    make_draft = data.get('is_draft', None)
    if make_draft is None:
        survey.is_draft = not survey.is_draft
    else:
        survey.is_draft = bool(make_draft)

    survey.is_active = not survey.is_draft
    survey.save(update_fields=['is_draft', 'is_active'])

    return JsonResponse({
        'ok': True,
        'uuid': str(survey.access_uuid),
        'accessCode': survey.access_code,
        'isDraft': survey.is_draft,
        'isActive': survey.is_active,
        'status': 'draft' if survey.is_draft else 'published',
    })


@require_GET
def api_openapi_spec(request):
    """Dynamic OpenAPI 3.1.0 specification for Gihini Research & Feedora API."""
    spec = {
        'openapi': '3.1.0',
        'info': {
            'title': 'Gihini Research & Feedora Survey Gateway API',
            'version': '1.0.0',
            'description': 'Unified REST contract connecting the Django Research backend with mobile and web clients.',
        },
        'servers': [
            {'url': 'http://127.0.0.1:8000', 'description': 'Local Dev Server'},
            {'url': 'http://10.10.66.76:8000', 'description': 'Local LAN Server (for Mobile Expo Go)'},
        ],
        'paths': {
            '/api/v1/auth/login/': {
                'post': {
                    'summary': 'Authenticate user via JSON',
                    'description': 'Accepts username or email, authenticates against Django auth, and returns user context with role.',
                    'requestBody': {
                        'required': True,
                        'content': {
                            'application/json': {
                                'schema': {
                                    'type': 'object',
                                    'properties': {
                                        'identity': {'type': 'string', 'example': 'Gihiniw'},
                                        'password': {'type': 'string', 'example': 'feedora123'},
                                        'persona': {'type': 'string', 'enum': ['respondent', 'creator'], 'example': 'creator'},
                                    },
                                    'required': ['identity', 'password'],
                                },
                            },
                        },
                    },
                    'responses': {
                        '200': {'description': 'Authentication successful'},
                        '401': {'description': 'Invalid credentials'},
                        '403': {'description': 'Role permission mismatch'},
                    },
                },
            },
            '/api/v1/auth/me/': {
                'get': {
                    'summary': 'Get current global account and active organization memberships',
                    'responses': {'200': {'description': 'Session state; user.organizations contains all active memberships and user.organization is only populated when unambiguous'}},
                },
            },
            '/api/v1/entities/': {
                'get': {
                    'summary': 'List public organization and platform scopes',
                    'responses': {'200': {'description': 'Available entity scopes'}},
                },
            },
            '/api/v1/entities/create/': {
                'post': {
                    'summary': 'Create an organization scope and its initial owner membership',
                    'description': 'Requires an authenticated creator account. Organization ownership is represented by active memberships; created_by is provenance only.',
                    'requestBody': {
                        'required': True,
                        'content': {
                            'application/json': {
                                'schema': {
                                    'type': 'object',
                                    'properties': {
                                        'name': {'type': 'string', 'maxLength': 200},
                                        'type': {'type': 'string', 'enum': ['university', 'school', 'company', 'research', 'other']},
                                        'country': {'type': 'string', 'maxLength': 100},
                                        'userRole': {'type': 'string', 'maxLength': 100},
                                    },
                                    'required': ['name'],
                                },
                            },
                        },
                    },
                    'responses': {
                        '201': {'description': 'Organization created with the caller as an active owner'},
                        '401': {'description': 'Authentication required'},
                        '403': {'description': 'Creator permission required'},
                    },
                },
            },
            '/api/v1/auth/logout/': {
                'post': {
                    'summary': 'Terminate current session',
                    'responses': {'200': {'description': 'Logged out'}},
                },
            },
            '/api/v1/surveys/': {
                'get': {
                    'summary': 'List available public surveys',
                    'responses': {'200': {'description': 'List of active surveys'}},
                },
            },
            '/api/v1/surveys/mine/': {
                'get': {
                    'summary': 'List creator surveys with question and response metrics',
                    'responses': {'200': {'description': 'List of owned surveys'}},
                },
            },
            '/api/v1/surveys/create/': {
                'post': {
                    'summary': 'Scaffold and publish or draft a new survey',
                    'requestBody': {
                        'required': True,
                        'content': {
                            'application/json': {
                                'schema': {
                                    'type': 'object',
                                    'properties': {
                                        'title': {'type': 'string'},
                                        'description': {'type': 'string'},
                                        'is_hmsam': {'type': 'boolean'},
                                        'organizationId': {'type': 'string', 'description': 'Explicit organization scope. Required when the creator actively owns multiple organizations.'},
                                        'questions': {'type': 'array'},
                                    },
                                    'required': ['title'],
                                },
                            },
                        },
                    },
                    'responses': {'201': {'description': 'Survey created'}},
                },
            },
            '/api/v1/surveys/{uuid}/meta/': {
                'get': {
                    'summary': 'Fetch survey metadata & questions',
                    'parameters': [
                        {'name': 'uuid', 'in': 'path', 'required': True, 'schema': {'type': 'string'}},
                    ],
                    'responses': {'200': {'description': 'Survey JSON schema and questions'}},
                },
            },
            '/api/v1/surveys/{uuid}/submit/': {
                'post': {
                    'summary': 'Submit respondent survey answers via JSON',
                    'parameters': [
                        {'name': 'uuid', 'in': 'path', 'required': True, 'schema': {'type': 'string'}},
                    ],
                    'requestBody': {
                        'required': True,
                        'content': {
                            'application/json': {
                                'schema': {
                                    'type': 'object',
                                    'properties': {
                                        'answers': {'type': 'object'},
                                    },
                                    'required': ['answers'],
                                },
                            },
                        },
                    },
                    'responses': {'200': {'description': 'Submission accepted with points awarded'}},
                },
            },
        },
    }
    return JsonResponse(spec)


@require_GET
def api_swagger_docs(request):
    """Serve embedded Swagger UI for interactive API exploration."""
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Gihini Research API - Swagger UI</title>
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css" />
  <style>
    body { margin: 0; padding: 0; background: #fafaf7; }
    .topbar { display: none; }
  </style>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    window.onload = function() {
      SwaggerUIBundle({
        url: "/api/v1/openapi.json",
        dom_id: '#swagger-ui',
        deepLinking: true,
        presets: [
          SwaggerUIBundle.presets.apis,
          SwaggerUIBundle.SwaggerUIStandalonePreset
        ],
        layout: "BaseLayout"
      });
    };
  </script>
</body>
</html>
"""
    return HttpResponse(html, content_type='text/html')


@require_GET
def api_survey_analysis(request, access_uuid):
    """
    Server-side analysis API (Phase 4 / Defect 6 & Task 4.3):
    Returns per-construct scores, Cronbach's alpha per construct,
    and comparative statistics between experimental arms (gamified vs control).
    """
    try:
        survey = Survey.objects.get(access_uuid=access_uuid, is_active=True)
    except Survey.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'Survey not found'}, status=404)

    if not request.user.is_authenticated:
        return JsonResponse({'ok': False, 'error': 'Authentication required.'}, status=401)
    is_staff_or_admin = request.user.is_staff or request.user.is_superuser
    if not (request.user.role == 'creator' or _has_survey_manager_membership(request.user) or is_staff_or_admin):
        return JsonResponse({'ok': False, 'error': 'Creator permissions required.'}, status=403)
    if not _can_manage_survey(request.user, survey):
        return JsonResponse({'ok': False, 'error': 'You do not own this survey.'}, status=403)

    from surveys.analytics import (
        get_hmsam_scores,
        cronbach_alpha,
        get_hmsam_construct_reliabilities,
        get_experimental_arm_analysis,
    )

    total_responses = Response.objects.filter(survey=survey).count()
    overall_scores, overall_adoption = get_hmsam_scores(survey)
    overall_alpha, alpha_interp = cronbach_alpha(survey)
    construct_alphas = get_hmsam_construct_reliabilities(survey)
    arm_analysis = get_experimental_arm_analysis(survey)

    return JsonResponse({
        'ok': True,
        'survey': {
            'id': survey.id,
            'uuid': str(survey.access_uuid),
            'title': survey.title,
            'isHmsam': survey.is_hmsam,
        },
        'totalResponses': total_responses,
        'overallAdoption': overall_adoption,
        'overallAlpha': {
            'value': overall_alpha,
            'interpretation': alpha_interp,
        },
        'constructScores': overall_scores,
        'constructReliabilities': construct_alphas,
        'experimentalComparison': arm_analysis,
    })


@require_GET
def api_survey_export(request, access_uuid):
    """
    Download research dataset formatted for SmartPLS / R seminr with pseudonymous respondent IDs.
    """
    try:
        survey = Survey.objects.get(access_uuid=access_uuid, is_active=True)
    except Survey.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'Survey not found'}, status=404)

    if not request.user.is_authenticated:
        return JsonResponse({'ok': False, 'error': 'Authentication required.'}, status=401)
    is_staff_or_admin = request.user.is_staff or request.user.is_superuser
    if not (request.user.role == 'creator' or _has_survey_manager_membership(request.user) or is_staff_or_admin):
        return JsonResponse({'ok': False, 'error': 'Creator permissions required.'}, status=403)
    if not _can_manage_survey(request.user, survey):
        return JsonResponse({'ok': False, 'error': 'You do not own this survey.'}, status=403)

    from surveys.analytics import csv_export
    csv_data = csv_export(survey, pseudonymize=True)
    response = HttpResponse(csv_data, content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="hmsam_survey_{access_uuid}_export.csv"'
    return response
