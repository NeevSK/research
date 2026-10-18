import json
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST, require_GET
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.middleware.csrf import get_token
from django.contrib.auth import authenticate, login, logout, get_user_model
from django.db import transaction
from django.db.models import F, Sum
from surveys.models import Survey, Response, Answer, Question, MCQOption
from accounts.models import Organization, CreatorProfile, RespondentProfile
from surveys.validation import validate_survey_submission
from surveys.points import calculate_question_points

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

    return JsonResponse({
        'ok': True,
        'totalPoints': total_points,
        'levelInfo': get_level_info(total_points),
        'submissionsCount': len(history_items),
        'history': history_items,
        'pointsByOrganization': sorted_org_points,
    })


@require_GET
def api_surveys_list(request):
    """List active surveys available for participation."""
    surveys = Survey.objects.filter(is_active=True).order_by('-created_at')[:50]
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
    return JsonResponse({'ok': True, 'surveys': items})


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

    return {
        'ok': True,
        'csrfToken': csrf_token,
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
        'features': survey.features or {},
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
        'pointsEarned': total_points,
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
        'questionsCount': survey.questions.count(),
    }, status=201)



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
