from django.contrib import admin

from .models import (
    AllocationReservation,
    Client,
    ClientIntegration,
    VendorClientAllocation,
    VendorCommercialProfile,
    VendorSurveyAllocation,
)


class ClientIntegrationInline(admin.TabularInline):
    model = ClientIntegration
    extra = 0


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "provider_code", "is_active", "updated_at"]
    search_fields = ["name", "code", "company_name_match"]
    list_filter = ["provider_code", "is_active"]
    inlines = [ClientIntegrationInline]


@admin.register(VendorCommercialProfile)
class VendorCommercialProfileAdmin(admin.ModelAdmin):
    list_display = ["vendor", "default_cpi_cut_percent", "currency", "is_active", "updated_at"]
    search_fields = ["vendor__username", "vendor__first_name", "vendor__last_name", "vendor__email"]
    list_filter = ["is_active", "currency"]


@admin.register(VendorClientAllocation)
class VendorClientAllocationAdmin(admin.ModelAdmin):
    list_display = [
        "vendor", "client", "quantity_limit", "reserved_quantity", "consumed_quantity",
        "remaining", "cpi_cut_override_percent", "is_active",
    ]
    search_fields = ["vendor__username", "vendor__email", "client__name", "client__code"]
    list_filter = ["client", "is_active"]
    readonly_fields = ["reserved_quantity", "consumed_quantity", "created_at", "updated_at"]

    @admin.display(description="Remaining")
    def remaining(self, obj):
        return obj.remaining_quantity


@admin.register(VendorSurveyAllocation)
class VendorSurveyAllocationAdmin(admin.ModelAdmin):
    list_display = [
        "vendor_name", "survey", "quantity_limit", "reserved_quantity", "consumed_quantity",
        "remaining", "cpi_cut_override_percent", "is_active",
    ]
    search_fields = [
        "client_allocation__vendor__username", "client_allocation__vendor__email",
        "survey__local_id", "survey__source_id", "survey__name",
    ]
    list_filter = ["client_allocation__client", "is_active"]
    readonly_fields = ["reserved_quantity", "consumed_quantity", "created_at", "updated_at"]

    @admin.display(description="Vendor")
    def vendor_name(self, obj):
        return obj.vendor

    @admin.display(description="Remaining")
    def remaining(self, obj):
        return obj.remaining_quantity


@admin.register(AllocationReservation)
class AllocationReservationAdmin(admin.ModelAdmin):
    list_display = ["attempt", "survey_allocation", "status", "quantity", "expires_at", "finalized_at"]
    search_fields = ["attempt__rid", "client_allocation__vendor__username", "survey_allocation__survey__local_id"]
    list_filter = ["status", "expires_at"]
    readonly_fields = [field.name for field in AllocationReservation._meta.fields]
