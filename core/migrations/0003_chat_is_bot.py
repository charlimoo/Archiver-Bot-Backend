from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0002_job_scheduled_for_telegramuser_default_destination_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="chat",
            name="is_bot",
            field=models.BooleanField(default=False),
        ),
    ]
