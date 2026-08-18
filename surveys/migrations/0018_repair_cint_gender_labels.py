from django.db import migrations


def repair_cint_gender_labels(apps, schema_editor):
    TargetingQuestion = apps.get_model("surveys", "TargetingQuestion")
    questions = TargetingQuestion.objects.filter(
        survey__integration__provider_code="cint",
        question_id=43,
    ).iterator(chunk_size=500)
    labels = {"1": "Male", "2": "Female"}
    for question in questions:
        changed = False
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
            if option_id in labels and option.get("OptionText") != labels[option_id]:
                option["OptionText"] = labels[option_id]
                changed = True
            options.append(option)
        if question.key.startswith("CINT_Q_"):
            question.key = "GENDER"
            changed = True
        if question.text.startswith("Cint qualification "):
            question.text = "What is your gender?"
            changed = True
        if question.question_type == "Qualification":
            question.question_type = "Single"
            changed = True
        if question.category == "Cint qualification":
            question.category = "Demographic"
            changed = True
        if changed:
            question.options = options
            question.save(update_fields=[
                "key", "text", "question_type", "category", "options", "updated_at"
            ])


class Migration(migrations.Migration):
    dependencies = [("surveys", "0017_surveyattempt_pid")]

    operations = [
        migrations.RunPython(repair_cint_gender_labels, migrations.RunPython.noop),
    ]
