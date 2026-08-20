from django.db import migrations


CODE = "studies.column.client_name"
SOURCE_CODE = "studies.column.project_id"


def seed_permission(apps, schema_editor):
    """Preserve existing visibility while making client name independently denyable."""

    AccessFunction = apps.get_model("accounts", "AccessFunction")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")

    function, _ = AccessFunction.objects.update_or_create(
        code=CODE,
        defaults={
            "name": "Show Client name in Traffic Reports",
            "module": "Traffic Reports - Table columns",
            "description": (
                "Display the client name below Project ID in desktop rows, mobile "
                "cards, the Traffic Reports API and Excel export."
            ),
            "is_active": True,
        },
    )
    source = AccessFunction.objects.filter(code=SOURCE_CODE).first()
    if source is None:
        return
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
    dependencies = [("accounts", "0031_seed_studies_pid_column_permission")]
    operations = [migrations.RunPython(seed_permission, migrations.RunPython.noop)]
