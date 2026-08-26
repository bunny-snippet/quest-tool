from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("surveys", "0031_surveyattempt_report_composite_indexes"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="survey",
            index=models.Index(
                fields=["-source_modified_at", "-created_at"],
                name="survey_modified_created_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="survey",
            index=models.Index(
                fields=["country_code", "country"],
                name="survey_country_label_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="survey",
            index=models.Index(
                fields=["buyer_id", "client", "company_name"],
                name="survey_buyer_scope_idx",
            ),
        ),
    ]
