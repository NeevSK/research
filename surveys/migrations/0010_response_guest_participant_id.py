from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('surveys', '0009_experiment_assignment'),
    ]

    operations = [
        migrations.AddField(
            model_name='response',
            name='guest_participant_id',
            field=models.CharField(blank=True, db_index=True, default='', max_length=128),
        ),
    ]
