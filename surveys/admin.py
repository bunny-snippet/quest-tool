"""Django Admin registration for inventory, mappings and attempt audits."""

from django.contrib import admin

from .models import (
    CintWebhookDelivery,
    CanonicalOption,
    CanonicalQuestion,
    ProviderOptionMapping,
    ProviderQuestionMapping,
    ProfileReuseEvent,
    ProfileReuseMonthlyCounter,
    Survey,
    SurveyAttempt,
    SurveyQuota,
    SyncRun,
    TargetingQuestion,
)


class SurveyQuotaInline(admin.TabularInline):
    model = SurveyQuota
    extra = 0
    readonly_fields = ["source_key", "quota_id", "title", "sample_size", "remaining", "status", "updated_at"]


class TargetingQuestionInline(admin.TabularInline):
    model = TargetingQuestion
    extra = 0
    readonly_fields = ["question_id", "key", "text", "question_type", "category", "updated_at"]


@admin.register(Survey)
class SurveyAdmin(admin.ModelAdmin):
    list_display = ["local_id", "client", "integration", "source_key", "company_name", "name", "country_code", "language_code", "completes", "sample_size", "status", "source_modified_at"]
    search_fields = ["local_id", "source_key", "source_id", "company_name", "name"]
    list_filter = ["status", "client", "integration", "company_name", "country_code", "language_code", "has_quota"]
    readonly_fields = ["local_id", "source_id", "source_key", "raw_data", "created_at", "updated_at", "last_seen_at", "detail_synced_at"]
    inlines = [SurveyQuotaInline, TargetingQuestionInline]


@admin.register(SyncRun)
class SyncRunAdmin(admin.ModelAdmin):
    list_display = ["id", "integration", "started_at", "status", "unique_surveys", "created", "updated", "closed", "detail_failures"]
    readonly_fields = [field.name for field in SyncRun._meta.fields]


@admin.register(CintWebhookDelivery)
class CintWebhookDeliveryAdmin(admin.ModelAdmin):
    list_display = [
        "id", "integration", "status", "item_count", "created_count",
        "updated_count", "closed_count", "skipped_count", "error_count",
        "received_at", "processed_at",
    ]
    search_fields = ["event_key", "payload_sha256", "signature_key_id", "error"]
    list_filter = ["status", "integration", "received_at"]
    readonly_fields = [field.name for field in CintWebhookDelivery._meta.fields]


@admin.register(SurveyAttempt)
class SurveyAttemptAdmin(admin.ModelAdmin):
    list_display = [
        "rid", "pid", "prescreener_uid", "provider_profile_uid", "survey", "platform_user", "vendor", "client", "status", "status_source", "initiated_at", "loi_seconds", "initiation_ip",
        "callback_ip", "entry_browser", "entry_device", "is_verified",
    ]
    search_fields = [
        "rid", "pid", "prescreener_uid", "provider_profile_uid", "user_id", "platform_user__username", "platform_user__email", "survey__local_id",
        "survey__source_key", "survey__source_id", "initiation_ip", "callback_ip",
    ]
    list_filter = ["status", "status_source", "supplier_code", "entry_device", "entry_browser", "is_verified", "initiated_at"]
    readonly_fields = [field.name for field in SurveyAttempt._meta.fields]


@admin.register(ProfileReuseMonthlyCounter)
class ProfileReuseMonthlyCounterAdmin(admin.ModelAdmin):
    list_display = [
        "integration", "period_start", "baseline_attempts", "target_reuses",
        "allocated_reuses", "updated_at",
    ]
    list_filter = ["integration", "period_start"]
    readonly_fields = [field.name for field in ProfileReuseMonthlyCounter._meta.fields]


@admin.register(ProfileReuseEvent)
class ProfileReuseEventAdmin(admin.ModelAdmin):
    list_display = [
        "attempt", "integration", "registered_uid", "reused_rid", "reused_uid",
        "country_code", "age_group", "gender", "source_usage_number", "created_at",
    ]
    search_fields = ["attempt__rid", "registered_uid", "reused_rid", "reused_uid"]
    list_filter = ["integration", "country_code", "age_group", "gender", "created_at"]
    readonly_fields = [field.name for field in ProfileReuseEvent._meta.fields]


class CanonicalOptionInline(admin.TabularInline):
    model = CanonicalOption
    extra = 0


@admin.register(CanonicalQuestion)
class CanonicalQuestionAdmin(admin.ModelAdmin):
    list_display = ["code", "label", "value_type", "is_active", "updated_at"]
    search_fields = ["code", "label", "description"]
    list_filter = ["value_type", "is_active"]
    inlines = [CanonicalOptionInline]


class ProviderOptionMappingInline(admin.TabularInline):
    model = ProviderOptionMapping
    extra = 0
    autocomplete_fields = ["canonical_option"]


@admin.register(ProviderQuestionMapping)
class ProviderQuestionMappingAdmin(admin.ModelAdmin):
    list_display = [
        "provider_code", "external_question_id", "external_question_key",
        "canonical_question", "country_code", "language_code", "is_active",
    ]
    search_fields = [
        "provider_code", "external_question_id", "external_question_key",
        "canonical_question__code", "canonical_question__label",
    ]
    list_filter = ["provider_code", "country_code", "language_code", "is_active"]
    autocomplete_fields = ["canonical_question"]
    inlines = [ProviderOptionMappingInline]


@admin.register(CanonicalOption)
class CanonicalOptionAdmin(admin.ModelAdmin):
    list_display = ["question", "code", "label", "normalized_value", "is_active"]
    search_fields = ["question__code", "code", "label", "normalized_value"]
    list_filter = ["question", "is_active"]
