from django.db import migrations


LOCALE_IDS = {
    "eng_ca": 6,
    "eng_in": 7,
    "eng_gb": 8,
    "eng_us": 9,
    "fra_fr": 10,
    "fre_fr": 10,
    "hin_in": 76,
}

MARKET_IDS = {
    ("CA", "ENG"): 6,
    ("IN", "ENG"): 7,
    ("GB", "ENG"): 8,
    ("US", "ENG"): 9,
    ("FR", "FRA"): 10,
    ("FR", "FRE"): 10,
    ("IN", "HIN"): 76,
}

QUESTION_FALLBACKS = {
    42: {
        "key": "AGE",
        "text": "What is your age?",
        "question_type": "Numeric - Open-end",
        "category": "Demographic",
        "options": {},
    },
    43: {
        "key": "GENDER",
        "text": "Are you...?",
        "question_type": "Single Punch",
        "category": "Demographic",
        "options": {"1": "Male", "2": "Female"},
    },
    45: {
        "key": "ZIP",
        "text": "What is your ZIP/postal code?",
        "question_type": "Numeric - Open-end",
        "category": "Demographic",
        "options": {},
    },
}


def _age_ranges(values):
    numbers = sorted({
        int(value)
        for value in values
        if str(value).strip().isdigit() and 0 <= int(value) <= 125
    })
    if not numbers:
        return []
    ranges = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append({"min": start, "max": previous})
        start = previous = number
    ranges.append({"min": start, "max": previous})
    return ranges


def repair_webhook_country_language_and_questions(apps, schema_editor):
    Survey = apps.get_model("surveys", "Survey")
    TargetingQuestion = apps.get_model("surveys", "TargetingQuestion")

    surveys = Survey.objects.filter(
        integration__provider_code="cint",
        raw_data___cint_inventory_source="opportunities_webhook",
    ).iterator(chunk_size=500)
    for survey in surveys:
        raw_data = dict(survey.raw_data or {})
        locale = str(raw_data.get("_cint_locale") or "").strip().lower().replace("-", "_")
        country_language_id = LOCALE_IDS.get(locale) or MARKET_IDS.get((
            str(survey.country_code or "").upper(),
            str(survey.language_code or "").upper(),
        ))
        update_fields = []
        if country_language_id:
            raw_data["CountryLanguageID"] = country_language_id
            raw_data["_cint_country_language_request_id"] = country_language_id
            survey.raw_data = raw_data
            update_fields.append("raw_data")
        # Force the next Eye/prescreener request to replace webhook precodes
        # with the official localized Question Library metadata.
        survey.targeting_synced_at = None
        survey.detail_synced_at = None
        update_fields.extend(["targeting_synced_at", "detail_synced_at"])
        survey.save(update_fields=update_fields)

    questions = TargetingQuestion.objects.filter(
        survey__integration__provider_code="cint",
        question_id__in=QUESTION_FALLBACKS,
    ).iterator(chunk_size=500)
    for question in questions:
        fallback = QUESTION_FALLBACKS[question.question_id]
        labels = fallback["options"]
        options = []
        for raw_option in question.options or []:
            option = dict(raw_option) if isinstance(raw_option, dict) else {
                "OptionId": str(raw_option),
                "OptionText": str(raw_option),
            }
            option_id = str(
                option.get("OptionId")
                or option.get("Precode")
                or option.get("value")
                or ""
            )
            option["OptionText"] = labels.get(option_id, option.get("OptionText") or option_id)
            options.append(option)
        raw_data = dict(question.raw_data or {})
        if question.question_id == 42:
            accepted = raw_data.get("targeting_choices") or [
                option.get("OptionId") for option in options
            ]
            raw_data["targeting_age_ranges"] = _age_ranges(accepted)
        question.key = fallback["key"]
        question.text = fallback["text"]
        question.question_type = fallback["question_type"]
        question.category = fallback["category"]
        question.options = options
        question.raw_data = raw_data
        question.save(update_fields=[
            "key", "text", "question_type", "category", "options", "raw_data", "updated_at"
        ])


class Migration(migrations.Migration):
    dependencies = [("surveys", "0018_repair_cint_gender_labels")]

    operations = [
        migrations.RunPython(
            repair_webhook_country_language_and_questions,
            migrations.RunPython.noop,
        ),
    ]
