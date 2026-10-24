import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


def quarantine_unscoped_organization_surveys(apps, schema_editor):
    Survey = apps.get_model('surveys', 'Survey')
    Survey.objects.filter(
        scope_kind='organization',
        organization_scope__isnull=True,
    ).update(scope_review_required=True)


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('accounts', '0007_organization_created_by_membership_ownership'),
        ('surveys', '0013_platformsharedaggregate'),
    ]

    operations = [
        migrations.RenameField(
            model_name='survey',
            old_name='organization',
            new_name='created_by',
        ),
        migrations.AlterField(
            model_name='survey',
            name='created_by',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='created_surveys',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name='survey',
            name='organization_scope',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='surveys',
                to='accounts.organization',
            ),
        ),
        migrations.AddField(
            model_name='survey',
            name='scope_review_required',
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(
            quarantine_unscoped_organization_surveys,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name='survey',
            constraint=models.CheckConstraint(
                condition=Q(scope_kind='platform') | Q(organization_scope__isnull=False) | Q(scope_review_required=True),
                name='survey_scope_present_or_review_required',
            ),
        ),
    ]
