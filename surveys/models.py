import hashlib
import uuid
import random
import string
from django.db import models
from django.db.models import Q
from accounts.models import User, Organization


def _response_submission_key(survey_id, user_id, guest_participant_id, submission_id):
    if not submission_id:
        return None
    if user_id is not None:
        principal = f'user:{user_id}'
    elif guest_participant_id:
        principal = f'guest:{guest_participant_id}'
    else:
        return None
    value = f'{survey_id}:{principal}:{submission_id}'.encode('utf-8')
    return hashlib.sha256(value).hexdigest()


class Survey(models.Model):
    # Authorship is provenance; authorization and data isolation come from the
    # explicit organization_scope/scope_kind pair.
    created_by  = models.ForeignKey(User, on_delete=models.PROTECT,
                                    related_name='created_surveys')
    organization_scope = models.ForeignKey(
        Organization, null=True, blank=True, on_delete=models.PROTECT,
        related_name='surveys',
    )
    scope_kind = models.CharField(max_length=20, default='organization', choices=[('organization', 'Organization'), ('platform', 'Platform')])
    scope_review_required = models.BooleanField(default=False)
    title         = models.CharField(max_length=200)
    description   = models.TextField(blank=True)
    is_hmsam      = models.BooleanField(default=False)
    access_uuid   = models.UUIDField(default=uuid.uuid4, unique=True,
                                     editable=False)
    access_code   = models.CharField(max_length=6, unique=True,
                                     editable=False)
    is_active     = models.BooleanField(default=True)
    is_draft = models.BooleanField(default=False)
    created_at    = models.DateTimeField(auto_now_add=True)

    # Theme settings saved from the builder
    theme_accent  = models.CharField(max_length=20, default='#14140f')
    theme_bg      = models.CharField(max_length=20, default='#fafaf7')
    submit_label  = models.CharField(max_length=100, default='Submit')

    header_image_b64 = models.TextField(blank=True, default='')
    btn_color        = models.CharField(max_length=20,  default='#14140f')
    btn_radius       = models.CharField(max_length=10,  default='6')
    btn_padding      = models.CharField(max_length=30,  default='10px 22px')
    btn_font_size    = models.CharField(max_length=10,  default='14px')
    q_font       = models.CharField(max_length=50, default="'Inter',sans-serif")
    header_font  = models.CharField(max_length=50, default="'Instrument Serif',serif")

    features = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(scope_kind='platform', organization_scope__isnull=True, scope_review_required=False)
                    | Q(scope_kind='organization', organization_scope__isnull=False, scope_review_required=False)
                    | Q(scope_kind='organization', organization_scope__isnull=True, scope_review_required=True)
                ),
                name='survey_scope_kind_matches_owner',
            ),
        ]

    def save(self, *args, **kwargs):
        valid_scope = (
            self.scope_kind == 'platform' and self.organization_scope_id is None and not self.scope_review_required
        ) or (
            self.scope_kind == 'organization' and self.organization_scope_id is not None and not self.scope_review_required
        ) or (
            self.scope_kind == 'organization' and self.organization_scope_id is None and self.scope_review_required
        )
        if not valid_scope:
            from django.core.exceptions import ValidationError
            raise ValidationError('Survey scope and owner are inconsistent.')
        if not self.access_code:
            self.access_code = self._generate_code()
        super().save(*args, **kwargs)

    def _generate_code(self):
        chars = string.ascii_uppercase + string.digits
        while True:
            code = ''.join(random.choices(chars, k=6))
            if not Survey.objects.filter(access_code=code).exists():
                return code

    def get_access_url(self):
        return f"/surveys/respond/{self.access_uuid}/"

    def __str__(self):
        return self.title


class Question(models.Model):
    LIKERT = 'likert'
    MCQ    = 'mcq'
    OPEN   = 'open'
    SHORT  = 'short'
    TYPE_CHOICES = [
        (LIKERT, 'Likert Scale'),
        (MCQ,    'Multiple Choice'),
        (OPEN,   'Long Answer'),
        (SHORT,  'Short Answer'),
    ]
    HMSAM_CONSTRUCTS = [
        ('peou',     'Perceived Ease of Use'),
        ('pu',       'Perceived Usefulness'),
        ('joy',      'Joy'),
        ('curiosity','Curiosity'),
        ('control',  'Control'),
        ('fi',       'Focused Immersion'),
        ('biu',      'Behavioural Intention to Use'),
    ]

    survey          = models.ForeignKey(Survey, on_delete=models.CASCADE,
                                        related_name='questions')
    text            = models.TextField()
    code            = models.CharField(max_length=10, blank=True)  # PEU1, JOY3
    question_type   = models.CharField(max_length=10, choices=TYPE_CHOICES,
                                       default=LIKERT)
    hmsam_construct = models.CharField(max_length=20,
                                       choices=HMSAM_CONSTRUCTS,
                                       blank=True, null=True)
    order           = models.PositiveIntegerField(default=0)
    is_required     = models.BooleanField(default=True)

    rating_style  = models.CharField(
        max_length=10,
        choices=[('stars','Stars'),('numeric','Numeric'),('likert','Likert')],
        default='stars', blank=True,
    )
    rating_max    = models.PositiveIntegerField(default=5)
    section_title = models.CharField(max_length=200, blank=True, default='')
    section_num   = models.PositiveIntegerField(default=1)


    class Meta:
        ordering = ['order']

    def __str__(self):
        return f"Q{self.order}: {self.text[:50]}"
    


