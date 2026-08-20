import surveys.identifiers
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("surveys", "0024_profilereuseprojectusage_profilereusestate_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="surveyattempt",
            name="pid",
            field=models.CharField(
                db_index=True,
                default=surveys.identifiers.generate_platform_pid,
                editable=False,
                help_text=(
                    "Platform tracking ID. Newly generated as 12-13 mixed "
                    "alphanumeric characters; legacy 6-9 character values remain "
                    "valid; kept separate from the provider-specific PID parameter."
                ),
                max_length=13,
                unique=True,
            ),
        ),
    ]
