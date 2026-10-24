import random
from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login
from django.contrib.auth.hashers import make_password
from django.core.mail import send_mail
from .models import User, Organization, OrganizationMembership, EmailVerificationCode, CreatorProfile, RespondentProfile
from django.db.models import Sum, Count
from surveys.models import Survey, Response
from .utils import get_level_info
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from .models import Organization, CreatorProfile, RespondentProfile
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.views.decorators.cache import never_cache


# ── Role selection ─────────────────────────────────────────

@never_cache
def role_select_login(request):
    return render(request, 'accounts/role_select.html', {'mode': 'login'})

@never_cache
def role_select_register(request):
    return render(request, 'accounts/role_select.html', {'mode': 'register'})


# ── Login ──────────────────────────────────────────────────
@never_cache
def login_creator(request):
    return _login_view(request, role='creator')

@never_cache
def login_respondent(request):
    return _login_view(request, role='respondent')

def _login_view(request, role):
    error = None
    if request.method == 'POST':
        email    = request.POST.get('email', '').lower().strip()
        password = request.POST.get('password', '')
        user = authenticate(request, username=email, password=password)
        if user and user.role == role:
            login(request, user)
            return redirect('dashboard' if role == 'creator' else 'respondent_home')
        error = 'Invalid email or password.'
    return render(request, 'accounts/login.html', {'role': role, 'error': error})


# ── Registration ───────────────────────────────────────────

def register_respondent(request):
    error = None
    if request.method == 'POST':
        email    = request.POST.get('email', '').lower().strip()
        password = request.POST.get('password', '')
        repeat   = request.POST.get('repeat_password', '')

        if password != repeat:
            error = 'Passwords do not match.'
        elif User.objects.filter(username=email).exists():
            error = 'An account with this email already exists.'
        else:
            user = User.objects.create(
                username=email,
                email=email,
                password=make_password(password),
                role=User.RESPONDENT,
                is_active=False,   # stays inactive until email verified
            )
            _send_verification_code(user)
            request.session['verify_user_id'] = user.id
            return redirect('verify_email')

    return render(request, 'accounts/register_respondent.html', {'error': error})


def register_creator(request):
    error = None
    form  = {}
    if request.method == 'POST':
        form = request.POST
        email    = request.POST.get('email', '').lower().strip()
        password = request.POST.get('password', '')
        repeat   = request.POST.get('repeat_password', '')

        if password != repeat:
            error = 'Passwords do not match.'
        elif User.objects.filter(username=email).exists():
            error = 'An account with this email already exists.'
        elif not request.POST.get('terms'):
            error = 'You must agree to the Terms of Service.'
        else:
            user = User.objects.create(
                username=email,
                email=email,
                password=make_password(password),
                first_name=request.POST.get('first_name', ''),
                last_name=request.POST.get('last_name', ''),
                role=User.CREATOR,
                is_active=False,
            )
            Organization.objects.create(
                created_by=user,
                org_name=request.POST.get('org_name', ''),
                org_type=request.POST.get('org_type', ''),
                user_role=request.POST.get('user_role', ''),
                country=request.POST.get('country', ''),
            )
            _send_verification_code(user)
            request.session['verify_user_id'] = user.id
            return redirect('verify_email')

    return render(request, 'accounts/register_creator.html', {'error': error, 'form': form})


# ── Email verification ─────────────────────────────────────

def verify_email(request):
    user_id = request.session.get('verify_user_id')
    if not user_id:
        return redirect('role_select_login')

    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return redirect('role_select_login')

    error = None
    if request.method == 'POST':
        # Join the 6 individual digit inputs into one string
        entered = ''.join([request.POST.get(f'c{i}', '') for i in range(1, 7)])

        try:
            record = EmailVerificationCode.objects.get(user=user)
            if record.is_expired():
                error = 'Code expired. Click resend to get a new one.'
            elif record.code == entered:
                user.is_active = True
                user.save()
                record.delete()
                del request.session['verify_user_id']
                login(request, user,
                      backend='django.contrib.auth.backends.ModelBackend')
                return redirect('dashboard' if user.role == User.CREATOR else 'respondent_home')
            else:
                error = 'Incorrect code. Please try again.'
        except EmailVerificationCode.DoesNotExist:
            error = 'No code found. Please request a new one.'

    return render(request, 'accounts/verify_email.html', {
        'email':     user.email,
        'error':     error,
        'user_role': user.role,
    })


