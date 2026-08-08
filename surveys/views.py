import csv
import json
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import HttpResponseRedirect, StreamingHttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import filters, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.access import (
    HasFunctionPermission,
    effective_permission_codes,
    function_permission_required,
    has_function_access,
    subordinate_user_ids,
)

from .filters import SurveyAttemptFilter, SurveyFilter
from .integrations import InnovateMRAPIError, InnovateMRClient
from .models import Survey, SurveyAttempt, SyncRun
from .serializers import (
    SurveyDetailSerializer,
    SurveyListSerializer,
    SurveyAttemptSerializer,
    SurveyQuotaSerializer,
    SyncRunSerializer,
    SyncTriggerResponseSerializer,
    TargetingQuestionSerializer,
    UserHitsResponseSerializer,
)
from .pagination import SurveyPagination
from .services import replace_survey_quotas, replace_survey_targeting, sync_surveys
from .survey_flow import (
    backfill_attempt_entry_audit,
    build_outbound_url,
    create_attempt,
    get_request_client_data,
    get_request_ip,
    status_rid_from_request,
)
from .tasks import sync_innovatemr_surveys_task
from .user_hits import aggregate_user_hits, user_hit_filter_options


class UpstreamUnavailable(APIException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "InnovateMR is temporarily unavailable and no cached survey detail exists."
    default_code = "upstream_unavailable"


@function_permission_required("dashboard.view")
def dashboard_page(request):
    return render(request, "surveys/dashboard.html", {"active_page": "dashboard"})


@function_permission_required("projects.view")
def projects_page(request):
    countries = Survey.objects.exclude(country_code="").values_list("country_code", "country").distinct().order_by("country_code")
    companies = Survey.objects.exclude(company_name="").values_list("company_name", flat=True).distinct().order_by("company_name")
    column_permissions = {
        "project_id": "projects.column.project_id", "survey": "projects.column.survey",
        "market": "projects.column.market", "completes": "projects.column.completes",
        "cpi": "projects.column.cpi", "loi_ir": "projects.column.loi_ir",
        "entry_link": "projects.column.entry_link", "modified": "projects.column.modified",
        "actions": "projects.column.actions",
    }
    codes = effective_permission_codes(request.user)
    project_columns = [name for name, code in column_permissions.items() if code in codes]
    if "entry_link" in project_columns and "survey_links.copy" not in codes:
        project_columns.remove("entry_link")
    if "actions" in project_columns and "survey_details.view" not in codes:
        project_columns.remove("actions")
    return render(request, "surveys/projects.html", {
        "active_page": "projects", "countries": countries, "companies": companies,
        "project_columns": project_columns, "project_column_count": max(1, len(project_columns)),
        "can_sync": has_function_access(request.user, "sync.run"),
    })


@function_permission_required("attempts.view")
def studies_page(request):
    user_ids = subordinate_user_ids(request.user)
    user_ids.add(request.user.pk)
    if request.user.is_superuser:
        tracked_users = get_user_model().objects.filter(survey_attempts__isnull=False)
    else:
        tracked_users = get_user_model().objects.filter(pk__in=user_ids, survey_attempts__isnull=False)
    tracked_users = tracked_users.distinct().order_by("first_name", "last_name", "username")
    return render(request, "surveys/studies.html", {
        "active_page": "studies",
        "tracked_users": tracked_users,
        "attempt_statuses": [
            ("initiated,redirected", "Initiated"),
            (SurveyAttempt.Status.COMPLETED, "Completed"),
            (SurveyAttempt.Status.TERMINATED, "Terminated"),
            (SurveyAttempt.Status.OVER_QUOTA, "Over quota"),
            (SurveyAttempt.Status.QUALITY_TERMINATED, "Quality terminated"),
        ],
        "can_export": has_function_access(request.user, "attempts.export"),
    })


@function_permission_required("user_hits.view")
def user_hits_page(request):
    return render(request, "surveys/user_hits.html", {
        "active_page": "user-hits",
        **user_hit_filter_options(request.user),
    })


def workspace_home(request):
    if not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login
        return redirect_to_login(request.get_full_path())
    if has_function_access(request.user, "projects.view"):
        return HttpResponseRedirect(reverse("projects"))
    if has_function_access(request.user, "dashboard.view"):
        return HttpResponseRedirect(reverse("dashboard"))
    if has_function_access(request.user, "attempts.view"):
        return HttpResponseRedirect(reverse("studies"))
    if has_function_access(request.user, "user_hits.view"):
        return HttpResponseRedirect(reverse("user-hits"))
    if any(has_function_access(request.user, code) for code in ("access.manage", "users.view", "users.create", "roles.view", "roles.create")):
        return HttpResponseRedirect(reverse("access-control"))
    from django.core.exceptions import PermissionDenied
    raise PermissionDenied("No workspace page is assigned to this account.")


def _prescreener_questions(survey, submitted_data=None):
    prepared = []
    for question in survey.targeting_questions.all():
        lowered_type = question.question_type.lower()
        options = []
        age_ranges = []
        for option in question.options:
            option_id = option.get("OptionId")
            if option.get("ageStart") is not None:
                label = f"{option.get('ageStart')}–{option.get('ageEnd')}"
                age_ranges.append(option)
            else:
                label = option.get("OptionText") or str(option_id or "Option")
            options.append({"value": str(option_id or label), "label": label})
        if "multi" in lowered_type:
            input_kind = "checkbox"
        elif "single" in lowered_type and options:
            input_kind = "radio"
        elif question.key.upper() == "AGE" or "numeric" in lowered_type:
            input_kind = "number"
        else:
            input_kind = "text"
        field_name = f"question_{question.pk}"
        selected_values = submitted_data.getlist(field_name) if submitted_data is not None else []
        for option in options:
            option["selected"] = option["value"] in selected_values
        prepared.append({
            "model": question,
            "field_name": field_name,
            "input_kind": input_kind,
            "options": options,
            "current_value": selected_values[0] if selected_values else "",
            "min_value": min((int(item["ageStart"]) for item in age_ranges), default=None),
            "max_value": max((int(item["ageEnd"]) for item in age_ranges), default=None),
        })
    return prepared


def _collect_prescreener_answers(request, survey):
    answers = {}
    errors = []
    for prepared in _prescreener_questions(survey):
        question = prepared["model"]
        values = [value.strip() for value in request.POST.getlist(prepared["field_name"]) if value.strip()]
        if not values:
            errors.append(f"Please answer: {question.text or question.key}")
            continue

        valid_options = {item["value"] for item in prepared["options"]}
        upstream_values = values.copy()
        if prepared["input_kind"] in {"radio", "checkbox"}:
            invalid = [value for value in values if value not in valid_options]
            if invalid:
                errors.append(f"Invalid answer for: {question.text or question.key}")
                continue
        elif prepared["input_kind"] == "number":
            try:
                numeric_value = int(values[0])
            except ValueError:
                errors.append(f"Enter a valid number for: {question.text or question.key}")
                continue
            matched = [
                str(option.get("OptionId"))
                for option in question.options
                if option.get("ageStart") is not None
                and int(option["ageStart"]) <= numeric_value <= int(option["ageEnd"])
                and option.get("OptionId") is not None
            ]
            upstream_values = matched or [str(numeric_value)]

        answers[str(question.pk)] = {
            "question_id": question.question_id,
            "question_key": question.key,
            "question_text": question.text,
            "values": values,
            "upstream_values": upstream_values,
        }
    return answers, errors


def _invalid_survey_link(request, message="This link is invalid or is no longer available.", status_code=400):
    return render(request, "surveys/flow_error.html", {
        "title": "Invalid survey link",
        "message": message,
    }, status=status_code)


def _has_exact_query(request, expected_names):
    """Reject duplicated or client-injected start-link parameters."""
    return set(request.GET.keys()) == set(expected_names) and all(
        len(request.GET.getlist(name)) == 1 for name in expected_names
    )


@require_http_methods(["GET", "POST"])
def survey_start(request):
    if request.method == "GET" and not request.GET.get("rid"):
        required_params = {"surveyId", "supplierCode", "userId", "code"}
        if not _has_exact_query(request, required_params):
            return _invalid_survey_link(request)

        survey_id = request.GET.get("surveyId", "").strip()
        supplier_code = request.GET.get("supplierCode", "").strip()
        internal_code = request.GET.get("code", "").strip()
        user_id = request.GET.get("userId", "").strip()
        if (
            not survey_id.isdigit()
            or not user_id.isdigit()
            or not internal_code.isdigit()
            or len(internal_code) != 14
            or supplier_code != settings.PUBLIC_SUPPLIER_CODE
        ):
            return _invalid_survey_link(request)

        platform_user = get_user_model().objects.filter(pk=int(user_id), is_active=True).first()
        if (
            platform_user is None
            or not has_function_access(platform_user, "projects.view")
            or not has_function_access(platform_user, "survey_links.copy")
        ):
            return _invalid_survey_link(request)

        survey = Survey.objects.filter(
            source_id=int(survey_id), local_id=internal_code, status=Survey.Status.LIVE
        ).first()
        if survey is None or not survey.entry_link:
            return _invalid_survey_link(request)

        stale = survey.targeting_synced_at is None or (
            survey.source_modified_at and survey.targeting_synced_at < survey.source_modified_at
        )
        targeting_warning = ""
        if stale:
            try:
                replace_survey_targeting(InnovateMRClient(), survey)
            except InnovateMRAPIError:
                if not survey.targeting_questions.exists():
                    targeting_warning = "Pre-screening criteria are temporarily unavailable. You can still continue."
        attempt = create_attempt(
            survey,
            platform_user,
            get_request_ip(request),
            client_data=get_request_client_data(request),
        )
        if targeting_warning:
            request.session[f"attempt_warning_{attempt.rid}"] = targeting_warning
        return HttpResponseRedirect(f"{reverse('survey-start')}?rid={quote(attempt.rid)}")

    if request.method == "GET" and not _has_exact_query(request, {"rid"}):
        return _invalid_survey_link(request)

    rid = (request.GET.get("rid", "") if request.method == "GET" else request.POST.get("rid", "")).strip()
    if len(rid) != 10 or not rid.isalnum():
        return _invalid_survey_link(request)
    attempt = SurveyAttempt.objects.select_related("survey", "platform_user").filter(rid=rid).first()
    if attempt is None or attempt.platform_user is None or not attempt.platform_user.is_active:
        return _invalid_survey_link(request, status_code=404)
    attempt = backfill_attempt_entry_audit(attempt, request)

    if request.method == "POST":
        answers, errors = _collect_prescreener_answers(request, attempt.survey)
        if not errors:
            with transaction.atomic():
                locked = SurveyAttempt.objects.select_for_update().select_related("survey").get(pk=attempt.pk)
                if locked.status != SurveyAttempt.Status.INITIATED:
                    return HttpResponseRedirect(f"{reverse('survey-start')}?rid={quote(locked.rid)}")
                outbound_url = build_outbound_url(locked.survey.entry_link, locked.rid, answers)
                now = timezone.now()
                locked.answers = answers
                locked.submitted_at = now
                locked.redirected_at = now
                locked.outbound_url = outbound_url
                locked.status = SurveyAttempt.Status.REDIRECTED
                locked.save(update_fields=["answers", "submitted_at", "redirected_at", "outbound_url", "status", "updated_at"])
            return HttpResponseRedirect(outbound_url)
    else:
        errors = []

    if attempt.status != SurveyAttempt.Status.INITIATED:
        return render(request, "surveys/status.html", {
            "title": "Survey already initiated",
            "message": "This RID has already been used to enter the survey.",
            "tone": "info",
            "status_label": attempt.get_status_display(),
            "rid": attempt.rid,
            "ip_address": attempt.callback_ip or attempt.initiation_ip,
            "loi_seconds": attempt.loi_seconds,
            "attempt_found": True,
        })

    return render(request, "surveys/prescreener.html", {
        "attempt": attempt,
        "survey": attempt.survey,
        "questions": _prescreener_questions(attempt.survey, request.POST if request.method == "POST" else None),
        "errors": errors,
        "warning": request.session.pop(f"attempt_warning_{attempt.rid}", ""),
    })


STATUS_PAGES = {
    "1": {"title": "Thank you for participating!", "message": "Your survey response has been completed successfully.", "tone": "success"},
    "2": {"title": "Survey ended", "message": "Your profile did not match the remaining survey requirements.", "tone": "neutral"},
    "3": {"title": "Quota already filled", "message": "The required quota was filled before your response could be completed.", "tone": "warning"},
    "4": {"title": "Quality check unsuccessful", "message": "This response did not pass the survey's quality checks.", "tone": "danger"},
}


@require_http_methods(["GET"])
def survey_status(request):
    status_code = request.GET.get("status", "").strip()
    rid = status_rid_from_request(request)
    page = STATUS_PAGES.get(status_code)
    if page is None or not rid:
        return render(request, "surveys/flow_error.html", {
            "title": "Invalid survey status",
            "message": "A valid status (1–4) and RID are required.",
        }, status=400)

    attempt = SurveyAttempt.objects.filter(rid=rid).first()
    ip_address = get_request_ip(request)
    if attempt:
        with transaction.atomic():
            attempt = SurveyAttempt.objects.select_for_update().get(pk=attempt.pk)
            now = timezone.now()
            exit_client_data = get_request_client_data(request)
            if attempt.callback_at is None:
                attempt.callback_at = now
                attempt.callback_ip = ip_address
                attempt.loi_seconds = max(0, int((now - attempt.initiated_at).total_seconds()))
                attempt.status = status_code
                attempt.exit_user_agent = exit_client_data.get("user_agent", "")
                attempt.exit_browser = exit_client_data.get("browser", "")
                attempt.exit_device = exit_client_data.get("device", "")
                attempt.exit_os = exit_client_data.get("os", "")
                attempt.exit_client_data = exit_client_data
                attempt.status_source = "browser_callback"
            attempt.last_callback_at = now
            attempt.callback_count += 1
            attempt.save(update_fields=[
                "callback_at", "callback_ip", "loi_seconds", "status", "exit_user_agent", "exit_browser",
                "exit_device", "exit_os", "exit_client_data", "status_source", "last_callback_at",
                "callback_count", "updated_at"
            ])
        status_label = attempt.get_status_display()
    else:
        status_label = "Unknown attempt"

    return render(request, "surveys/status.html", {
        **page,
        "status_label": status_label,
        "rid": rid,
        "ip_address": ip_address,
        "loi_seconds": attempt.loi_seconds if attempt else None,
        "attempt_found": bool(attempt),
    }, status=200 if attempt else 404)


@extend_schema_view(
    list=extend_schema(
        tags=["Surveys"],
        summary="List synchronized surveys",
        description=(
            "Returns locally stored surveys using page-number pagination. Search matches project ID, InnovateMR survey ID, "
            "survey name, country and category. Date filters accept ISO-8601 timestamps."
        ),
        parameters=[
            OpenApiParameter("search", OpenApiTypes.STR, description="Free-text search across survey identifiers and descriptive fields."),
            OpenApiParameter("ordering", OpenApiTypes.STR, description="One of source_modified_at, source_created_at, cpi, sample_size, completes, created_at; prefix '-' for descending."),
            OpenApiParameter("page", OpenApiTypes.INT, description="1-based result page."),
            OpenApiParameter("page_size", OpenApiTypes.INT, description="Rows per page (1–100, default 20)."),
        ],
    ),
    retrieve=extend_schema(
        tags=["Surveys"],
        summary="Get one survey",
        description="Looks up a survey by the platform's immutable 14-digit local_id and embeds current quotas and targeting questions.",
    ),
)
class SurveyViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Survey.objects.select_related("client").all().prefetch_related("quotas", "targeting_questions")
    lookup_field = "local_id"
    filterset_class = SurveyFilter
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["local_id", "=source_id", "name", "company_name", "country", "country_code", "job_category"]
    ordering_fields = ["source_modified_at", "source_created_at", "cpi", "sample_size", "completes", "created_at"]
    ordering = ["-source_modified_at", "-created_at"]
    permission_classes = [HasFunctionPermission]

    def get_required_function_permission(self):
        return "survey_details.view" if self.action in {"retrieve", "quotas", "targeting"} else "projects.view"

    def get_serializer_class(self):
        return SurveyDetailSerializer if self.action == "retrieve" else SurveyListSerializer

    @staticmethod
    def _refresh_if_stale(survey, detail_type):
        synced_at = survey.quota_synced_at if detail_type == "quotas" else survey.targeting_synced_at
        stale = synced_at is None or (
            survey.source_modified_at is not None and synced_at < survey.source_modified_at
        )
        if stale:
            refresh = replace_survey_quotas if detail_type == "quotas" else replace_survey_targeting
            refresh(InnovateMRClient(), survey)

    @extend_schema(
        tags=["Survey details"],
        summary="List a survey's quotas",
        description="Returns the most recently synchronized getQuotaForSurvey result for this survey.",
        responses={200: SurveyQuotaSerializer(many=True)},
    )
    @action(detail=True, methods=["get"])
    def quotas(self, request, local_id=None):
        survey = self.get_object()
        try:
            self._refresh_if_stale(survey, "quotas")
        except InnovateMRAPIError as exc:
            if survey.quota_synced_at is None:
                raise UpstreamUnavailable(str(exc)) from exc
        return Response(SurveyQuotaSerializer(survey.quotas.all(), many=True).data)

    @extend_schema(
        tags=["Survey details"],
        summary="List pre-screening questions and accepted answers",
        description="Returns the most recently synchronized getSurveyTargeting result. Options preserve InnovateMR's source structure.",
        responses={200: TargetingQuestionSerializer(many=True)},
    )
    @action(detail=True, methods=["get"], url_path="targeting")
    def targeting(self, request, local_id=None):
        survey = self.get_object()
        try:
            self._refresh_if_stale(survey, "targeting")
        except InnovateMRAPIError as exc:
            if survey.targeting_synced_at is None:
                raise UpstreamUnavailable(str(exc)) from exc
        return Response(TargetingQuestionSerializer(survey.targeting_questions.all(), many=True).data)


class SyncTriggerView(APIView):
    permission_classes = [HasFunctionPermission]
    required_function_permission = "sync.run"
    @extend_schema(
        tags=["Synchronization"],
        summary="Start an InnovateMR inventory synchronization",
        description=(
            "By default queues the same Celery task that beat runs every minute. Use wait=true for operational testing to run in the HTTP process "
            "and receive counters immediately. The sync fetches both full and cursor-paged inventory, deduplicates by surveyId using modifiedDate, "
            "and refreshes quota/targeting only for new or changed surveys."
        ),
        parameters=[OpenApiParameter("wait", OpenApiTypes.BOOL, description="Run synchronously and return the completed run summary.")],
        request=None,
        responses={200: SyncTriggerResponseSerializer, 202: OpenApiTypes.OBJECT},
        examples=[OpenApiExample("Synchronous result", value={"run_id": 42, "status": "success", "created": 3, "updated": 8, "unchanged": 110, "closed": 2, "detail_failures": 0}, response_only=True)],
    )
    def post(self, request):
        wait = str(request.query_params.get("wait", "false")).lower() in {"1", "true", "yes"}
        if wait:
            try:
                summary = sync_surveys()
            except InnovateMRAPIError as exc:
                raise UpstreamUnavailable(str(exc)) from exc
            return Response(SyncTriggerResponseSerializer(summary.__dict__).data)
        task = sync_innovatemr_surveys_task.delay()
        return Response({"task_id": task.id, "status": "queued"}, status=status.HTTP_202_ACCEPTED)


class SyncRunViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = SyncRun.objects.all()
    serializer_class = SyncRunSerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["status"]
    ordering_fields = ["started_at", "finished_at", "created", "updated", "detail_failures"]
    ordering = ["-started_at"]
    permission_classes = [HasFunctionPermission]
    required_function_permission = "sync.view"

    @extend_schema(tags=["Synchronization"], summary="List synchronization audit runs")
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(tags=["Synchronization"], summary="Get one synchronization audit run")
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)


