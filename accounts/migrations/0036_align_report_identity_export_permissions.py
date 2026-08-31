from django.db import migrations


GRANTS = (
    # Traffic's PID and RID/UID columns are one audit identity set in Excel.
    ("studies.column.respondent_id", "studies.column.pid"),
    # A role allowed to download Term Reports must receive the identifiers in
    # that workbook, while the page's normal column permission remains intact.
    ("termination_reasons.column.rid", "termination_reasons.export"),
)


def align_identity_permissions(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")

    for target_code, source_code in GRANTS:
        target = AccessFunction.objects.filter(code=target_code).first()
        source = AccessFunction.objects.filter(code=source_code).first()
        if target is None or source is None:
            continue
        for role_id in RoleFunctionPermission.objects.filter(
            function=source,
            allowed=True,
        ).values_list("role_id", flat=True):
            RoleFunctionPermission.objects.update_or_create(
                role_id=role_id,
                function=target,
                defaults={"allowed": True},
            )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0035_seed_supplier_report_filter_permissions")]

    operations = [migrations.RunPython(align_identity_permissions, migrations.RunPython.noop)]
