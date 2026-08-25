"""Backfill RFG postal hints from already-saved open quota targeting details."""

import re

from django.core.management.base import BaseCommand

from surveys.models import Survey
from surveys.rfg_text import clean_rfg_display_text


def _geo_dimension_label(name):
    lowered = str(name or "").lower()
    if "dma" in lowered:
        return "DMA"
    if "zip" in lowered or "postal" in lowered or "postcode" in lowered:
        return "ZIP/postal codes"
    if "region" in lowered or "district council" in lowered or "principal area" in lowered:
        return "region"
    if "state" in lowered:
        return "state"
    if "county" in lowered:
        return "county"
    if "city" in lowered or re.search(r"\bmetro(?:politan)?\b", lowered):
        return "city/metro area"
    return ""


def backfill_survey_geo_hint(survey):
    """Merge cached open-quota geo labels into one survey's postal question."""

    postal = survey.targeting_questions.filter(key="RFG_POSTAL_CODE").first()
    if postal is None:
        return False
    raw = dict(postal.raw_data or {})
    requirements = [
        item for item in raw.get("targeting_requirements") or []
        if isinstance(item, dict) and item.get("scope") != "quota"
    ]
    seen = {
        (
            str(item.get("name") or "").lower(),
            tuple(str(value) for value in item.get("values") or []),
        )
        for item in requirements
    }
    quotas = survey.quotas.filter(remaining__gt=0).exclude(status__iexact="Full")
    for quota in quotas:
        quota_raw = quota.raw_data or {}
        if quota_raw.get("quotaThrottle") == 1:
            continue
        details = quota_raw.get("targeting_details")
        if not isinstance(details, list):
            details = (quota.targeting or {}).get("targeting_details") or []
        for detail in details:
            if not isinstance(detail, dict):
                continue
            name = clean_rfg_display_text(detail.get("name") or "")
            dimension_label = _geo_dimension_label(name)
            values = []
            for value in detail.get("values") or []:
                value = clean_rfg_display_text(value)
                if value and value != "Provider-defined segment" and value not in values:
                    values.append(value)
            key = (name.lower(), tuple(values))
            if not dimension_label or not values or key in seen:
                continue
            seen.add(key)
            requirements.append({
                "name": name,
                "label": f"Open quota {dimension_label}",
                "values": values,
                "uses_wildcards": any("*" in value for value in values),
                "scope": "quota",
            })

    if not requirements:
        return False
    note = " · ".join(
        f"{item['label']}: {', '.join(str(value) for value in item['values'])}"
        for item in requirements
    )
    changed = (
        raw.get("targeting_requirements") != requirements
        or raw.get("targeting_note") != note
        or postal.text != f"What is your postal code? {note}"
    )
    if not changed:
        return False
    raw["targeting_requirements"] = requirements
    raw["targeting_note"] = note
    postal.raw_data = raw
    postal.text = f"What is your postal code? {note}"
    postal.save(update_fields=["text", "raw_data", "updated_at"])
    return True


class Command(BaseCommand):
    help = "Backfill RFG postal hints from cached open quota targeting data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--survey",
            dest="source_key",
            help="Optional RFG source key; omit to backfill every cached RFG survey.",
        )

    def handle(self, *args, **options):
        surveys = Survey.objects.filter(integration__provider_code="rfg").prefetch_related(
            "targeting_questions", "quotas"
        )
        if options.get("source_key"):
            surveys = surveys.filter(source_key=options["source_key"])
        checked = updated = 0
        for survey in surveys.iterator(chunk_size=200):
            checked += 1
            if backfill_survey_geo_hint(survey):
                updated += 1
        self.stdout.write(self.style.SUCCESS(
            f"Checked {checked} RFG surveys; updated {updated} postal hints."
        ))
