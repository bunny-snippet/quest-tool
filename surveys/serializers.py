from urllib.parse import urlencode

from django.urls import reverse
from rest_framework import serializers

from accounts.access import has_function_access

from .models import Survey, SurveyAttempt, SurveyQuota, SyncRun, TargetingQuestion
from .survey_flow import supplier_code_from_entry_link


class SurveyQuotaSerializer(serializers.ModelSerializer):
    class Meta:
        model = SurveyQuota
        fields = ["id", "quota_id", "title", "name", "sample_size", "remaining", "completes", "clicks", "status", "targeting", "updated_at"]


class TargetingQuestionSerializer(serializers.ModelSerializer):
    class Meta:
        model = TargetingQuestion
        fields = ["id", "question_id", "key", "text", "question_type", "category", "options", "updated_at"]


class SurveyListSerializer(serializers.ModelSerializer):
    country_label = serializers.SerializerMethodField()
    progress_percent = serializers.SerializerMethodField()
    source_created_display = serializers.SerializerMethodField()
    source_modified_display = serializers.SerializerMethodField()
    start_link = serializers.SerializerMethodField()

    class Meta:
        model = Survey
        fields = [
            "local_id", "source_id", "company_name", "name", "status", "sample_size", "completes", "remaining",
            "starts", "cpi", "loi", "incidence_rate", "country", "country_code", "country_label",
            "language", "language_code", "group_type", "device_type", "entry_link", "start_link", "has_quota",
            "source_created_at", "source_modified_at", "source_created_display", "source_modified_display",
            "detail_synced_at", "quota_synced_at", "targeting_synced_at", "created_at", "updated_at",
            "progress_percent",
        ]

    def get_country_label(self, obj) -> str:
        return " ".join(part for part in [obj.country_code, obj.language_code] if part) or obj.country

    def get_progress_percent(self, obj) -> float:
        return round((obj.completes / obj.sample_size) * 100, 1) if obj.sample_size else 0

    def get_source_created_display(self, obj) -> str | None:
        return obj.raw_data.get("createdDate") or None

    def get_source_modified_display(self, obj) -> str | None:
        return obj.raw_data.get("modifiedDate") or None

    def get_start_link(self, obj) -> str | None:
        """Return the shareable platform pre-screener URL, never the supplier entry URL."""
        request = self.context.get("request")
        if not request or not request.user.is_authenticated or not has_function_access(request.user, "survey_links.copy"):
            return None
        if not obj.entry_link:
            return None
        query = urlencode({
            "surveyId": obj.source_id,
            "supplierCode": supplier_code_from_entry_link(obj.entry_link),
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


class SurveyAttemptSerializer(serializers.ModelSerializer):
    survey_local_id = serializers.CharField(source="survey.local_id", read_only=True)
    survey_source_id = serializers.IntegerField(source="survey.source_id", read_only=True)
    company_name = serializers.CharField(source="survey.company_name", read_only=True)

    class Meta:
        model = SurveyAttempt
        fields = [
            "rid", "survey_local_id", "survey_source_id", "company_name", "platform_user", "user_id", "supplier_code",
            "status", "initiated_at", "submitted_at", "redirected_at", "callback_at", "last_callback_at",
            "loi_seconds", "initiation_ip", "callback_ip", "answers", "outbound_url", "callback_count",
            "is_verified", "created_at", "updated_at",
        ]