def resend_code(request):
    user_id = request.session.get('verify_user_id')
    if user_id:
        try:
            user = User.objects.get(id=user_id)
            _send_verification_code(user)
        except User.DoesNotExist:
            pass
    return redirect('verify_email')


# ── Helper ─────────────────────────────────────────────────

def _send_verification_code(user):
    code = str(random.randint(100000, 999999))
    EmailVerificationCode.objects.update_or_create(
        user=user,
        defaults={'code': code}
    )
    send_mail(
        subject='Your Hoops verification code',
        message=f'Your verification code is: {code}\n\nThis code expires in 10 minutes.',
        from_email='noreply@hoops.com',
        recipient_list=[user.email],
    )

#Placeholder views for post-login pages - a temporary solution

from django.contrib.auth.decorators import login_required

@login_required
@never_cache
def dashboard(request):
    return render(request, 'accounts/creator_home.html')

@login_required
@never_cache
def respondent_home(request):
    return render(request, 'accounts/respondent_profile.html')


@login_required
@never_cache
def creator_profile(request):
    profile, _ = CreatorProfile.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        profile.display_name = request.POST.get('display_name', '').strip()
        profile.company      = request.POST.get('company', '').strip()
        profile.bio          = request.POST.get('bio', '').strip()
        profile.website      = request.POST.get('website', '').strip()
        profile.avatar       = request.POST.get('avatar', 'default')
        profile.save()
        return redirect('creator_profile')

    surveys = Survey.objects.filter(
        created_by=request.user
    ).annotate(resp_count=Count('responses')).order_by('-created_at')

    total_responses = sum(s.resp_count for s in surveys)

    return render(request, 'accounts/creator_profile.html', {
        'profile':         profile,
        'surveys':         surveys,
        'survey_count':    surveys.count(),
        'total_responses': total_responses,
    })


@login_required
@never_cache
def respondent_profile(request):
    profile, _ = RespondentProfile.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        profile.display_name = request.POST.get('display_name', '').strip()
        profile.company      = request.POST.get('company', '').strip()
        profile.gender       = request.POST.get('gender', '')
        profile.age_group    = request.POST.get('age_group', '')
        profile.avatar       = request.POST.get('avatar', 'default')
        profile.save()
        return redirect('respondent_profile')

    # Recalculate total points from responses
    total = Response.objects.filter(
        user=request.user
    ).aggregate(t=Sum('points_earned'))['t'] or 0

    if profile.total_points != total:
        profile.total_points = total
        profile.save(update_fields=['total_points'])

    responses = Response.objects.filter(
        user=request.user
    ).select_related('survey').order_by('-completed_at')

    # Points grouped by the creator's company
    points_by_company = {}
    for r in responses:
        try:
            company = r.survey.created_by.creator_profile.company or 'Independent'
        except CreatorProfile.DoesNotExist:
            company = 'Independent'
        points_by_company[company] = points_by_company.get(company, 0) + r.points_earned

    points_by_company = sorted(points_by_company.items(),
                               key=lambda x: x[1], reverse=True)

    return render(request, 'accounts/respondent_profile.html', {
        'profile':           profile,
        'responses':         responses,
        'response_count':    responses.count(),
        'points_by_company': points_by_company,
        'level':             get_level_info(total),
    })


@login_required
@never_cache
def my_profile(request):
    """
    Single entry point. Picks the creator or respondent template based on
    request.user.role, and passes the related Organization + role profile.
    Uses first()/get-or-None so missing related rows don't 500 the page.
    """
    user = request.user

    # Organization is a OneToOne but may not exist yet — fetch safely.
    organization = Organization.objects.filter(
        memberships__user=user,
        memberships__role=OrganizationMembership.OWNER,
        memberships__status=OrganizationMembership.ACTIVE,
    ).first()

    if user.role == User.RESPONDENT:
        profile = RespondentProfile.objects.filter(user=user).first()
        return render(request, 'accounts/respondent_profile.html', {
            'user':         user,
            'organization': organization,
            'profile':      profile,
        })
    else:
        # default to creator view for creators (or unset role)
        profile = CreatorProfile.objects.filter(user=user).first()
        return render(request, 'accounts/creator_profile.html', {
            'user':         user,
            'organization': organization,
            'profile':      profile,
        })


def logout_view(request):
    logout(request)
    return redirect('login_creator')
