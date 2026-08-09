from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework import serializers

from accounts.models import EmployeeProfile

from .models import (
    AllocationReservation,
    Client,
    ClientIntegration,
    OrganizationClientAccess,
    OrganizationUnit,
    VendorClientAllocation,
    VendorCommercialProfile,
    VendorAPIKey,
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
    delivery_mode = serializers.CharField(source="vendor_commercial_profile.delivery_mode", read_only=True, allow_null=True)
    api_key_count = serializers.IntegerField(read_only=True)
    allocation_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = get_user_model()
        fields = [
            "id", "username", "full_name", "email", "account_type", "role_name", "created_by",
            "commercial_profile_id", "default_cpi_cut_percent", "currency", "delivery_mode",
            "allocation_count", "api_key_count",
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


class OrganizationOwnerOptionSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    username = serializers.CharField()
    type = serializers.ChoiceField(choices=[("owner", "Main office"), ("internal_vendor", "Internal vendor")])


class OrganizationManagementOptionsSerializer(serializers.Serializer):
    owners = OrganizationOwnerOptionSerializer(many=True)
    clients = VendorManagementClientOptionSerializer(many=True)
    client_eligibility = serializers.DictField(child=serializers.ListField(child=serializers.IntegerField()))


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


class OrganizationUnitSerializer(serializers.ModelSerializer):
    workspace_owner_name = serializers.SerializerMethodField()
    workspace_owner_type = serializers.SerializerMethodField()
    parent_name = serializers.CharField(source="parent.name", read_only=True, allow_null=True)
    path = serializers.CharField(source="path_label", read_only=True)
    member_count = serializers.IntegerField(read_only=True, default=0)
    client_count = serializers.IntegerField(read_only=True, default=0)
    created_by = serializers.CharField(source="created_by.username", read_only=True, allow_null=True)

    class Meta:
        model = OrganizationUnit
        fields = [
            "id", "workspace_owner", "workspace_owner_name", "workspace_owner_type", "parent", "parent_name",
            "unit_type", "name", "code", "description", "path", "member_count", "client_count",
            "is_active", "created_by", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_workspace_owner_name(self, obj) -> str:
        return obj.workspace_owner.get_full_name() or obj.workspace_owner.username

    def get_workspace_owner_type(self, obj) -> str:
        if obj.workspace_owner.is_superuser:
            return "owner"
        return getattr(getattr(obj.workspace_owner, "employee_profile", None), "account_type", "")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        request = self.context.get("request")
        owner = attrs.get("workspace_owner", getattr(self.instance, "workspace_owner", None))
        if request:
            from .access import organization_workspace_owner_ids
            if not owner or owner.pk not in organization_workspace_owner_ids(request.user):
                raise serializers.ValidationError({"workspace_owner": "You cannot manage this organization workspace."})
        if self.instance and "workspace_owner" in attrs and owner != self.instance.workspace_owner:
            raise serializers.ValidationError({"workspace_owner": "An organization unit cannot be moved to another workspace."})
        values = {
            "workspace_owner": owner,
            "parent": attrs.get("parent", getattr(self.instance, "parent", None)),
            "unit_type": attrs.get("unit_type", getattr(self.instance, "unit_type", None)),
            "name": attrs.get("name", getattr(self.instance, "name", "")),
            "code": attrs.get("code", getattr(self.instance, "code", "")),
            "description": attrs.get("description", getattr(self.instance, "description", "")),
            "is_active": attrs.get("is_active", getattr(self.instance, "is_active", True)),
        }
        candidate = OrganizationUnit(pk=getattr(self.instance, "pk", None), **values)
        try:
            candidate.full_clean(exclude=["created_by"])
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc
        return attrs


class OrganizationClientAccessSerializer(serializers.ModelSerializer):
    workspace_owner = serializers.IntegerField(source="organization_unit.workspace_owner_id", read_only=True)
    workspace_owner_name = serializers.SerializerMethodField()
    unit_name = serializers.CharField(source="organization_unit.name", read_only=True)
    unit_type = serializers.CharField(source="organization_unit.unit_type", read_only=True)
    unit_path = serializers.CharField(source="organization_unit.path_label", read_only=True)
    client_name = serializers.CharField(source="client.name", read_only=True)
    created_by = serializers.CharField(source="created_by.username", read_only=True, allow_null=True)

    class Meta:
        model = OrganizationClientAccess
        fields = [
            "id", "organization_unit", "workspace_owner", "workspace_owner_name", "unit_name", "unit_type",
            "unit_path", "client", "client_name", "is_active", "created_by", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_workspace_owner_name(self, obj) -> str:
        owner = obj.organization_unit.workspace_owner
        return owner.get_full_name() or owner.username

    def validate(self, attrs):
        attrs = super().validate(attrs)
        unit = attrs.get("organization_unit", getattr(self.instance, "organization_unit", None))
        client = attrs.get("client", getattr(self.instance, "client", None))
        request = self.context.get("request")
        if request:
            from .access import organization_workspace_owner_ids
            if not unit or unit.workspace_owner_id not in organization_workspace_owner_ids(request.user):
                raise serializers.ValidationError({"organization_unit": "You cannot manage this organization workspace."})
        candidate = OrganizationClientAccess(
            pk=getattr(self.instance, "pk", None),
            organization_unit=unit,
            client=client,
            is_active=attrs.get("is_active", getattr(self.instance, "is_active", True)),
        )
        try:
            candidate.full_clean(exclude=["created_by"])
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc
        return attrs


class VendorCommercialProfileSerializer(serializers.ModelSerializer):
    vendor_name = serializers.SerializerMethodField()
    account_type = serializers.CharField(source="vendor.employee_profile.account_type", read_only=True)
    created_by = serializers.CharField(source="created_by.username", read_only=True, allow_null=True)

    class Meta:
        model = VendorCommercialProfile
        fields = [
            "id", "vendor", "vendor_name", "account_type", "default_cpi_cut_percent", "currency",
            "delivery_mode", "is_active", "created_by", "created_at", "updated_at",
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
        delivery_mode = attrs.get("delivery_mode", getattr(self.instance, "delivery_mode", VendorCommercialProfile.DeliveryMode.PANEL))
        if profile.account_type == EmployeeProfile.AccountType.INTERNAL_VENDOR and delivery_mode != VendorCommercialProfile.DeliveryMode.PANEL:
            raise serializers.ValidationError({"delivery_mode": "Internal vendors use panel-only delivery."})
        return attrs


class VendorAPIKeySerializer(serializers.ModelSerializer):
    vendor_name = serializers.SerializerMethodField()
    account_type = serializers.CharField(source="vendor.employee_profile.account_type", read_only=True)
    masked_key = serializers.CharField(read_only=True)
    api_key = serializers.CharField(read_only=True, required=False)
    created_by = serializers.CharField(source="created_by.username", read_only=True, allow_null=True)

    class Meta:
        model = VendorAPIKey
        fields = [
            "id", "vendor", "vendor_name", "account_type", "name", "prefix", "last_four", "masked_key",
            "api_key", "is_active", "expires_at", "last_used_at", "revoked_at", "created_by",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "prefix", "last_four", "is_active", "last_used_at", "revoked_at", "created_at", "updated_at",
        ]

    def get_vendor_name(self, obj) -> str:
        return obj.vendor.get_full_name() or obj.vendor.username

    def validate(self, attrs):
        attrs = super().validate(attrs)
        vendor = attrs.get("vendor", getattr(self.instance, "vendor", None))
        if self.instance and "vendor" in attrs and attrs["vendor"] != self.instance.vendor:
            raise serializers.ValidationError({"vendor": "An issued API key cannot be transferred to another vendor."})
        profile = getattr(vendor, "employee_profile", None) if vendor else None
        if not profile or profile.account_type != EmployeeProfile.AccountType.EXTERNAL_VENDOR:
            raise serializers.ValidationError({"vendor": "API keys can only be issued to external vendors."})
        commercial = getattr(vendor, "vendor_commercial_profile", None)
        if not commercial or not commercial.is_active or not commercial.api_access_enabled:
            raise serializers.ValidationError({"vendor": "Enable API or Panel + API delivery before issuing a key."})
        expires_at = attrs.get("expires_at", getattr(self.instance, "expires_at", None))
        if expires_at and expires_at <= timezone.now():
            raise serializers.ValidationError({"expires_at": "Expiration must be in the future."})
        return attrs

    def create(self, validated_data):
        from .security import generate_api_key

        raw_key, prefix, last_four, key_hash = generate_api_key()
        request = self.context.get("request")
        instance = VendorAPIKey.objects.create(
            **validated_data,
            prefix=prefix,
            last_four=last_four,
            key_hash=key_hash,
            created_by=request.user if request else None,
        )
        instance.api_key = raw_key
        return instance


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
