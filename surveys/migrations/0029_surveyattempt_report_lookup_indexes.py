from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("surveys", "0028_surveyentryipclaim"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="survey",
            index=models.Index(
                fields=["integration", "last_seen_at"],
                name="survey_integration_seen_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="surveyattempt",
            index=models.Index(
                fields=["client", "-initiated_at"],
                name="attempt_client_init_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="surveyattempt",
            index=models.Index(fields=["-callback_at"], name="attempt_callback_idx"),
        ),
        migrations.AddIndex(
            model_name="surveyattempt",
            index=models.Index(fields=["callback_ip"], name="attempt_exit_ip_idx"),
        ),
    ]
