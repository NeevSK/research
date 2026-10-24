from django.contrib.auth.models import AbstractUser
from django.conf import settings
from django.db import models
import uuid


class User(AbstractUser):
    CREATOR    = 'creator'
    RESPONDENT = 'respondent'
    ROLE_CHOICES = [
        (CREATOR,    'Form Creator'),
        (RESPONDENT, 'Form Respondent'),
    ]
    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        null=True,
        blank=True
    )

    def __str__(self):
        return self.email


class Organization(models.Model):
    ORG_TYPES = [
        ('university', 'University'),
        ('school',     'School'),
        ('company',    'Company'),
        ('research',   'Research Institute'),
        ('other',      'Other'),
    ]
    # Provenance only: membership rows determine who currently owns/manages an
    # organization. This relation is intentionally non-unique so one account may
    # create multiple organizations without becoming their authorization source.
    created_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='created_organizations',
    )
    org_name  = models.CharField(max_length=200, blank=True)
    org_type  = models.CharField(max_length=50, choices=ORG_TYPES, blank=True)
    user_role = models.CharField(max_length=100, blank=True)
    country   = models.CharField(max_length=100, blank=True)
    # Sharing defaults on and is limited to aggregate completion counts. It never
    # transfers respondent identities, answers, individual marks, or reward points.
    share_aggregate_stats_to_platform = models.BooleanField(default=True)
    aggregate_stats_approved_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.org_name or f"Organization {self.pk}"


class OrganizationMembership(models.Model):
    """One user's role and membership status within one organization."""
    OWNER = 'owner'
    ADMIN = 'admin'
    MEMBER = 'member'
    ROLE_CHOICES = [
        (OWNER, 'Owner'),
        (ADMIN, 'Admin'),
        (MEMBER, 'Member'),
    ]
    # Owners retain organization-wide authority. Admins can manage surveys in
    # their organization; membership and participant policy administration is
    # intentionally a later capability.
    SURVEY_MANAGER_ROLES = (OWNER, ADMIN)
    ACTIVE = 'active'
    LEFT = 'left'
    STATUS_CHOICES = [
        (ACTIVE, 'Active'),
        (LEFT, 'Left'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='organization_memberships',
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='memberships',
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=MEMBER)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=ACTIVE)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'organization'],
                name='unique_organization_membership',
            ),
        ]
        ordering = ['organization__org_name', 'joined_at']

    def __str__(self):
        return f"{self.user} — {self.organization} ({self.role})"


class GuestPrincipal(models.Model):
    """Server-issued identity for anonymous participation. Only the token hash is stored."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token_hash = models.CharField(max_length=64, unique=True)
    claimed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='claimed_guest_principals',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    claimed_at = models.DateTimeField(null=True, blank=True)


class EmailVerificationCode(models.Model):
    user       = models.OneToOneField(User, on_delete=models.CASCADE)
    code       = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)

    def is_expired(self):
        from django.utils import timezone
        return (timezone.now() - self.created_at).seconds > 600  # 10 minutes

    def __str__(self):
        return f"Code for {self.user.email}"
    

class CreatorProfile(models.Model):
    user         = models.OneToOneField(User, on_delete=models.CASCADE,
                                        related_name='creator_profile')
    display_name = models.CharField(max_length=100, blank=True)
    company      = models.CharField(max_length=100, blank=True)
    bio          = models.TextField(blank=True)
    website      = models.URLField(blank=True)
    avatar       = models.CharField(max_length=50, default='default')
    created_at   = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.email} — Creator"


class RespondentProfile(models.Model):
    GENDER_CHOICES = [
        ('M',  'Male'),
        ('F',  'Female'),
        ('NB', 'Non-binary'),
        ('NS', 'Prefer not to say'),
    ]
    AGE_CHOICES = [
        ('under18', 'Under 18'),
        ('18-24',   '18–24'),
        ('25-34',   '25–34'),
        ('35-44',   '35–44'),
        ('45+',     '45+'),
    ]
    user         = models.OneToOneField(User, on_delete=models.CASCADE,
                                        related_name='respondent_profile')
    display_name = models.CharField(max_length=100, blank=True)
    company      = models.CharField(max_length=100, blank=True)
    gender       = models.CharField(max_length=5, choices=GENDER_CHOICES, blank=True)
    age_group    = models.CharField(max_length=10, choices=AGE_CHOICES, blank=True)
    avatar       = models.CharField(max_length=50, default='default')
    total_points = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.user.email} — Respondent"
