import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('accounts', '0006_alter_organization_share_aggregate_stats_to_platform'),
        # Earlier survey data backfills still read the legacy owner column.
        ('surveys', '0013_platformsharedaggregate'),
    ]

    operations = [
        migrations.RenameField(
            model_name='organization',
            old_name='user',
            new_name='created_by',
        ),
        migrations.AlterField(
            model_name='organization',
            name='created_by',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='created_organizations',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
