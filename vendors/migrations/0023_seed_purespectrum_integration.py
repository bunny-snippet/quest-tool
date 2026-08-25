from django.db import migrations


def seed_purespectrum(apps, schema_editor):
    """Publish the non-secret Fusion Match integration card after deployment."""

    Client = apps.get_model("vendors", "Client")
    ClientIntegration = apps.get_model("vendors", "ClientIntegration")
    client, created = Client.objects.get_or_create(
        code="purespectrum",
        defaults={
            "name": "PureSpectrum",
            "provider_code": "purespectrum",
            "company_name_match": "PureSpectrum",
            "is_active": True,
        },
    )
    if not created and client.provider_code != "purespectrum":
        return
    ClientIntegration.objects.get_or_create(
        client=client,
        name="Fusion Match",
        defaults={
            "provider_code": "purespectrum",
            "base_url": "https://fusionapi.spectrumsurveys.com/surveys/fusionMatch",
            "credential_env_key": "PURESPECTRUM_ACCESS_TOKEN",
            "config": {"timeout_seconds": 30},
            "supplier_code": "1000",
            "inventory_endpoint": "",
            "auth_header_name": "access-token",
            "auth_header_prefix": "",
            "inventory_result_key": "surveys",
            "scheduled_sync_enabled": False,
            "sync_interval_seconds": 60,
            "detail_refresh_batch": 3,
            "is_active": True,
        },
    )


class Migration(migrations.Migration):
    dependencies = [("vendors", "0022_verisoul_security_policy")]
    operations = [migrations.RunPython(seed_purespectrum, migrations.RunPython.noop)]
