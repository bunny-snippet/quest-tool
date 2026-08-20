from django.db import migrations


QUANTITY_CODES = (
    "vendors.card.quantity",
    "vendors.column.client.quantity",
    "vendors.column.project.quantity",
)


def deactivate_quantity_permissions(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    AccessFunction.objects.filter(code__in=QUANTITY_CODES).update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0032_seed_studies_client_name_permission")]

    operations = [migrations.RunPython(deactivate_quantity_permissions, migrations.RunPython.noop)]
