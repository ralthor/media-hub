# Generated manually for adding duration_seconds to StoredFile

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('storage', '0003_storedfile_deleted_at_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='storedfile',
            name='duration_seconds',
            field=models.IntegerField(blank=True, default=0, null=True),
        ),
    ]
