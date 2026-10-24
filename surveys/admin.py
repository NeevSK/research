from django.contrib import admin
from .models import Survey, Question, MCQOption, Response, Answer

@admin.register(Survey)
class SurveyAdmin(admin.ModelAdmin):
    list_display  = ['title', 'created_by', 'organization_scope', 'scope_kind', 'scope_review_required', 'is_hmsam', 'access_code', 'is_active', 'created_at']
    list_filter   = ['is_hmsam', 'is_active']
    search_fields = ['title', 'created_by__email', 'organization_scope__org_name']
    readonly_fields = ['access_uuid', 'access_code', 'created_at']

@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display  = ['code', 'text', 'survey', 'question_type', 'hmsam_construct', 'order']
    list_filter   = ['question_type', 'hmsam_construct']
    search_fields = ['text', 'survey__title']

@admin.register(MCQOption)
class MCQOptionAdmin(admin.ModelAdmin):
    list_display = ['text', 'question', 'order']

@admin.register(Response)
class ResponseAdmin(admin.ModelAdmin):
    list_display = ['survey', 'user', 'points_earned', 'completed_at']

@admin.register(Answer)
class AnswerAdmin(admin.ModelAdmin):
    list_display = ['question', 'response', 'likert_value', 'text_value']
