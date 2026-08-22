import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("surveys", "0029_surveyattempt_report_lookup_indexes"),
    ]

    operations = [
        migrations.CreateModel(
            name="SurveyProjectEntryIPClaim",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("ip_address", models.GenericIPAddressField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "first_attempt",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="project_entry_ip_claim",
                        to="surveys.surveyattempt",
                    ),
                ),
                (
                    "survey",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="entry_ip_claims",
                        to="surveys.survey",
                    ),
                ),
            ],
            options={"ordering": ["created_at"]},
        ),
        migrations.AddConstraint(
            model_name="surveyprojectentryipclaim",
            constraint=models.UniqueConstraint(
                fields=("survey", "ip_address"),
                name="unique_survey_entry_ip_claim",
            ),
        ),
    ]
