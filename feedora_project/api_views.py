import json
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST, require_GET
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.middleware.csrf import get_token
from django.contrib.auth import authenticate, login, logout, get_user_model
from django.db import transaction
from django.db.models import F, Sum
from surveys.models import Survey, Response, Answer, Question, MCQOption, Experiment, Assignment
from accounts.models import Organization, CreatorProfile, RespondentProfile
from surveys.validation import validate_survey_submission
from surveys.points import (
    calculate_question_points,
    calculate_variable_reward,
    evaluate_negative_reinforcement,
)

User = get_user_model()


def _serialize_user(user, persona=None):
    """Serialize Django User instance with role, organization, and live score context."""
    resolved_persona = persona or getattr(user, 'role', None)
    if not resolved_persona:
        resolved_persona = 'creator' if (user.is_staff or user.is_superuser) else 'respondent'

    display_name = user.get_full_name() or user.username
    org_data = None
    avatar = '🔬' if resolved_persona == 'creator' else '🎙️'
    total_points = 0
    surveys_completed = 0
    level_info = None

    try:
        if hasattr(user, 'organization'):
            org = user.organization
            org_data = {
                'id': str(org.id),
                'name': org.org_name or 'Default Organization',
                'type': org.org_type or 'university',
                'userRole': org.user_role or '',
                'country': org.country or '',
            }
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
        surveys_completed = Response.objects.filter(user=user).count()
        if total_points == 0:
            from django.db.models import Sum
            pts_agg = Response.objects.filter(user=user).aggregate(Sum('points_earned'))['points_earned__sum']
            if pts_agg:
                total_points = pts_agg
        from accounts.utils import get_level_info
        level_info = get_level_info(total_points)
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
        'surveysCompleted': surveys_completed,
        'badges': badges,
        'levelInfo': level_info,
        'organization': org_data,
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

            org_name = str(payload.get('organizationName') or payload.get('org_name') or 'Research Lab').strip()
            org_type = str(payload.get('organizationType') or payload.get('org_type') or 'university').strip()
            user_role = str(payload.get('userRoleInOrg') or payload.get('user_role') or 'Principal Investigator').strip()
            country = str(payload.get('country') or 'Sri Lanka').strip()
            bio = str(payload.get('bio') or '').strip()

            Organization.objects.create(
                user=user,
                org_name=org_name,
                org_type=org_type,
                user_role=user_role,
                country=country,
            )
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
    """Return historical submissions and points grouped by organization for authenticated respondent."""
    if not request.user.is_authenticated:
        return JsonResponse({'ok': False, 'error': 'Authentication required.'}, status=401)

    from surveys.models import Response
    from accounts.utils import get_level_info
    from django.db.models import Sum

    user = request.user
    responses_qs = Response.objects.filter(user=user).select_related('survey', 'survey__organization').order_by('-completed_at')

    total_points = responses_qs.aggregate(t=Sum('points_earned'))['t'] or 0

    points_by_org = {}
    history_items = []
    for r in responses_qs:
        survey = r.survey
        org_user = survey.organization
        org_name = 'Independent Research'
        if hasattr(org_user, 'organization') and org_user.organization.org_name:
            org_name = org_user.organization.org_name
        elif hasattr(org_user, 'creator_profile') and org_user.creator_profile.company:
            org_name = org_user.creator_profile.company
        elif org_user.first_name or org_user.last_name:
            org_name = f"{org_user.first_name} {org_user.last_name}".strip()

        points_by_org[org_name] = points_by_org.get(org_name, 0) + r.points_earned

        history_items.append({
            'responseId': r.id,
            'surveyTitle': survey.title,
            'surveyUuid': str(survey.access_uuid),
            'accessCode': survey.access_code,
            'isHmsam': survey.is_hmsam,
            'organization': org_name,
            'pointsEarned': r.points_earned,
            'completedAt': r.completed_at.isoformat(),
        })

    sorted_org_points = [
        {'organization': k, 'points': v}
        for k, v in sorted(points_by_org.items(), key=lambda x: x[1], reverse=True)
    ]

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
        'levelInfo': get_level_info(total_points),
        'submissionsCount': submissions_count,
        'badges': badges,
        'history': history_items,
        'pointsByOrganization': sorted_org_points,
    })


