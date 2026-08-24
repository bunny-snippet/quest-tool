from django.db import migrations


FUNCTIONS = (
    (
        "studies.filter.supplier",
        "Use Supplier filter",
        "Traffic Reports - Filters",
        "Filter Traffic Reports by one or more external supplier accounts attached to respondent journeys.",
        "studies.filter.user",
    ),
    (
        "studies.field.provider_status",
        "Show provider outcome under Status",
        "Traffic Reports - Row details",
        "Display the provider-reported status, term reason and category below the normalized S1-S4 status in Traffic Reports, its API rows and Excel export.",
        "studies.column.status",
    ),
    (
        "studies.field.status_source",
        "Show status source in export",
        "Traffic Reports - Row details",
        "Include the internal callback/status source in the Traffic Reports Excel export.",
        "studies.column.status",
    ),
    (
        "termination_reasons.table.provider_status",
        "Show provider status in table",
        "Term Reports - Table details",
        "Display the provider-reported outcome inside each Term Report status cell.",
        "termination_reasons.column.status",
    ),
    (
        "termination_reasons.table.reason",
        "Show term reason in table",
        "Term Reports - Table details",
        "Display the exact provider term reason or category inside each Term Report status cell.",
        "termination_reasons.column.status",
    ),
    (
        "termination_reasons.export.status_source",
        "Export status source",
        "Term Reports - Export fields",
        "Include the internal callback/status source in the Term Reports workbook.",
        "termination_reasons.column.status",
    ),
)


def seed_permissions(apps, schema_editor):
    """Keep existing role visibility while making nested outcome data denyable."""

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
        role_ids = RoleFunctionPermission.objects.filter(
            function=source,
            allowed=True,
        ).values_list("role_id", flat=True)
        for role_id in role_ids:
            RoleFunctionPermission.objects.update_or_create(
                role_id=role_id,
                function=function,
                defaults={"allowed": True},
            )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0033_deactivate_supplier_quantity_permissions")]
    operations = [migrations.RunPython(seed_permissions, migrations.RunPython.noop)]
