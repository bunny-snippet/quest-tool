from urllib.parse import urlencode

from django.conf import settings
from django.urls import reverse
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from accounts.access import has_function_access
from vendors.access import is_external_vendor_scope, vendor_scope_user_id
from vendors.services import survey_pricing_for_user

from .models import Survey, SurveyAttempt, SurveyQuota, SyncRun, TargetingQuestion


class SurveyQuotaSerializer(serializers.ModelSerializer):
    class Meta:
        model = SurveyQuota
        fields = ["id", "quota_id", "title", "name", "sample_size", "remaining", "completes", "clicks", "status", "targeting", "updated_at"]


class TargetingQuestionSerializer(serializers.ModelSerializer):
    class Meta:
        model = TargetingQuestion
        fields = ["id", "question_id", "key", "text", "question_type", "category", "options", "updated_at"]


class SurveyListSerializer(serializers.ModelSerializer):
    client_name = serializers.CharField(source="client.name", read_only=True, allow_null=True)
    display_company_name = serializers.SerializerMethodField()
    country_label = serializers.SerializerMethodField()
    progress_percent = serializers.SerializerMethodField()
    source_created_display = serializers.SerializerMethodField()
    source_modified_display = serializers.SerializerMethodField()
    start_link = serializers.SerializerMethodField()
    cpi = serializers.SerializerMethodField()
    cpi_cut_percent = serializers.SerializerMethodField()
    vendor_pricing = serializers.SerializerMethodField()

    class Meta:
        model = Survey
        fields = [
            "id", "local_id", "client", "client_name", "display_company_name", "source_id", "company_name", "name", "status", "sample_size", "completes", "remaining",
            "starts", "cpi", "cpi_cut_percent", "vendor_pricing", "loi", "incidence_rate", "country", "country_code", "country_label",
            "language", "language_code", "group_type", "device_type", "entry_link", "start_link", "has_quota",
            "source_created_at", "source_modified_at", "source_created_display", "source_modified_display",
            "detail_synced_at", "quota_synced_at", "targeting_synced_at", "created_at", "updated_at",
            "progress_percent",
        ]

    def get_country_label(self, obj) -> str:
        return " ".join(part for part in [obj.country_code, obj.language_code] if part) or obj.country

    def get_display_company_name(self, obj) -> str:
        request = self.context.get("request")
        if request and vendor_scope_user_id(request.user) and obj.client:
            return obj.client.name
        return obj.company_name

    def get_progress_percent(self, obj) -> float:
        return round((obj.completes / obj.sample_size) * 100, 1) if obj.sample_size else 0

    def get_source_created_display(self, obj) -> str | None:
        return obj.raw_data.get("createdDate") or None

    def get_source_modified_display(self, obj) -> str | None:
        return obj.raw_data.get("modifiedDate") or None

    def _pricing(self, obj):
        request = self.context.get("request")
        return survey_pricing_for_user(request.user, obj) if request and request.user.is_authenticated else (obj.cpi, None)

    @extend_schema_field(serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True))
    def get_cpi(self, obj):
        return self._pricing(obj)[0]

    @extend_schema_field(serializers.DecimalField(max_digits=5, decimal_places=2, allow_null=True))
    def get_cpi_cut_percent(self, obj):
        return self._pricing(obj)[1]

    def get_vendor_pricing(self, obj) -> bool:
        request = self.context.get("request")
        return bool(request and vendor_scope_user_id(request.user))

    def get_start_link(self, obj) -> str | None:
        """Return the shareable platform pre-screener URL, never the supplier entry URL."""
        request = self.context.get("request")
        if not request or not request.user.is_authenticated or not has_function_access(request.user, "survey_links.copy"):
            return None
        if not obj.entry_link:
            return None
        query = urlencode({
            "surveyId": obj.source_id,
            "supplierCode": settings.PUBLIC_SUPPLIER_CODE,
            "userId": request.user.pk,
            "code": obj.local_id,
        })
        path = f"{reverse('survey-start')}?{query}"
        return request.build_absolute_uri(path) if request else path


class SurveyDetailSerializer(SurveyListSerializer):
    quotas = SurveyQuotaSerializer(many=True, read_only=True)
    targeting_questions = TargetingQuestionSerializer(many=True, read_only=True)

    class Meta(SurveyListSerializer.Meta):
        fields = SurveyListSerializer.Meta.fields + [
            "test_entry_link", "job_category", "is_pii_required", "is_recontact", "quotas", "targeting_questions"
        ]


class SyncRunSerializer(serializers.ModelSerializer):
    duration_seconds = serializers.SerializerMethodField()

    class Meta:
        model = SyncRun
        fields = [
            "id", "started_at", "finished_at", "duration_seconds", "status", "fetched_full", "fetched_paged",
            "unique_surveys", "created", "updated", "unchanged", "closed", "detail_failures", "error",
        ]

    def get_duration_seconds(self, obj) -> float | None:
        return round((obj.finished_at - obj.started_at).total_seconds(), 3) if obj.finished_at else None


