from django.db import migrations


CODE = "access.cint_email_pool.manage"


def seed_permission(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    Role = apps.get_model("accounts", "Role")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")

    function, _ = AccessFunction.objects.update_or_create(
        code=CODE,
        defaults={
            "name": "Manage Cint respondent emails",
            "module": "Access control - Sensitive data",
            "description": (
                "Import real respondent emails into the encrypted Cint identity pool "
                "and view aggregate pool status."
            ),
            "is_active": True,
        },
    )
    for role in Role.objects.filter(slug__in=("super-admin", "superadmin"), is_active=True):
        RoleFunctionPermission.objects.update_or_create(
            role=role,
            function=function,
            defaults={"allowed": True},
        )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0029_restrict_dashboard_to_super_admin")]

    operations = [migrations.RunPython(seed_permission, migrations.RunPython.noop)]
