from django.db import migrations, models


def backfill_response_scopes(apps, schema_editor):
    Response = apps.get_model('surveys', 'Response')
    Organization = apps.get_model('accounts', 'Organization')
    organization_ids = dict(Organization.objects.values_list('user_id', 'id'))
    batch = []
    for response in Response.objects.select_related('survey').iterator(chunk_size=500):
        features = response.survey.features if isinstance(response.survey.features, dict) else {}
        declared_scope = features.get('scope') if isinstance(features.get('scope'), dict) else {}
        if declared_scope.get('kind') == 'platform' or features.get('is_global') is True:
            response.scope_kind = 'platform'
            response.scope_id = 'platform'
        else:
            organization_id = organization_ids.get(response.survey.organization_id)
            response.scope_kind = 'organization'
            response.scope_id = f'org-{organization_id}' if organization_id else f'user-{response.survey.organization_id}'
        batch.append(response)
        if len(batch) >= 500:
            Response.objects.bulk_update(batch, ['scope_kind', 'scope_id'])
            batch.clear()
    if batch:
        Response.objects.bulk_update(batch, ['scope_kind', 'scope_id'])


class Migration(migrations.Migration):
    dependencies = [
        ('surveys', '0010_response_guest_participant_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='response',
            name='policy_version',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='response',
            name='request_hash',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='response',
            name='scope_kind',
            field=models.CharField(default='organization', max_length=20),
        ),
        migrations.AddField(
            model_name='response',
            name='scope_id',
            field=models.CharField(blank=True, db_index=True, default='', max_length=128),
        ),
        migrations.RunPython(backfill_response_scopes, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='response',
            constraint=models.UniqueConstraint(
                condition=models.Q(('submission_id__isnull', False), ('user__isnull', False)),
                fields=('survey', 'user', 'submission_id'),
                name='unique_user_survey_submission',
            ),
        ),
        migrations.AddConstraint(
            model_name='response',
            constraint=models.UniqueConstraint(
                condition=models.Q(('guest_participant_id__gt', ''), ('submission_id__isnull', False), ('user__isnull', True)),
                fields=('survey', 'guest_participant_id', 'submission_id'),
                name='unique_guest_survey_submission',
            ),
        ),
    ]
