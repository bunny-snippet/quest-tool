from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import serializers

from accounts.models import EmployeeProfile

from .models import (
    AllocationReservation,
    Client,
    ClientIntegration,
    VendorClientAllocation,
    VendorCommercialProfile,
    VendorSurveyAllocation,
)


class VendorDirectorySerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()
    account_type = serializers.CharField(source="employee_profile.account_type", read_only=True)
    role_name = serializers.CharField(source="employee_profile.role.name", read_only=True, allow_null=True)
    created_by = serializers.CharField(source="employee_profile.created_by.username", read_only=True, allow_null=True)
    commercial_profile_id = serializers.IntegerField(source="vendor_commercial_profile.id", read_only=True, allow_null=True)
    default_cpi_cut_percent = serializers.DecimalField(
        source="vendor_commercial_profile.default_cpi_cut_percent",
        max_digits=5,
        decimal_places=2,
        read_only=True,
        allow_null=True,
    )
    currency = serializers.CharField(source="vendor_commercial_profile.currency", read_only=True, allow_null=True)
    allocation_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = get_user_model()
        fields = [
            "id", "username", "full_name", "email", "account_type", "role_name", "created_by",
            "commercial_profile_id", "default_cpi_cut_percent", "currency", "allocation_count",
            "is_active", "date_joined",
        ]
        read_only_fields = fields

    def get_full_name(self, obj) -> str:
        return obj.get_full_name() or obj.username


class VendorManagementVendorOptionSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    full_name = serializers.CharField()
    username = serializers.CharField()
    account_type = serializers.ChoiceField(choices=EmployeeProfile.AccountType.choices)


class VendorManagementClientOptionSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    code = serializers.CharField()
    provider_code = serializers.CharField()


class VendorManagementOptionsSerializer(serializers.Serializer):
    vendors = VendorManagementVendorOptionSerializer(many=True)
    clients = VendorManagementClientOptionSerializer(many=True)


