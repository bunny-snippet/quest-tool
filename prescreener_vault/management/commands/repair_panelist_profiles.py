"""Repair normalized panelist profile fields from immutable stored answers."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from prescreener_vault.constants import DATABASE_ALIAS
from prescreener_vault.cache import invalidate_vault_cache
from prescreener_vault.models import PrescreenerAnswer, PrescreenerAnswerValue, PrescreenerSubmission
from prescreener_vault.services import (
    _age_from_value,
    _age_group,
    _canonical_attribute,
    _normalize_profile_value,
)


class Command(BaseCommand):
    help = "Rebuild Panelist Data profile specs from existing immutable vault answers."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=250)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        if not settings.PRESCREENER_VAULT_ENABLED:
            raise CommandError("Set PRESCREENER_VAULT_ENABLED=true before running this command.")

        scanned = repaired = answers_reclassified = 0
        queryset = PrescreenerSubmission.objects.using(DATABASE_ALIAS).prefetch_related(
            "question_answers__values"
        ).order_by("uid")

        for submission in queryset.iterator(
            chunk_size=max(1, int(options["batch_size"])),
        ):
            scanned += 1
            age = submission.respondent_age
            gender = submission.respondent_gender
            ethnicity = submission.respondent_ethnicity
            postal_code = submission.respondent_postal_code
            dimensions = dict(submission.profile_dimensions or {})
            answer_updates = []
            value_updates = []

            for answer in submission.question_answers.all():
                canonical = answer.canonical_attribute or _canonical_attribute(
                    answer.question_key,
                    answer.question_text,
                    answer.question_category,
                )
                if canonical and answer.canonical_attribute != canonical:
                    answer.canonical_attribute = canonical
                    answer_updates.append(answer)
                    answers_reclassified += 1

                reusable_values = (
                    answer.answer_labels
                    or answer.answer_values
                    or answer.upstream_values
                    or []
                )
                normalized = [
                    _normalize_profile_value(canonical, value)
                    for value in reusable_values
                ]
                normalized = [value for value in normalized if value]
                if canonical and normalized:
                    dimensions[canonical] = list(dict.fromkeys(normalized))
                if canonical in {"age", "date_of_birth"} and reusable_values:
                    detected = _age_from_value(reusable_values[0], submission.submitted_at)
                    if detected is not None:
                        age = detected
                elif canonical == "gender" and normalized:
                    gender = normalized[0]
                elif canonical == "ethnicity" and normalized:
                    ethnicity = normalized[0]
                elif canonical == "postal_code" and normalized:
                    postal_code = normalized[0]

                for index, value_row in enumerate(answer.values.all()):
                    normalized_value = (
                        normalized[index]
                        if index < len(normalized)
                        else _normalize_profile_value(canonical, value_row.label or value_row.value)
                    )
                    if (
                        value_row.canonical_attribute != canonical
                        or value_row.normalized_value != normalized_value
                    ):
                        value_row.canonical_attribute = canonical
                        value_row.normalized_value = normalized_value
                        value_updates.append(value_row)

            age_group = (
                _age_group(age)
                if age is not None
                else submission.respondent_age_group
            )
            if age is not None:
                dimensions["age"] = [str(age)]
                dimensions["age_group"] = [age_group]
            changed = any((
                submission.respondent_age != age,
                submission.respondent_age_group != age_group,
                submission.respondent_gender != gender,
                submission.respondent_ethnicity != ethnicity,
                submission.respondent_postal_code != postal_code,
                submission.profile_dimensions != dimensions,
                bool(answer_updates),
                bool(value_updates),
            ))
            if not changed:
                continue
            repaired += 1
            if options["dry_run"]:
                continue
            with transaction.atomic(using=DATABASE_ALIAS):
                if answer_updates:
                    PrescreenerAnswer.objects.using(DATABASE_ALIAS).bulk_update(
                        answer_updates, ["canonical_attribute"], batch_size=500
                    )
                if value_updates:
                    PrescreenerAnswerValue.objects.using(DATABASE_ALIAS).bulk_update(
                        value_updates,
                        ["canonical_attribute", "normalized_value"],
                        batch_size=1000,
                    )
                PrescreenerSubmission.objects.using(DATABASE_ALIAS).filter(
                    uid=submission.uid
                ).update(
                    respondent_age=age,
                    respondent_age_group=age_group,
                    respondent_gender=gender,
                    respondent_ethnicity=ethnicity,
                    respondent_postal_code=postal_code,
                    profile_dimensions=dimensions,
                )

        if repaired and not options["dry_run"]:
            invalidate_vault_cache()
        prefix = "would_repair" if options["dry_run"] else "repaired"
        self.stdout.write(self.style.SUCCESS(
            f"scanned={scanned} {prefix}={repaired} answers_reclassified={answers_reclassified}"
        ))
