import secrets
import string

from django.db import migrations, models

import surveys.identifiers


ALPHABET = string.ascii_letters + string.digits


def migration_pid():
    length = secrets.randbelow(4) + 6
    characters = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        *(secrets.choice(ALPHABET) for _ in range(length - 3)),
    ]
    secrets.SystemRandom().shuffle(characters)
    return "".join(characters)


def backfill_attempt_pids(apps, schema_editor):
    SurveyAttempt = apps.get_model("surveys", "SurveyAttempt")
    used = set(
        SurveyAttempt.objects.exclude(pid__isnull=True)
        .exclude(pid="")
        .values_list("pid", flat=True)
    )
    batch = []
    for attempt in SurveyAttempt.objects.filter(pid__isnull=True).iterator(chunk_size=2000):
        while True:
            candidate = migration_pid()
            if candidate not in used and candidate not in {
                str(attempt.rid or ""), str(attempt.prescreener_uid or "")
            }:
                break
        used.add(candidate)
        attempt.pid = candidate
        batch.append(attempt)
        if len(batch) >= 2000:
            SurveyAttempt.objects.bulk_update(batch, ["pid"], batch_size=2000)
            batch = []
    if batch:
        SurveyAttempt.objects.bulk_update(batch, ["pid"], batch_size=2000)


class Migration(migrations.Migration):

    dependencies = [("surveys", "0016_cint_opportunities_webhook")]

    operations = [
        migrations.AddField(
            model_name="surveyattempt",
            name="pid",
            field=models.CharField(
                blank=True,
                db_index=True,
                editable=False,
                max_length=9,
                null=True,
                unique=True,
            ),
        ),
        migrations.RunPython(backfill_attempt_pids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="surveyattempt",
            name="pid",
            field=models.CharField(
                db_index=True,
                default=surveys.identifiers.generate_platform_pid,
                editable=False,
                help_text=(
                    "Platform tracking ID. Generated as 6-9 mixed alphanumeric characters; "
                    "kept separate from the provider-specific PID parameter."
                ),
                max_length=9,
                unique=True,
            ),
        ),
    ]