class ClientIntegrationSerializer(serializers.ModelSerializer):
    created_by = serializers.CharField(source="created_by.username", read_only=True, allow_null=True)

    class Meta:
        model = ClientIntegration
        fields = [
            "id", "client", "name", "provider_code", "base_url", "credential_env_key",
            "scheduled_sync_enabled", "is_active", "created_by", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class ClientSerializer(serializers.ModelSerializer):
    created_by = serializers.CharField(source="created_by.username", read_only=True, allow_null=True)
    integrations = ClientIntegrationSerializer(many=True, read_only=True)

    class Meta:
        model = Client
        fields = [
            "id", "code", "name", "provider_code", "company_name_match", "is_active",
            "created_by", "created_at", "updated_at", "integrations",
        ]
        read_only_fields = ["created_at", "updated_at"]


class VendorCommercialProfileSerializer(serializers.ModelSerializer):
    vendor_name = serializers.SerializerMethodField()
    account_type = serializers.CharField(source="vendor.employee_profile.account_type", read_only=True)
    created_by = serializers.CharField(source="created_by.username", read_only=True, allow_null=True)

    class Meta:
        model = VendorCommercialProfile
        fields = [
            "id", "vendor", "vendor_name", "account_type", "default_cpi_cut_percent", "currency",
            "is_active", "created_by", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_vendor_name(self, obj) -> str:
        return obj.vendor.get_full_name() or obj.vendor.username

    def validate(self, attrs):
        attrs = super().validate(attrs)
        vendor = attrs.get("vendor", getattr(self.instance, "vendor", None))
        cut = attrs.get("default_cpi_cut_percent", getattr(self.instance, "default_cpi_cut_percent", Decimal("0.00")))
        profile = EmployeeProfile.objects.filter(user=vendor).first()
        if not profile or profile.account_type not in {
            EmployeeProfile.AccountType.INTERNAL_VENDOR,
            EmployeeProfile.AccountType.EXTERNAL_VENDOR,
        }:
            raise serializers.ValidationError({"vendor": "Select an internal or external vendor account."})
        if profile.account_type == EmployeeProfile.AccountType.INTERNAL_VENDOR and cut != Decimal("0.00"):
            raise serializers.ValidationError({"default_cpi_cut_percent": "Internal vendor cut must be zero."})
        return attrs


class VendorClientAllocationSerializer(serializers.ModelSerializer):
    vendor_name = serializers.SerializerMethodField()
    client_name = serializers.CharField(source="client.name", read_only=True)
    account_type = serializers.CharField(source="vendor.employee_profile.account_type", read_only=True)
    remaining_quantity = serializers.IntegerField(read_only=True)
    effective_cpi_cut_percent = serializers.DecimalField(max_digits=5, decimal_places=2, read_only=True)
    created_by = serializers.CharField(source="created_by.username", read_only=True, allow_null=True)

    class Meta:
        model = VendorClientAllocation
        fields = [
            "id", "vendor", "vendor_name", "account_type", "client", "client_name", "quantity_limit",
            "reserved_quantity", "consumed_quantity", "remaining_quantity", "cpi_cut_override_percent",
            "effective_cpi_cut_percent", "starts_at", "ends_at", "is_active", "created_by",
            "created_at", "updated_at",
        ]
        read_only_fields = ["reserved_quantity", "consumed_quantity", "created_at", "updated_at"]

    def get_vendor_name(self, obj) -> str:
        return obj.vendor.get_full_name() or obj.vendor.username

    def validate(self, attrs):
        attrs = super().validate(attrs)
        vendor = attrs.get("vendor", getattr(self.instance, "vendor", None))
        quantity_limit = attrs.get("quantity_limit", getattr(self.instance, "quantity_limit", 0))
        cut = attrs.get("cpi_cut_override_percent", getattr(self.instance, "cpi_cut_override_percent", None))
        profile = EmployeeProfile.objects.filter(user=vendor).first()
        if not profile or profile.account_type not in {
            EmployeeProfile.AccountType.INTERNAL_VENDOR,
            EmployeeProfile.AccountType.EXTERNAL_VENDOR,
        }:
            raise serializers.ValidationError({"vendor": "Select an internal or external vendor account."})
        if profile.account_type == EmployeeProfile.AccountType.INTERNAL_VENDOR and cut not in {None, Decimal("0.00")}:
            raise serializers.ValidationError({"cpi_cut_override_percent": "Internal vendor cut must be zero."})
        instance = self.instance
        used = (instance.consumed_quantity + instance.reserved_quantity) if instance else 0
        if quantity_limit < used:
            raise serializers.ValidationError({"quantity_limit": "Limit cannot be below consumed plus reserved quantity."})
        starts_at = attrs.get("starts_at", getattr(instance, "starts_at", None))
        ends_at = attrs.get("ends_at", getattr(instance, "ends_at", None))
        if starts_at and ends_at and ends_at <= starts_at:
            raise serializers.ValidationError({"ends_at": "End time must be after start time."})
        return attrs


class VendorSurveyAllocationSerializer(serializers.ModelSerializer):
    vendor = serializers.IntegerField(source="client_allocation.vendor_id", read_only=True)
    vendor_name = serializers.SerializerMethodField()
    client = serializers.IntegerField(source="client_allocation.client_id", read_only=True)
    client_name = serializers.CharField(source="client_allocation.client.name", read_only=True)
    survey_local_id = serializers.CharField(source="survey.local_id", read_only=True)
    survey_source_id = serializers.IntegerField(source="survey.source_id", read_only=True)
    survey_name = serializers.CharField(source="survey.name", read_only=True)
    remaining_quantity = serializers.IntegerField(read_only=True)
    effective_cpi_cut_percent = serializers.DecimalField(max_digits=5, decimal_places=2, read_only=True)
    created_by = serializers.CharField(source="created_by.username", read_only=True, allow_null=True)

    class Meta:
        model = VendorSurveyAllocation
        fields = [
            "id", "client_allocation", "vendor", "vendor_name", "client", "client_name", "survey",
            "survey_local_id", "survey_source_id", "survey_name", "quantity_limit", "reserved_quantity",
            "consumed_quantity", "remaining_quantity", "cpi_cut_override_percent",
            "effective_cpi_cut_percent", "starts_at", "ends_at", "is_active", "created_by",
            "created_at", "updated_at",
        ]
        read_only_fields = ["reserved_quantity", "consumed_quantity", "created_at", "updated_at"]

    def get_vendor_name(self, obj) -> str:
        return obj.vendor.get_full_name() or obj.vendor.username

    def validate(self, attrs):
        attrs = super().validate(attrs)
        parent = attrs.get("client_allocation", getattr(self.instance, "client_allocation", None))
        survey = attrs.get("survey", getattr(self.instance, "survey", None))
        quantity_limit = attrs.get("quantity_limit", getattr(self.instance, "quantity_limit", 0))
        cut = attrs.get("cpi_cut_override_percent", getattr(self.instance, "cpi_cut_override_percent", None))
        if parent and survey and survey.client_id != parent.client_id:
            raise serializers.ValidationError({"survey": "Survey must belong to the parent allocation's client."})
        account_type = getattr(getattr(parent.vendor, "employee_profile", None), "account_type", "") if parent else ""
        if account_type == EmployeeProfile.AccountType.INTERNAL_VENDOR and cut not in {None, Decimal("0.00")}:
            raise serializers.ValidationError({"cpi_cut_override_percent": "Internal vendor cut must be zero."})
        instance = self.instance
        used = (instance.consumed_quantity + instance.reserved_quantity) if instance else 0
        if quantity_limit < used:
            raise serializers.ValidationError({"quantity_limit": "Limit cannot be below consumed plus reserved quantity."})
        starts_at = attrs.get("starts_at", getattr(instance, "starts_at", None))
        ends_at = attrs.get("ends_at", getattr(instance, "ends_at", None))
        if starts_at and ends_at and ends_at <= starts_at:
            raise serializers.ValidationError({"ends_at": "End time must be after start time."})
        return attrs


class AllocationReservationSerializer(serializers.ModelSerializer):
    rid = serializers.CharField(source="attempt.rid", read_only=True)
    vendor = serializers.IntegerField(source="client_allocation.vendor_id", read_only=True)
    survey = serializers.IntegerField(source="survey_allocation.survey_id", read_only=True, allow_null=True)

    class Meta:
        model = AllocationReservation
        fields = [
            "id", "attempt", "rid", "vendor", "client_allocation", "survey_allocation", "survey",
            "quantity", "status", "expires_at", "finalized_at", "reason", "created_at", "updated_at",
        ]
        read_only_fields = fields