class SyncTriggerResponseSerializer(serializers.Serializer):
    run_id = serializers.IntegerField()
    status = serializers.ChoiceField(choices=SyncRun.Status.choices)
    created = serializers.IntegerField()
    updated = serializers.IntegerField()
    unchanged = serializers.IntegerField()
    closed = serializers.IntegerField()
    detail_failures = serializers.IntegerField()


class UserHitDeviceCountsSerializer(serializers.Serializer):
    total = serializers.IntegerField(min_value=0)
    desktop = serializers.IntegerField(min_value=0)
    mobile = serializers.IntegerField(min_value=0)
    tablet = serializers.IntegerField(min_value=0)
    unclassified = serializers.IntegerField(min_value=0)


class UserHitRowSerializer(serializers.Serializer):
    user_id = serializers.IntegerField()
    user_name = serializers.CharField()
    username = serializers.CharField()
    user_email = serializers.EmailField(allow_blank=True)
    branch = serializers.CharField(allow_blank=True)
    sub_branch = serializers.CharField(allow_blank=True)
    date = serializers.DateField()
    hits = UserHitDeviceCountsSerializer()
    completes = UserHitDeviceCountsSerializer()


class UserHitSummarySerializer(serializers.Serializer):
    hits = UserHitDeviceCountsSerializer()
    completes = UserHitDeviceCountsSerializer()
    active_users = serializers.IntegerField(min_value=0)
    days = serializers.IntegerField(min_value=0)
    conversion_rate = serializers.FloatField(min_value=0)


class UserHitsResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField(min_value=0)
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = UserHitRowSerializer(many=True)
    summary = UserHitSummarySerializer()


class SurveyAttemptSerializer(serializers.ModelSerializer):
    survey_local_id = serializers.CharField(source="survey.local_id", read_only=True)
    survey_source_id = serializers.IntegerField(source="survey.source_id", read_only=True)
    survey_name = serializers.CharField(source="survey.name", read_only=True)
    company_name = serializers.CharField(source="survey.company_name", read_only=True)
    country_code = serializers.CharField(source="survey.country_code", read_only=True)
    language_code = serializers.CharField(source="survey.language_code", read_only=True)
    user_name = serializers.SerializerMethodField()
    username = serializers.CharField(source="platform_user.username", read_only=True, allow_null=True)
    user_email = serializers.EmailField(source="platform_user.email", read_only=True, allow_null=True)
    status_label = serializers.SerializerMethodField()
    entry_ip = serializers.IPAddressField(source="initiation_ip", read_only=True, allow_null=True)
    exit_ip = serializers.IPAddressField(source="callback_ip", read_only=True, allow_null=True)
    client_name = serializers.CharField(source="client.name", read_only=True, allow_null=True)
    vendor_name = serializers.SerializerMethodField()
    source_cpi_snapshot = serializers.SerializerMethodField()

    class Meta:
        model = SurveyAttempt
        fields = [
            "rid", "survey_local_id", "survey_source_id", "survey_name", "company_name", "country_code",
            "language_code", "platform_user", "user_id", "user_name", "username", "user_email", "vendor",
            "vendor_name", "client", "client_name", "client_allocation", "survey_allocation", "supplier_code",
            "source_cpi_snapshot", "cpi_cut_percent_snapshot", "payable_cpi_snapshot", "cpi_currency_snapshot",
            "status_label",
            "status", "initiated_at", "submitted_at", "redirected_at", "callback_at", "last_callback_at",
            "loi_seconds", "entry_ip", "exit_ip", "initiation_ip", "callback_ip", "entry_user_agent",
            "exit_user_agent", "entry_browser", "exit_browser", "entry_device", "exit_device", "entry_os",
            "exit_os", "entry_referrer", "entry_accept_language", "entry_client_data", "exit_client_data",
            "status_source", "upstream_checked_at", "upstream_transaction_data", "answers", "outbound_url", "callback_count",
            "is_verified", "created_at", "updated_at",
        ]

    def get_user_name(self, obj) -> str:
        if not obj.platform_user:
            return "Deleted user"
        return obj.platform_user.get_full_name() or obj.platform_user.username

    def get_vendor_name(self, obj) -> str | None:
        if not obj.vendor:
            return None
        return obj.vendor.get_full_name() or obj.vendor.username

    @extend_schema_field(serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True))
    def get_source_cpi_snapshot(self, obj):
        request = self.context.get("request")
        return None if request and is_external_vendor_scope(request.user) else obj.source_cpi_snapshot

    def get_status_label(self, obj) -> str:
        if obj.status in {SurveyAttempt.Status.INITIATED, SurveyAttempt.Status.REDIRECTED}:
            return "Initiated"
        return obj.get_status_display()
