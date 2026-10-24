from django.db import migrations, models
from django.db.models import Q


def normalize_platform_scope_rows(apps, schema_editor):
    Survey = apps.get_model('surveys', 'Survey')
    Survey.objects.filter(scope_kind='platform').update(
        organization_scope=None,
        scope_review_required=False,
    )


class Migration(migrations.Migration):
    dependencies = [
        ('surveys', '0014_survey_creator_scope_separation'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='survey',
            name='survey_scope_present_or_review_required',
        ),
        migrations.RunPython(normalize_platform_scope_rows, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='survey',
            constraint=models.CheckConstraint(
                condition=(
                    Q(scope_kind='platform', organization_scope__isnull=True, scope_review_required=False)
                    | Q(scope_kind='organization', organization_scope__isnull=False, scope_review_required=False)
                    | Q(scope_kind='organization', organization_scope__isnull=True, scope_review_required=True)
                ),
                name='survey_scope_kind_matches_owner',
            ),
        ),
    ]
