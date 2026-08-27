from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("surveys", "0032_survey_project_list_indexes"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="survey",
            index=models.Index(
                fields=["integration", "status", "last_seen_at"],
                name="survey_int_status_seen_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="surveyattempt",
            index=models.Index(
                fields=["-callback_at", "-initiated_at", "status"],
                name="attempt_term_order_idx",
            ),
        ),
    ]
