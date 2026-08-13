from django.db import migrations


NEW_ENDPOINT = (
    "/Supply/v1/Surveys/AllOfferwall/ByCountryLanguage/"
    "{country_language_id}/{supplier_code}"
)
OLD_ENDPOINT = "/Supply/v1/Surveys/AllOfferwall/{supplier_code}"


def use_country_language_inventory(apps, schema_editor):
    integration = apps.get_model("vendors", "ClientIntegration")
    integration.objects.filter(provider_code="cint").update(
        inventory_endpoint=NEW_ENDPOINT,
        paged_inventory_endpoint="",
        inventory_result_key="Surveys",
    )


def restore_combined_inventory(apps, schema_editor):
    integration = apps.get_model("vendors", "ClientIntegration")
    integration.objects.filter(provider_code="cint").update(
        inventory_endpoint=OLD_ENDPOINT,
        paged_inventory_endpoint=(
            "/Supply/v1/Surveys/SupplierAllocations/All/{supplier_code}"
        ),
        inventory_result_key="Surveys + SupplierAllocationSurveys",
    )


class Migration(migrations.Migration):
    dependencies = [("vendors", "0012_supplier_product_terminology")]
    operations = [
        migrations.RunPython(use_country_language_inventory, restore_combined_inventory),
    ]
