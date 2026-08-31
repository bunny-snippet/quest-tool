from django.db import migrations


FUNCTIONS = (
    (
        "user_hits.filter.supplier",
        "Use Supplier filter",
        "User Hits - Filters",
        "Filter User Hits by one or more external suppliers attached to respondent journeys.",
        "user_hits.filter.user",
    ),
    (
        "termination_reasons.filter.supplier",
        "Filter by supplier",
        "Term Reports - Filters",
        "Filter unsuccessful outcomes by one or more external suppliers attached to respondent journeys.",
        "termination_reasons.filter.user",
    ),
)


def seed_permissions(apps, schema_editor):
    """Grant each new supplier filter wherever the equivalent user filter is allowed."""

    AccessFunction = apps.get_model("accounts", "AccessFunction")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")

    for code, name, module, description, source_code in FUNCTIONS:
        function, _ = AccessFunction.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "module": module,
                "description": description,
                "is_active": True,
            },
        )
        source = AccessFunction.objects.filter(code=source_code).first()
        if source is None:
            continue
        for role_id in RoleFunctionPermission.objects.filter(
            function=source,
            allowed=True,
        ).values_list("role_id", flat=True):
            RoleFunctionPermission.objects.update_or_create(
                role_id=role_id,
                function=function,
                defaults={"allowed": True},
            )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0034_seed_provider_outcome_permissions")]

    operations = [migrations.RunPython(seed_permissions, migrations.RunPython.noop)]
