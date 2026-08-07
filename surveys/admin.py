from django.contrib import admin

from .models import Survey, SurveyAttempt, SurveyQuota, SyncRun, TargetingQuestion


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
    list_display = ["local_id", "source_id", "company_name", "name", "country_code", "language_code", "completes", "sample_size", "status", "source_modified_at"]
    search_fields = ["local_id", "source_id", "company_name", "name"]
    list_filter = ["status", "company_name", "country_code", "language_code", "has_quota"]
    readonly_fields = ["local_id", "source_id", "raw_data", "created_at", "updated_at", "last_seen_at", "detail_synced_at"]
    inlines = [SurveyQuotaInline, TargetingQuestionInline]


@admin.register(SyncRun)
class SyncRunAdmin(admin.ModelAdmin):
    list_display = ["id", "started_at", "status", "unique_surveys", "created", "updated", "closed", "detail_failures"]
    readonly_fields = [field.name for field in SyncRun._meta.fields]


@admin.register(SurveyAttempt)
class SurveyAttemptAdmin(admin.ModelAdmin):
    list_display = ["rid", "survey", "user_id", "supplier_code", "status", "initiated_at", "loi_seconds", "callback_ip", "is_verified"]
    search_fields = ["rid", "user_id", "survey__local_id", "survey__source_id"]
    list_filter = ["status", "supplier_code", "is_verified", "initiated_at"]
    readonly_fields = [field.name for field in SurveyAttempt._meta.fields]
