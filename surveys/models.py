import uuid
import random
import string
from django.db import models
from accounts.models import User


class Survey(models.Model):
    organization  = models.ForeignKey(User, on_delete=models.CASCADE,
                                      related_name='surveys')
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

    def save(self, *args, **kwargs):
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
    survey        = models.ForeignKey(Survey, on_delete=models.CASCADE,
                                      related_name='responses')
    user          = models.ForeignKey(User, on_delete=models.SET_NULL,
                                      null=True, blank=True)
    submission_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    points_earned = models.PositiveIntegerField(default=0)
    completed_at  = models.DateTimeField(auto_now_add=True)


class Answer(models.Model):
    response        = models.ForeignKey(Response, on_delete=models.CASCADE,
                                        related_name='answers')
    question        = models.ForeignKey(Question, on_delete=models.CASCADE)
    likert_value    = models.PositiveIntegerField(null=True, blank=True)
    text_value      = models.TextField(blank=True)
    selected_option = models.ForeignKey(MCQOption, on_delete=models.SET_NULL,
                                        null=True, blank=True)