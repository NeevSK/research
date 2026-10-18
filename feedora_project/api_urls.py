from django.urls import path
from . import api_views

urlpatterns = [
    path('auth/login/', api_views.api_login, name='api_login'),
    path('auth/register/', api_views.api_register, name='api_register'),
    path('auth/me/', api_views.api_me, name='api_me'),
    path('auth/logout/', api_views.api_logout, name='api_logout'),
    path('respondent/history/', api_views.api_respondent_history, name='api_respondent_history'),
    path('entities/', api_views.api_entities_list, name='api_entities_list'),
    path('entities/<int:entity_id>/surveys/', api_views.api_entity_surveys, name='api_entity_surveys'),
    path('entities/<int:entity_id>/join/', api_views.api_entity_join, name='api_entity_join'),
    path('surveys/', api_views.api_surveys_list, name='api_surveys_list'),
    path('surveys/mine/', api_views.api_creator_surveys, name='api_creator_surveys'),
    path('surveys/create/', api_views.api_creator_create_survey, name='api_creator_create_survey'),
    path('surveys/<uuid:access_uuid>/publish/', api_views.api_creator_toggle_publish, name='api_creator_toggle_publish'),
    path('surveys/<uuid:access_uuid>/meta/', api_views.api_survey_meta, name='api_survey_meta'),
    path('surveys/code/<str:access_code>/meta/', api_views.api_survey_meta_by_code, name='api_survey_meta_by_code'),
    path('surveys/<uuid:access_uuid>/submit/', api_views.api_survey_submit, name='api_survey_submit'),
    path('surveys/<uuid:access_uuid>/analysis/', api_views.api_survey_analysis, name='api_survey_analysis'),
    path('surveys/<uuid:access_uuid>/export.csv', api_views.api_survey_export, name='api_survey_export'),
    path('openapi.json', api_views.api_openapi_spec, name='api_openapi_spec'),
    path('docs/', api_views.api_swagger_docs, name='api_swagger_docs'),
]