@require_GET
def api_entities_list(request):
    """List organizations and institutions hosting surveys on Feedora."""
    from accounts.models import Organization

    orgs = Organization.objects.select_related('user').all()
    entities = []
    seen_user_ids = set()

    for org in orgs:
        seen_user_ids.add(org.user_id)
        active_surveys_count = Survey.objects.filter(organization=org.user, is_active=True).count()
        entities.append({
            'id': org.id,
            'entityId': f"org-{org.id}",
            'name': org.org_name or f"{org.user.email}'s Lab",
            'type': org.org_type or 'university',
            'userRole': org.user_role or '',
            'country': org.country or '',
            'creatorEmail': org.user.email,
            'activeSurveysCount': active_surveys_count,
        })

    creators_with_surveys = User.objects.filter(
        surveys__is_active=True
    ).exclude(id__in=seen_user_ids).distinct()

    for u in creators_with_surveys:
        org_name = 'Independent Research'
        if hasattr(u, 'creator_profile') and u.creator_profile.company:
            org_name = u.creator_profile.company
        elif u.first_name or u.last_name:
            org_name = f"{u.first_name} {u.last_name}".strip()

        active_count = Survey.objects.filter(organization=u, is_active=True).count()
        entities.append({
            'id': u.id,
            'entityId': f"user-{u.id}",
            'name': org_name,
            'type': 'research',
            'userRole': 'Principal Investigator',
            'country': '',
            'creatorEmail': u.email,
            'activeSurveysCount': active_count,
        })

    return JsonResponse({'ok': True, 'entities': entities})


