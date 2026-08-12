from django.db import migrations, models
import django.db.models.deletion


CANONICAL_QUESTIONS = [
    ("age", "Age", "integer", "Respondent age in completed years."),
    ("date-of-birth", "Date of birth", "date", "Full respondent date of birth."),
    ("gender", "Gender", "single", "Respondent gender."),
    ("ethnicity", "Ethnicity", "multiple", "Respondent ethnicity or race."),
    ("postal-code", "ZIP / postal code", "text", "Respondent ZIP or postal code."),
    ("household-income", "Household income", "single", "Household income band."),
    ("country", "Country", "single", "Respondent country."),
    ("language", "Language", "single", "Respondent language."),
    ("employment-status", "Employment status", "single", "Respondent employment status."),
    ("industry", "Industry", "single", "Respondent industry."),
]


def seed_questions(apps, schema_editor):
    CanonicalQuestion = apps.get_model("surveys", "CanonicalQuestion")
    for code, label, value_type, description in CANONICAL_QUESTIONS:
        CanonicalQuestion.objects.update_or_create(
            code=code,
            defaults={
                "label": label,
                "value_type": value_type,
                "description": description,
                "is_active": True,
            },
        )


class Migration(migrations.Migration):
    dependencies = [("surveys", "0014_surveyattempt_prescreener_uid")]

    operations = [
        migrations.CreateModel(
            name="CanonicalQuestion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.SlugField(db_index=True, max_length=80, unique=True)),
                ("label", models.CharField(max_length=180)),
                ("value_type", models.CharField(choices=[("integer", "Integer"), ("decimal", "Decimal"), ("text", "Text"), ("date", "Date"), ("single", "Single choice"), ("multiple", "Multiple choice")], default="text", max_length=16)),
                ("description", models.TextField(blank=True)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["code"]},
        ),
        migrations.CreateModel(
            name="CanonicalOption",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.SlugField(max_length=100)),
                ("label", models.CharField(max_length=250)),
                ("normalized_value", models.CharField(blank=True, max_length=250)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("question", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="options", to="surveys.canonicalquestion")),
            ],
            options={"ordering": ["question__code", "code"]},
        ),
        migrations.CreateModel(
            name="ProviderQuestionMapping",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("provider_code", models.SlugField(db_index=True, max_length=50)),
                ("country_code", models.CharField(blank=True, db_index=True, max_length=8)),
                ("language_code", models.CharField(blank=True, db_index=True, max_length=8)),
                ("country_language_id", models.CharField(blank=True, db_index=True, max_length=40)),
                ("external_question_id", models.CharField(max_length=160)),
                ("external_question_key", models.CharField(blank=True, max_length=180)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("canonical_question", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="provider_mappings", to="surveys.canonicalquestion")),
            ],
            options={"ordering": ["provider_code", "country_code", "language_code", "external_question_id"]},
        ),
        migrations.CreateModel(
            name="ProviderOptionMapping",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("external_value", models.CharField(max_length=250)),
                ("external_label", models.CharField(blank=True, max_length=500)),
                ("canonical_value", models.CharField(blank=True, max_length=250)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("canonical_option", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="provider_mappings", to="surveys.canonicaloption")),
                ("question_mapping", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="option_mappings", to="surveys.providerquestionmapping")),
            ],
            options={"ordering": ["question_mapping", "external_value"]},
        ),
        migrations.AddConstraint(
            model_name="canonicaloption",
            constraint=models.UniqueConstraint(fields=("question", "code"), name="unique_canonical_question_option"),
        ),
        migrations.AddConstraint(
            model_name="providerquestionmapping",
            constraint=models.UniqueConstraint(fields=("provider_code", "country_code", "language_code", "country_language_id", "external_question_id"), name="unique_provider_question_mapping"),
        ),
        migrations.AddConstraint(
            model_name="provideroptionmapping",
            constraint=models.UniqueConstraint(fields=("question_mapping", "external_value"), name="unique_provider_option_mapping"),
        ),
        migrations.RunPython(seed_questions, migrations.RunPython.noop),
    ]