class MCQOption(models.Model):
    question = models.ForeignKey(Question, on_delete=models.CASCADE,
                                 related_name='options')
    text     = models.CharField(max_length=200)
    order    = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order']


class Response(models.Model):
    survey          = models.ForeignKey(Survey, on_delete=models.CASCADE,
                                        related_name='responses')
    user            = models.ForeignKey(User, on_delete=models.SET_NULL,
                                        null=True, blank=True)
    guest_participant_id = models.CharField(max_length=128, blank=True, default='', db_index=True)
    submission_id   = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    submission_key  = models.CharField(max_length=64, null=True, blank=True)
    request_hash    = models.CharField(max_length=64, blank=True, default='')
    scope_kind      = models.CharField(max_length=20, default='organization')
    scope_id        = models.CharField(max_length=128, blank=True, default='', db_index=True)
    policy_version  = models.CharField(max_length=64, blank=True, default='')
    attempt = models.ForeignKey('SurveyAttempt', null=True, blank=True, on_delete=models.SET_NULL, related_name='responses')
    variant         = models.CharField(max_length=50, blank=True, default='gamified')
    experiment_key  = models.CharField(max_length=100, blank=True, default='hmsam-gamification-study')
    platform        = models.CharField(max_length=20, blank=True, default='web')
    app_version     = models.CharField(max_length=50, blank=True, default='1.0.0')
    duration_ms     = models.PositiveIntegerField(null=True, blank=True)
    dwell_ms        = models.JSONField(default=dict, blank=True)
    quality_flags   = models.JSONField(default=list, blank=True)
    consent_version = models.CharField(max_length=20, blank=True, default='1.0')
    consented_at    = models.DateTimeField(null=True, blank=True)
    points_earned   = models.PositiveIntegerField(default=0)
    completed_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['submission_key'],
                name='unique_response_submission_key',
            ),
        ]

    def save(self, *args, **kwargs):
        self.submission_key = _response_submission_key(
            self.survey_id, self.user_id, self.guest_participant_id, self.submission_id,
        )
        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = set(kwargs['update_fields']) | {'submission_key'}
        return super().save(*args, **kwargs)


class PlatformSharedAggregate(models.Model):
    """Opt-in aggregate completion counts; contains no participant or answer data."""
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='platform_shared_aggregates')
    survey = models.ForeignKey(Survey, on_delete=models.CASCADE, related_name='platform_shared_aggregates')
    period_start = models.DateField()
    completion_count = models.PositiveBigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'survey', 'period_start'],
                name='unique_org_survey_platform_aggregate_period',
            ),
        ]
        indexes = [models.Index(fields=['period_start'])]


class SurveyPolicyRevision(models.Model):
    """Immutable snapshot of one effective survey policy fingerprint."""
    survey = models.ForeignKey(Survey, on_delete=models.CASCADE, related_name='policy_revisions')
    version = models.CharField(max_length=64)
    document = models.JSONField(default=dict)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='authored_policy_revisions')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['survey', 'version'], name='unique_survey_policy_revision'),
        ]
        ordering = ['created_at', 'id']

    def save(self, *args, **kwargs):
        if self.pk:
            prior = type(self).objects.filter(pk=self.pk).values('survey_id', 'version', 'document', 'created_by_id').first()
            if prior and any(prior[field] != getattr(self, field) for field in ('survey_id', 'version', 'document', 'created_by_id')):
                from django.core.exceptions import ValidationError
                raise ValidationError('Survey policy revisions are immutable.')
        return super().save(*args, **kwargs)


class SurveyPolicyScopeLock(models.Model):
    """Portable serialization row for policy writes sharing a survey scope."""
    scope_kind = models.CharField(max_length=20)
    scope_id = models.CharField(max_length=128)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['scope_kind', 'scope_id'], name='unique_survey_policy_scope_lock'),
        ]


