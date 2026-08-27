from django.db import migrations


def raise_rfg_sync_interval_floor(apps, schema_editor):
    ClientIntegration = apps.get_model("vendors", "ClientIntegration")
    ClientIntegration.objects.filter(
        provider_code="rfg",
        sync_interval_seconds__lt=600,
    ).update(sync_interval_seconds=600)


class Migration(migrations.Migration):
    dependencies = [("vendors", "0023_seed_purespectrum_integration")]
    operations = [
        migrations.RunPython(
            raise_rfg_sync_interval_floor,
            migrations.RunPython.noop,
        ),
    ]
