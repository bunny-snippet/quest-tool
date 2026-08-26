from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("surveys", "0030_surveyprojectentryipclaim"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="surveyattempt",
            index=models.Index(
                fields=["user_id", "-initiated_at"],
                name="attempt_legacy_user_init_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="surveyattempt",
            index=models.Index(
                fields=["platform_user", "status", "-callback_at"],
                name="attempt_user_status_cb_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="surveyattempt",
            index=models.Index(
                fields=["user_id", "status", "-callback_at"],
                name="attempt_legacy_status_cb_idx",
            ),
        ),
    ]