class SurveyAttempt(models.Model):
    IN_PROGRESS = 'in_progress'
    COMPLETED = 'completed'
    STALE = 'stale'
    STATUS_CHOICES = [(IN_PROGRESS, 'In progress'), (COMPLETED, 'Completed'), (STALE, 'Policy changed')]

    attempt_id = models.UUIDField(unique=True)
    survey = models.ForeignKey(Survey, on_delete=models.CASCADE, related_name='attempts')
    policy_revision = models.ForeignKey(SurveyPolicyRevision, on_delete=models.PROTECT, related_name='attempts')
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.CASCADE, related_name='survey_attempts')
    guest_principal = models.ForeignKey('accounts.GuestPrincipal', null=True, blank=True, on_delete=models.CASCADE, related_name='survey_attempts')
    active_key = models.CharField(max_length=128, null=True, blank=True)
    scope_kind = models.CharField(max_length=20)
    scope_id = models.CharField(max_length=128)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=IN_PROGRESS)
    started_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(Q(user__isnull=False, guest_principal__isnull=True) | Q(user__isnull=True, guest_principal__isnull=False)),
                name='survey_attempt_exactly_one_principal',
            ),
            models.UniqueConstraint(fields=['active_key'], name='unique_active_survey_attempt_key'),
        ]
        indexes = [
            models.Index(fields=['survey', 'status', 'policy_revision']),
        ]

    def save(self, *args, **kwargs):
        if (self.user_id is None) == (self.guest_principal_id is None):
            from django.core.exceptions import ValidationError
            raise ValidationError('A survey attempt must belong to exactly one participant.')
        if self.status == self.IN_PROGRESS:
            if self.user_id is not None:
                self.active_key = f'{self.survey_id}:user:{self.user_id}'
            elif self.guest_principal_id is not None:
                self.active_key = f'{self.survey_id}:guest:{self.guest_principal_id}'
        else:
            self.active_key = None
        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = set(kwargs['update_fields']) | {'active_key'}
        return super().save(*args, **kwargs)


class SurveyAdmissionGrant(models.Model):
    """Short-lived bearer grant bound to one survey and one stable principal."""
    survey = models.ForeignKey(Survey, on_delete=models.CASCADE, related_name='admission_grants')
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.CASCADE, related_name='survey_admission_grants')
    guest_principal = models.ForeignKey('accounts.GuestPrincipal', null=True, blank=True, on_delete=models.CASCADE, related_name='survey_admission_grants')
    token_hash = models.CharField(max_length=64, unique=True)
    policy_version = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(Q(user__isnull=False, guest_principal__isnull=True) | Q(user__isnull=True, guest_principal__isnull=False)),
                name='survey_admission_exactly_one_principal',
            ),
        ]

    def save(self, *args, **kwargs):
        if (self.user_id is None) == (self.guest_principal_id is None):
            from django.core.exceptions import ValidationError
            raise ValidationError('An admission grant must belong to exactly one participant.')
        return super().save(*args, **kwargs)


class SurveyAccessCodeAttempt(models.Model):
    """Privacy-preserving per-survey/IP window for access-code throttling."""
    bucket_hash = models.CharField(max_length=64, unique=True)
    window_started_at = models.DateTimeField(db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)


class Answer(models.Model):
    response        = models.ForeignKey(Response, on_delete=models.CASCADE,
                                        related_name='answers')
    question        = models.ForeignKey(Question, on_delete=models.CASCADE)
    likert_value    = models.PositiveIntegerField(null=True, blank=True)
    text_value      = models.TextField(blank=True)
    selected_option = models.ForeignKey(MCQOption, on_delete=models.SET_NULL,
                                        null=True, blank=True)


class Experiment(models.Model):
    survey = models.ForeignKey(Survey, on_delete=models.CASCADE, related_name='experiments')
    key = models.CharField(max_length=100, default='hmsam-gamification-study')
    arms = models.JSONField(default=list, blank=True)
    salt = models.CharField(max_length=64, default='feedora_exp_salt_v1')
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Experiment {self.key} for {self.survey.title}"


class Assignment(models.Model):
    experiment = models.ForeignKey(Experiment, on_delete=models.CASCADE, related_name='assignments')
    participant_id = models.CharField(max_length=128, db_index=True)
    arm = models.CharField(max_length=64)
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('experiment', 'participant_id')

    def __str__(self):
        return f"{self.participant_id} -> {self.arm} ({self.experiment.key})"
