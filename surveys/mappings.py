"""Provider-neutral question and answer mapping services."""

import hashlib
import re

from django.db import transaction
from django.utils.text import slugify

from .models import (
    CanonicalOption,
    CanonicalQuestion,
    ProviderOptionMapping,
    ProviderQuestionMapping,
)


QUESTION_PATTERNS = (
    ("date-of-birth", (r"date of birth", r"birth ?date", r"birthday", r"\bdob\b")),
    ("age", (r"\bage\b", r"years old")),
    ("gender", (r"\bgender\b", r"\bsex\b")),
    ("ethnicity", (r"ethnic", r"\brace\b", r"racial")),
    ("postal-code", (r"postal", r"zip ?code", r"\bzip\b")),
    ("household-income", (r"household income", r"\bhhi\b", r"annual income", r"income band")),
    ("country", (r"\bcountry\b", r"nation of residence")),
    ("language", (r"\blanguage\b",)),
    ("employment-status", (r"employment status", r"work status", r"occupation status")),
    ("industry", (r"\bindustry\b", r"business sector")),
)


def _question_value_type(question) -> str:
    value = f"{question.question_type} {question.key}".lower()
    if any(item in value for item in ("multi", "multiple", "checkbox")):
        return CanonicalQuestion.ValueType.MULTIPLE
    if any(item in value for item in ("single", "radio", "punch", "choice", "qualification")):
        return CanonicalQuestion.ValueType.SINGLE
    if "date" in value or "birth" in value:
        return CanonicalQuestion.ValueType.DATE
    if "numeric" in value or "number" in value or re.search(r"\bage\b", value):
        return CanonicalQuestion.ValueType.INTEGER
    return CanonicalQuestion.ValueType.TEXT


def infer_canonical_code(question) -> str:
    raw = question.raw_data or {}
    explicit = str(raw.get("canonical_key") or "").strip().lower()
    if explicit:
        return explicit
    searchable = " ".join((str(question.key or ""), str(question.text or ""))).lower()
    searchable = re.sub(r"[_\-.]+", " ", searchable)
    for code, patterns in QUESTION_PATTERNS:
        if any(re.search(pattern, searchable) for pattern in patterns):
            return code
    base = slugify(question.key or question.text or f"question-{question.question_id}")[:58]
    return f"custom-{base or question.question_id}"


def _option_parts(option):
    if not isinstance(option, dict):
        return str(option), str(option)
    external = next(
        (
            option.get(key) for key in (
                "OptionId", "OptionID", "optionId", "Precode", "precode", "value", "id", "code"
            ) if option.get(key) not in (None, "")
        ),
        "",
    )
    label = next(
        (
            option.get(key) for key in (
                "OptionText", "optionText", "label", "text", "name", "value"
            ) if option.get(key) not in (None, "")
        ),
        external,
    )
    return str(external), str(label)


def _canonical_option_code(question_code: str, label: str, external_value: str) -> str:
    compact = re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()
    if question_code == "gender":
        if compact in {"m", "male", "man", "boy"}:
            return "male"
        if compact in {"f", "female", "woman", "girl"}:
            return "female"
        if any(item in compact for item in ("non binary", "nonbinary", "other identification")):
            return "non-binary"
        if "prefer not" in compact:
            return "prefer-not-to-say"
    value = slugify(label) or slugify(external_value) or "value"
    if len(value) <= 88:
        return value
    digest = hashlib.sha256(f"{label}|{external_value}".encode("utf-8")).hexdigest()[:10]
    return f"{value[:77]}-{digest}"


@transaction.atomic
def sync_survey_mappings(survey) -> dict[str, int]:
    """Learn mappings from stored targeting rows without changing respondent data."""
    provider_code = (
        survey.integration.provider_code if survey.integration_id else "innovatemr"
    ).strip().lower()
    country_language_id = str((survey.raw_data or {}).get("CountryLanguageID") or "")
    counters = {"questions": 0, "options": 0}
    for question in survey.targeting_questions.all():
        canonical_code = infer_canonical_code(question)
        canonical, _ = CanonicalQuestion.objects.get_or_create(
            code=canonical_code,
            defaults={
                "label": question.text or question.key or canonical_code.replace("-", " ").title(),
                "value_type": _question_value_type(question),
                "description": "Automatically discovered from provider targeting metadata.",
            },
        )
        mapping, _ = ProviderQuestionMapping.objects.update_or_create(
            provider_code=provider_code,
            country_code=(survey.country_code or "").upper(),
            language_code=(survey.language_code or "").upper(),
            country_language_id=country_language_id,
            external_question_id=str(question.question_id),
            defaults={
                "external_question_key": question.key,
                "canonical_question": canonical,
                "is_active": True,
                "metadata": {
                    "text": question.text,
                    "question_type": question.question_type,
                    "category": question.category,
                },
            },
        )
        counters["questions"] += 1
        active_values = []
        for option in question.options or []:
            external_value, external_label = _option_parts(option)
            if not external_value:
                continue
            option_code = _canonical_option_code(canonical.code, external_label, external_value)
            canonical_option, _ = CanonicalOption.objects.get_or_create(
                question=canonical,
                code=option_code,
                defaults={
                    "label": external_label,
                    "normalized_value": option_code,
                },
            )
            ProviderOptionMapping.objects.update_or_create(
                question_mapping=mapping,
                external_value=external_value,
                defaults={
                    "external_label": external_label,
                    "canonical_option": canonical_option,
                    "canonical_value": canonical_option.normalized_value or canonical_option.code,
                    "is_active": True,
                    "metadata": option if isinstance(option, dict) else {},
                },
            )
            active_values.append(external_value)
            counters["options"] += 1
        mapping.option_mappings.exclude(external_value__in=active_values).update(is_active=False)
        raw_data = dict(question.raw_data or {})
        if raw_data.get("canonical_key") != canonical.code:
            raw_data["canonical_key"] = canonical.code
            question.raw_data = raw_data
            question.save(update_fields=["raw_data", "updated_at"])
    return counters


def provider_answers(canonical_answers: dict, *, provider_code: str, country_code: str = "", language_code: str = "", country_language_id: str = "") -> dict[str, list[str]]:
    """Translate our stable keys into a provider's question IDs and precodes."""
    mappings = ProviderQuestionMapping.objects.filter(
        provider_code=provider_code.lower(),
        country_code=country_code.upper(),
        language_code=language_code.upper(),
        country_language_id=str(country_language_id or ""),
        is_active=True,
        canonical_question__code__in=canonical_answers,
    ).select_related("canonical_question").prefetch_related("option_mappings__canonical_option")
    translated = {}
    for mapping in mappings:
        supplied = canonical_answers.get(mapping.canonical_question.code)
        values = supplied if isinstance(supplied, (list, tuple, set)) else [supplied]
        wanted = {str(value) for value in values if value not in (None, "")}
        external = [
            item.external_value for item in mapping.option_mappings.all()
            if item.is_active and (
                item.canonical_value in wanted
                or (item.canonical_option_id and item.canonical_option.code in wanted)
            )
        ]
        if external:
            translated[mapping.external_question_id] = external
        elif supplied not in (None, ""):
            translated[mapping.external_question_id] = [str(supplied)]
    return translated