@require_GET
def api_entity_surveys(request, entity_id):
    """List active surveys published by a specific organization or creator."""
    from accounts.models import Organization

    target_user = None
    org_obj = Organization.objects.filter(id=entity_id).select_related('user').first()
    if org_obj:
        target_user = org_obj.user
        org_name = org_obj.org_name
    else:
        user_candidate = User.objects.filter(id=entity_id).first()
        if user_candidate:
            target_user = user_candidate
            org_name = (
                user_candidate.creator_profile.company
                if hasattr(user_candidate, 'creator_profile') and user_candidate.creator_profile.company
                else user_candidate.email
            )
        else:
            return JsonResponse({'ok': False, 'error': 'Entity not found.'}, status=404)

    surveys = Survey.objects.filter(organization=target_user, is_active=True).order_by('-created_at')
    items = []
    for s in surveys:
        items.append({
            'id': s.id,
            'uuid': str(s.access_uuid),
            'accessCode': s.access_code,
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
    """Associate authenticated respondent with an organization/entity."""
    if not request.user.is_authenticated:
        return JsonResponse({'ok': False, 'error': 'Authentication required.'}, status=401)

    from accounts.models import Organization, RespondentProfile
    target_org = Organization.objects.filter(id=entity_id).first()
    org_name = target_org.org_name if target_org else 'Independent'
    org_type = target_org.org_type if target_org else 'university'
    country = target_org.country if target_org else ''

    user_org, _ = Organization.objects.get_or_create(user=request.user)
    user_org.org_name = org_name
    user_org.org_type = org_type
    user_org.country = country
    user_org.save()

    profile, _ = RespondentProfile.objects.get_or_create(user=request.user)
    profile.company = org_name
    profile.save(update_fields=['company'])

    return JsonResponse({
        'ok': True,
        'message': f'Successfully joined {org_name}.',
        'organization': {
            'orgName': user_org.org_name,
            'orgType': user_org.org_type,
            'country': user_org.country,
        },
    })


@require_GET
def api_surveys_list(request):
    """List active surveys available for participation, enriched with archetypes and access policies."""
    surveys = Survey.objects.filter(is_active=True).order_by('-created_at')[:50]
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

        allow_multi = bool(s.features.get('allow_multiple_submissions', False))
        if kind == 'quiz':
            allow_redo = True
            reward_strat = 'score_delta'
        elif kind == 'survey':
            allow_redo = allow_multi
            reward_strat = 'first_attempt_only'
        else:
            allow_redo = allow_multi
            reward_strat = 'first_attempt_only'

        items.append({
            'id': s.id,
            'uuid': str(s.access_uuid),
            'accessCode': s.access_code,
            'title': s.title,
            'description': s.description or '',
            'isHmsam': s.is_hmsam,
            'kind': kind,
            'accessPolicy': {
                'allowRedo': allow_redo,
                'maxAttempts': 1 if not allow_redo else None,
                'rewardStrategy': reward_strat,
            },
            'questionsCount': s.questions.count(),
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
            'title': survey.title,
            'description': survey.description,
            'isHmsam': survey.is_hmsam,
            'accessCode': survey.access_code,
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
        survey = Survey.objects.get(access_uuid=access_uuid, is_active=True)
    except Survey.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'Survey not found'}, status=404)

    return JsonResponse(_serialize_survey_meta(survey, request=request))


@require_GET
def api_survey_meta_by_code(request, access_code):
    """Fetch survey metadata and ordered questions by 6-char access code."""
    try:
        survey = Survey.objects.get(access_code=access_code.strip().upper(), is_active=True)
    except Survey.DoesNotExist:
        return JsonResponse({'ok': False, 'error': f'Survey with access code {access_code} not found'}, status=404)

    return JsonResponse(_serialize_survey_meta(survey, request=request))


@require_POST
def api_survey_submit(request, access_uuid):
    """Submit respondent survey answers via JSON API and award points."""
    try:
        survey = Survey.objects.get(access_uuid=access_uuid, is_active=True)
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
    user = request.user if request.user.is_authenticated else None
    submission_id = str(payload.get('submissionId') or payload.get('submission_id') or '').strip() or None

    # Idempotent re-entry check via client submissionId
    if submission_id:
        existing_resp = Response.objects.filter(submission_id=submission_id).first()
        if existing_resp:
            user_data = _serialize_user(user) if user else None
            return JsonResponse({
                'ok': True,
                'idempotent': True,
                'responseId': existing_resp.id,
                'submissionId': str(submission_id),
                'pointsEarned': existing_resp.points_earned,
                'surveyUuid': str(survey.access_uuid),
                'totalPoints': user_data['totalPoints'] if user_data else existing_resp.points_earned,
                'user': user_data,
            }, status=200)

    # Configurable duplicate submission guard for authenticated participants
    allow_multi_submit = bool(survey.features.get('allow_multiple_submissions', False))
    if not allow_multi_submit and user and Response.objects.filter(survey=survey, user=user).exists():
        return JsonResponse({
            'ok': False,
            'error': 'User has already submitted a response to this survey'
        }, status=409)

    # Validate submission invariants against survey definition
    is_valid, validation_errors = validate_survey_submission(survey, raw_answers)
    if not is_valid:
        return JsonResponse({
            'ok': False,
            'error': 'Validation failed',
            'errors': validation_errors,
        }, status=400)

    import time
    import random
    from django.db import OperationalError
    from django.utils import timezone

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

    max_db_attempts = 10
    for db_attempt in range(max_db_attempts):
        try:
            with transaction.atomic():
                response_obj = Response.objects.create(
                    survey=survey,
                    user=user,
                    submission_id=submission_id,
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

                # Optional Variable Rewards bonus (Skinnerian Variable-Ratio)
                variable_bonus, multiplier, bonus_label = 0, 1.0, None
                if variant != 'control' and total_points > 0:
                    variable_bonus, multiplier, bonus_label = calculate_variable_reward(total_points, survey.features)
                    total_points += variable_bonus

                # Optional Negative Reinforcement perks (Friction Removal & Streak Shielding)
                responses_count = Response.objects.filter(survey=survey, user=user).count() + 1 if user else 1
                nr_eval = evaluate_negative_reinforcement(
                    survey.features,
                    responses_count=responses_count,
                    completed_clean=len(quality_flags) == 0
                )

                response_obj.points_earned = total_points
                response_obj.save(update_fields=['points_earned'])

                # Increment respondent profile total points atomically via database F() expression
                if user:
                    profile, _ = RespondentProfile.objects.get_or_create(user=user)
                    if total_points > 0:
                        RespondentProfile.objects.filter(pk=profile.pk).update(
                            total_points=F('total_points') + total_points
                        )
                        profile.refresh_from_db()
            break
        except OperationalError as db_err:
            if 'locked' in str(db_err).lower() and db_attempt < max_db_attempts - 1:
                time.sleep(0.05 * (1.5 ** db_attempt) + random.uniform(0.02, 0.08))
                continue
            raise

    user_data = _serialize_user(user) if user else None
    return JsonResponse({
        'ok': True,
        'responseId': response_obj.id,
        'submissionId': submission_id,
        'variant': variant,
        'arm': variant,
        'platform': platform,
        'qualityFlags': response_obj.quality_flags,
        'pointsEarned': total_points,
        'variableBonus': variable_bonus,
        'bonusLabel': bonus_label,
        'negativeReinforcement': nr_eval,
        'surveyUuid': str(survey.access_uuid),
        'totalPoints': user_data['totalPoints'] if user_data else total_points,
        'user': user_data,
    })


@require_GET
def api_creator_surveys(request):
    """List surveys owned by the authenticated creator or all surveys for superuser/admin."""
    if not request.user.is_authenticated:
        return JsonResponse({'ok': False, 'error': 'Authentication required.'}, status=401)

    is_staff_or_admin = request.user.is_staff or request.user.is_superuser
    if not (request.user.role == 'creator' or is_staff_or_admin):
        return JsonResponse({'ok': False, 'error': 'Creator permissions required.'}, status=403)

    if is_staff_or_admin:
        surveys = Survey.objects.all().order_by('-created_at')
    else:
        surveys = Survey.objects.filter(organization=request.user).order_by('-created_at')

    items = []
    for s in surveys:
        items.append({
            'id': s.id,
            'uuid': str(s.access_uuid),
            'accessCode': s.access_code,
            'title': s.title,
            'description': s.description or '',
            'isHmsam': s.is_hmsam,
            'isActive': s.is_active,
            'isDraft': s.is_draft,
            'questionsCount': s.questions.count(),
            'responsesCount': s.responses.count(),
            'createdAt': s.created_at.isoformat(),
        })

    return JsonResponse({'ok': True, 'surveys': items})


@require_POST
def api_creator_create_survey(request):
    """Scaffold and publish or draft a new survey from mobile/web Studio."""
    if not request.user.is_authenticated:
        return JsonResponse({'ok': False, 'error': 'Authentication required.'}, status=401)

    is_staff_or_admin = request.user.is_staff or request.user.is_superuser
    if not (request.user.role == 'creator' or is_staff_or_admin):
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

    with transaction.atomic():
        survey = Survey.objects.create(
            organization=request.user,
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
            features=data.get('features', {})
        )

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
def api_creator_toggle_publish(request, access_uuid):
    """Toggle survey between draft (private) and published (active/public)."""
    if not request.user.is_authenticated:
        return JsonResponse({'ok': False, 'error': 'Authentication required.'}, status=401)

    is_staff_or_admin = request.user.is_staff or request.user.is_superuser
    if not (request.user.role == 'creator' or is_staff_or_admin):
        return JsonResponse({'ok': False, 'error': 'Creator permissions required.'}, status=403)

    survey = Survey.objects.filter(access_uuid=access_uuid).first()
    if not survey:
        return JsonResponse({'ok': False, 'error': 'Survey not found.'}, status=404)

    if not is_staff_or_admin and survey.organization != request.user:
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
                    'summary': 'Get current session profile',
                    'responses': {'200': {'description': 'Session state'}},
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

    from surveys.analytics import csv_export
    csv_data = csv_export(survey, pseudonymize=True)
    response = HttpResponse(csv_data, content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="hmsam_survey_{access_uuid}_export.csv"'
    return response