@extend_schema_view(
    list=extend_schema(
        tags=["Survey attempts"],
        summary="List respondent survey attempts",
        description="Staff-only audit data for initiated pre-screeners, redirects, callbacks, IPs and measured LOI.",
    ),
    retrieve=extend_schema(
        tags=["Survey attempts"],
        summary="Get one respondent attempt by RID",
        description="Staff-only detail including captured answers and outbound supplier URL.",
    ),
)
class SurveyAttemptViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = SurveyAttemptSerializer
    permission_classes = [HasFunctionPermission]
    lookup_field = "rid"
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = SurveyAttemptFilter
    search_fields = [
        "rid", "user_id", "survey__local_id", "=survey__source_id", "survey__name", "survey__company_name",
        "platform_user__username", "platform_user__first_name", "platform_user__last_name", "platform_user__email",
        "initiation_ip", "callback_ip", "entry_browser", "entry_device", "entry_os",
    ]
    ordering_fields = ["initiated_at", "callback_at", "loi_seconds", "status"]
    ordering = ["-initiated_at"]

    def get_required_function_permission(self):
        return "attempts.export" if self.action == "export" else "attempts.view"

    def get_queryset(self):
        queryset = SurveyAttempt.objects.select_related(
            "survey", "platform_user", "platform_user__employee_profile", "platform_user__employee_profile__role",
            "vendor", "vendor__employee_profile", "client", "client_allocation", "survey_allocation",
        ).all()
        if self.request.user.is_superuser:
            return queryset
        visible_user_ids = subordinate_user_ids(self.request.user)
        visible_user_ids.add(self.request.user.pk)
        return queryset.filter(platform_user_id__in=visible_user_ids)

    @extend_schema(
        tags=["Survey attempts"],
        summary="Export all filtered survey attempt data",
        description=(
            "Downloads every field associated with the currently filtered attempts, including user and survey "
            "identifiers, entry/exit network metadata, client metadata, timestamps, answers and callback audit data."
        ),
        parameters=[
            OpenApiParameter("search", OpenApiTypes.STR, description="Search RID, user, survey, IP or client metadata."),
            OpenApiParameter("user", OpenApiTypes.STR, description="Comma-separated platform user IDs."),
            OpenApiParameter("status", OpenApiTypes.STR, description="Comma-separated attempt status codes."),
            OpenApiParameter("company", OpenApiTypes.STR, description="Comma-separated survey company names."),
            OpenApiParameter("survey_id", OpenApiTypes.INT, description="Exact upstream survey ID."),
            OpenApiParameter("internal_id", OpenApiTypes.STR, description="Exact internal 14-digit project ID."),
            OpenApiParameter("entry_ip", OpenApiTypes.STR, description="Exact entry IP address."),
            OpenApiParameter("exit_ip", OpenApiTypes.STR, description="Exact exit IP address."),
            OpenApiParameter("initiated_from", OpenApiTypes.DATETIME, description="Entry timestamp lower bound (ISO 8601)."),
            OpenApiParameter("initiated_to", OpenApiTypes.DATETIME, description="Entry timestamp upper bound (ISO 8601)."),
            OpenApiParameter("callback_from", OpenApiTypes.DATETIME, description="Exit timestamp lower bound (ISO 8601)."),
            OpenApiParameter("callback_to", OpenApiTypes.DATETIME, description="Exit timestamp upper bound (ISO 8601)."),
            OpenApiParameter("ordering", OpenApiTypes.STR, description="Sort by initiated_at, callback_at, loi_seconds or status; prefix - for descending."),
        ],
        responses={(200, "text/csv"): OpenApiTypes.BINARY},
    )
    @action(detail=False, methods=["get"], url_path="export")
    def export(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        local_now = timezone.localtime()
        response = StreamingHttpResponse(
            _attempt_csv_rows(queryset),
            content_type="text/csv; charset=utf-8",
        )
        response["Content-Disposition"] = f'attachment; filename="studies-{local_now:%Y%m%d-%H%M%S}-IST.csv"'
        response["X-Content-Type-Options"] = "nosniff"
        return response


class UserHitsAPIView(APIView):
    permission_classes = [HasFunctionPermission]
    required_function_permission = "user_hits.view"

    @extend_schema(
        tags=["User hits"],
        summary="Aggregate user survey hits and completes by IST date and device",
        description=(
            "Returns one row per visible user and IST calendar date. Hits count initiated survey attempts; "
            "completes count status 1 within those attempts. Device splits use entry-device audit data."
        ),
        parameters=[
            OpenApiParameter("search", OpenApiTypes.STR, description="Search user, email, branch or sub-branch."),
            OpenApiParameter("user", OpenApiTypes.STR, description="Comma-separated platform user IDs."),
            OpenApiParameter("branch", OpenApiTypes.STR, description="Comma-separated branch/company labels."),
            OpenApiParameter("sub_branch", OpenApiTypes.STR, description="Comma-separated sub-branch/department labels."),
            OpenApiParameter("from_date", OpenApiTypes.DATE, description="Inclusive IST entry date."),
            OpenApiParameter("to_date", OpenApiTypes.DATE, description="Inclusive IST entry date."),
            OpenApiParameter("from_time", OpenApiTypes.TIME, description="Optional inclusive IST start time; requires from_date."),
            OpenApiParameter("to_time", OpenApiTypes.TIME, description="Optional inclusive IST end time; requires to_date."),
            OpenApiParameter("page", OpenApiTypes.INT, description="1-based aggregate result page."),
            OpenApiParameter("page_size", OpenApiTypes.INT, description="Rows per page, 1–100."),
        ],
        responses={200: UserHitsResponseSerializer},
    )
    def get(self, request):
        try:
            rows, summary = aggregate_user_hits(request.user, request.query_params)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        paginator = SurveyPagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        response = paginator.get_paginated_response(page)
        response.data["summary"] = summary
        return response


class _CsvEcho:
    def write(self, value):
        return value


def _csv_safe(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    elif hasattr(value, "isoformat"):
        value = timezone.localtime(value).isoformat() if timezone.is_aware(value) else value.isoformat()
    else:
        value = str(value)
    return f"'{value}" if value.startswith(("=", "+", "-", "@")) else value


def _attempt_csv_rows(queryset):
    headers = [
        "Respondent ID (RID)", "Status code", "Status", "Status source", "Platform user ID", "Username", "Employee name",
        "Email", "Employee ID", "Account type", "Role", "Vendor ID", "Vendor name", "Vendor account type",
        "Client ID", "Client name", "Client allocation ID", "Survey allocation ID",
        "Internal project ID", "Survey ID", "Survey name", "Company", "Country", "Language", "Supplier code",
        "Current survey CPI", "Source CPI snapshot", "CPI cut snapshot (%)", "Payable CPI snapshot",
        "CPI currency snapshot", "Expected LOI (minutes)",
        "Actual LOI (seconds)", "Entry IP", "Exit IP", "Entry browser", "Exit browser", "Entry device",
        "Exit device", "Entry OS", "Exit OS", "Entry user agent", "Exit user agent", "Entry referrer",
        "Entry accept language", "Initiated at (IST)", "Pre-screener submitted at (IST)",
        "Redirected at (IST)", "First callback at (IST)", "Last callback at (IST)", "Callback count",
        "Verified", "Last upstream check (IST)", "Upstream transaction", "Pre-screener answers",
        "Outbound supplier URL", "Entry client metadata", "Exit client metadata", "Record created at (IST)",
        "Record updated at (IST)",
    ]
    writer = csv.writer(_CsvEcho())
    yield "\ufeff" + writer.writerow(headers)
    for attempt in queryset.iterator(chunk_size=1000):
        user = attempt.platform_user
        profile = getattr(user, "employee_profile", None) if user else None
        role = getattr(profile, "role", None) if profile else None
        vendor = attempt.vendor
        vendor_profile = getattr(vendor, "employee_profile", None) if vendor else None
        survey = attempt.survey
        values = [
            attempt.rid, attempt.status,
            "Initiated" if attempt.status in {SurveyAttempt.Status.INITIATED, SurveyAttempt.Status.REDIRECTED} else attempt.get_status_display(),
            attempt.status_source, user.pk if user else attempt.user_id,
            user.username if user else "", (user.get_full_name() or user.username) if user else "Deleted user",
            user.email if user else "", getattr(profile, "employee_id", ""),
            profile.get_account_type_display() if profile else "", role.name if role else "",
            vendor.pk if vendor else "", (vendor.get_full_name() or vendor.username) if vendor else "",
            vendor_profile.get_account_type_display() if vendor_profile else "",
            attempt.client_id, attempt.client.name if attempt.client else "", attempt.client_allocation_id,
            attempt.survey_allocation_id,
            survey.local_id, survey.source_id, survey.name, survey.company_name, survey.country_code,
            survey.language_code, attempt.supplier_code, survey.cpi, attempt.source_cpi_snapshot,
            attempt.cpi_cut_percent_snapshot, attempt.payable_cpi_snapshot, attempt.cpi_currency_snapshot,
            survey.loi, attempt.loi_seconds,
            attempt.initiation_ip, attempt.callback_ip, attempt.entry_browser, attempt.exit_browser,
            attempt.entry_device, attempt.exit_device, attempt.entry_os, attempt.exit_os,
            attempt.entry_user_agent, attempt.exit_user_agent, attempt.entry_referrer,
            attempt.entry_accept_language, attempt.initiated_at, attempt.submitted_at, attempt.redirected_at,
            attempt.callback_at, attempt.last_callback_at, attempt.callback_count, attempt.is_verified,
            attempt.upstream_checked_at, attempt.upstream_transaction_data, attempt.answers, attempt.outbound_url,
            attempt.entry_client_data, attempt.exit_client_data,
            attempt.created_at, attempt.updated_at,
        ]
        yield writer.writerow([_csv_safe(value) for value in values])
