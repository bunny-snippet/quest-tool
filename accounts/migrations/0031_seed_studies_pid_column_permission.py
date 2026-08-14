from django.db import migrations


CODE = "studies.column.pid"
DEFAULT_ROLES = ("employee", "employees", "external-vendor", "tl", "manager", "admin", "super-admin")


def seed_permission(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    Role = apps.get_model("accounts", "Role")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")

    function, _ = AccessFunction.objects.update_or_create(
        code=CODE,
        defaults={
            "name": "Show PID column",
            "module": "Traffic Reports - Table columns",
            "description": "Display the platform's independent per-attempt PID tracking identifier.",
            "is_active": True,
        },
    )
    for role in Role.objects.filter(slug__in=DEFAULT_ROLES, is_active=True):
        RoleFunctionPermission.objects.update_or_create(
            role=role,
            function=function,
            defaults={"allowed": True},
        )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0030_seed_cint_email_pool_permission")]
    operations = [migrations.RunPython(seed_permission, migrations.RunPython.noop)]
