"""Workspace pages, respondent flow, callbacks, reports, exports and REST APIs.

Business writes are delegated to survey/provider/allocation/vault services where
possible. Public respondent endpoints live here because they coordinate several
of those services inside one guarded request lifecycle.
"""

import csv
import hmac
import ipaddress
import json
import logging
import re
import secrets
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.db import DatabaseError, transaction
from django.db.models import Count, IntegerField, Max, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect, JsonResponse, StreamingHttpResponse
from django.core.paginator import Paginator
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import filters, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.access import (
    HasFunctionPermission,
    activity_visible_user_ids,
    effective_permission_codes,
    function_permission_required,
    has_function_access,
    is_super_admin_account,
)
from vendors.services import (
    AllocationUnavailable,
    annotate_survey_pricing_for_user,
    finalize_attempt_capacity,
    reserve_attempt_capacity,
    resolve_vendor_survey_context,
    organization_client_ids_for_user,
    scope_surveys_for_api_key,
    scope_surveys_for_user,
)
from vendors.access import is_external_vendor_scope, vendor_scope_user_id
from vendors.models import Client, VerisoulAssessment, VendorAPIKey
from vendors.security import decode_delivery_token
from vendors.verisoul import (
    VerisoulError,
    authenticate_verisoul_session,
    effective_verisoul_policy,
    verisoul_sdk_url,
)
from config.filter_backends import SparseDjangoFilterBackend

from .filters import SurveyAttemptFilter, SurveyFilter
from .dashboard import (
    build_dashboard_payload,
    dashboard_attempts,
    dashboard_client_options,
    dashboard_financial_year_options,
    dashboard_range_window,
)
from .excel import ExcelSheet, build_excel_response
from .entry_tokens import (
    EntryTokenError,
    decode_entry_token,
    decode_journey_token,
    issue_journey_token,
)
from .integrations import InnovateMRAPIError, InnovateMRClient
from .identifiers import is_valid_platform_pid
from .innovatemr_callbacks import verify_callback_request
from .models import CanonicalQuestion, ExportJob, FinalIDUpload, ProviderQuestionMapping, Survey, SurveyAttempt, SyncRun
from .final_ids import FinalIDImportError, import_final_ids
from .outcomes import provider_outcome, termination_origin
from .report_pricing import (
    apply_percentage,
    can_view_report_commercials,
    role_visibility_percent,
    supplier_cpi_for_admin,
    supplier_label_for_admin,
    viewer_attempt_cpi,
)
from .serializers import (
    SurveyDetailSerializer,
    DashboardResponseSerializer,
    CanonicalQuestionSerializer,
    ProviderQuestionMappingSerializer,
    SurveyListSerializer,
    SurveyAttemptSerializer,
    SurveyAttemptListSerializer,
    SurveyAttemptListResponseSerializer,
    SurveyQuotaSerializer,
    RFGCallbackResponseSerializer,
    SyncRunSerializer,
    SyncTriggerResponseSerializer,
    TargetingQuestionSerializer,
    UserHitsResponseSerializer,
)
from .pagination import SurveyPagination
from .project_cache import invalidate_project_cache, project_filter_metadata
from .report_cache import (
    cached_report_payload,
    cached_user_metadata,
    term_filter_metadata,
    traffic_filter_metadata,
)
from .supplier_callbacks import (
    build_supplier_result_url,
    queue_supplier_result_callback,
)
from prescreener_vault.services import (
    PrescreenerVaultError,
    answers_with_entry_postal_code,
    capture_prescreener_submission,
    operational_answer_value,
    wrong_target_country_answers,
)
from prescreener_vault.cint_email_pool import CintEmailPoolExhausted
from prescreener_vault.models import PrescreenerSubmission
from prescreener_vault.reuse import maybe_assign_reusable_profile
from prescreener_vault.cache import (
    apply_submission_filters,
    vault_filter_options,
    vault_filtered_summary,
)
from .providers import ProviderError, ProviderSurveyUnavailable, get_provider, has_provider
from .geolocation import (
    geolocation_client_data,
    is_wrong_target_country,
    resolve_entry_geolocation,
    survey_target_country_code,
)
from .rfg_outcomes import RFG_STATUS_MAP, describe_rfg_outcome
from .rfg_text import clean_rfg_display_text
from .services import reconcile_attempt_status, replace_survey_quotas, replace_survey_targeting, sync_surveys
from .survey_flow import (
    attach_project_entry_ip_claim,
    backfill_attempt_entry_audit,
    build_biobrain_outbound_url,
    build_outbound_url,
    claim_project_entry_ip,
    create_attempt,
    ensure_attempt_prescreener_uid,
    get_request_client_data,
    get_request_ip,
    status_identifiers_from_request,
)
from .tasks import reconcile_rfg_project_log_entries, sync_innovatemr_surveys_task
from .user_hits import aggregate_user_hit_payload, expand_user_hit_rows, user_hit_filter_options
from .user_dashboard import build_user_dashboard_payload, user_dashboard_filter_options


logger = logging.getLogger(__name__)


class UpstreamUnavailable(APIException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "InnovateMR is temporarily unavailable and no cached survey detail exists."
    default_code = "upstream_unavailable"


PROJECT_COLUMN_PERMISSIONS = {
    "project_id": "projects.column.project_id", "survey": "projects.column.survey",
    "market": "projects.column.market", "completes": "projects.column.completes",
    "cpi": "projects.column.cpi", "loi_ir": "projects.column.loi_ir",
    "entry_link": "projects.column.entry_link", "modified": "projects.column.modified",
    "actions": "projects.column.actions",
}

PROJECT_FILTER_PERMISSIONS = {
    "search": "projects.filter.search", "country": "projects.filter.country",
    "status": "projects.filter.status", "client": "projects.filter.client",
    "buyer": "projects.filter.buyer", "survey_type": "projects.filter.survey_type",
    "cpi": "projects.filter.cpi", "date": "projects.filter.date",
    "clear": "projects.filters.clear",
}

STUDY_COLUMN_PERMISSIONS = {
    "project_id": "studies.column.project_id", "survey_id": "studies.column.survey_id",
    "country": "studies.column.country", "cpi": "studies.column.cpi",
    "respondent_id": "studies.column.respondent_id", "pid": "studies.column.pid",
    "user": "studies.column.user",
    "device": "studies.column.device", "ip": "studies.column.ip", "loi": "studies.column.loi",
    "status": "studies.column.status", "start": "studies.column.start", "end": "studies.column.end",
}

STUDY_CLIENT_NAME_PERMISSION = "studies.column.client_name"
STUDY_PROVIDER_STATUS_PERMISSION = "studies.field.provider_status"
STUDY_STATUS_SOURCE_PERMISSION = "studies.field.status_source"

STUDY_FILTER_PERMISSIONS = {
    "search": "studies.filter.search", "branch": "studies.filter.branch",
    "sub_branch": "studies.filter.sub_branch", "shift": "studies.filter.shift", "user": "studies.filter.user",
    "supplier": "studies.filter.supplier",
    "status": "studies.filter.status", "country": "studies.filter.country",
    "client": "studies.filter.client", "buyer": "studies.filter.buyer",
    "project": "studies.filter.project", "date": "studies.filter.date",
    "clear": "studies.filters.clear",
}

DASHBOARD_FILTER_PERMISSIONS = {
    "client": "dashboard.filter.client", "country": "dashboard.filter.country",
    "branch": "dashboard.filter.branch", "sub_branch": "dashboard.filter.sub_branch",
    "shift": "dashboard.filter.shift", "user": "dashboard.filter.user",
    "date": "dashboard.filter.date", "clear": "dashboard.filters.clear",
}

DASHBOARD_CARD_PERMISSIONS = {
    "hits": "dashboard.card.hits", "completes": "dashboard.card.completes",
    "conversion_rate": "dashboard.card.conversion", "active_users": "dashboard.card.active_users",
    "average_loi_seconds": "dashboard.card.average_loi", "revenue": "dashboard.card.revenue",
    "average_cpi": "dashboard.card.average_cpi", "rpc": "dashboard.card.rpc",
    "incidence_rate": "dashboard.card.ir",
}

DASHBOARD_CHART_PERMISSIONS = {
    "performance": "dashboard.chart.performance", "client_share": "dashboard.chart.client_share",
    "status": "dashboard.chart.status", "device": "dashboard.chart.device",
    "top_users": "dashboard.chart.top_users",
}

DASHBOARD_GRAPH_FILTER_PERMISSIONS = {
    "traffic": "dashboard.graph.traffic_filters",
    "finance": "dashboard.graph.finance_filters",
}

STUDY_CARD_PERMISSIONS = {
    "total": "studies.card.total", "initiated": "studies.card.initiated",
    "completed": "studies.card.completed", "terminated": "studies.card.terminated",
    "quota": "studies.card.quota", "security": "studies.card.security",
    "conversion": "studies.card.conversion", "desktop": "studies.card.desktop",
    "mobile": "studies.card.mobile", "tablet": "studies.card.tablet",
    "revenue": "studies.card.revenue",
    "ir": "studies.card.ir",
}

USER_HIT_COLUMN_PERMISSIONS = {
    "branch": "user_hits.column.branch", "sub_branch": "user_hits.column.sub_branch", "shift": "user_hits.column.shift",
    "user": "user_hits.column.user", "date": "user_hits.column.date",
    "hits": "user_hits.column.hits", "completes": "user_hits.column.completes",
}

USER_HIT_FILTER_PERMISSIONS = {
    "search": "user_hits.filter.search", "branch": "user_hits.filter.branch",
    "sub_branch": "user_hits.filter.sub_branch", "shift": "user_hits.filter.shift", "user": "user_hits.filter.user",
    "supplier": "user_hits.filter.supplier",
    "date": "user_hits.filter.date", "clear": "user_hits.filters.clear",
}

USER_HIT_CARD_PERMISSIONS = {
    "total_hits": "user_hits.card.total_hits", "completes": "user_hits.card.completes",
    "conversion": "user_hits.card.conversion", "active_users": "user_hits.card.active_users",
    "devices": "user_hits.card.devices", "ir": "user_hits.card.ir",
}

TERM_REASON_FIELD_PERMISSIONS = {
    "status": "termination_reasons.field.status",
    "reason": "termination_reasons.field.reason",
    "respondent": "termination_reasons.field.respondent",
    "survey": "termination_reasons.field.survey",
    "timing": "termination_reasons.field.timing",
    "audit": "termination_reasons.field.audit",
}

TERM_REASON_COLUMN_PERMISSIONS = {
    "rid": "termination_reasons.column.rid",
    "survey": "termination_reasons.column.survey",
    "client": "termination_reasons.column.client",
    "respondent": "termination_reasons.column.respondent",
    "status": "termination_reasons.column.status",
    "ended": "termination_reasons.column.ended",
    "actions": "termination_reasons.column.actions",
}

TERM_REASON_FILTER_PERMISSIONS = {
    "rid": "termination_reasons.filter.rid",
    "branch": "termination_reasons.filter.branch",
    "sub_branch": "termination_reasons.filter.sub_branch",
    "shift": "termination_reasons.filter.shift",
    "user": "termination_reasons.filter.user",
    "supplier": "termination_reasons.filter.supplier",
    "status": "termination_reasons.filter.status",
    "country": "termination_reasons.filter.country",
    "client": "termination_reasons.filter.client",
    "buyer": "termination_reasons.filter.buyer",
    "date": "termination_reasons.filter.date",
    "clear": "termination_reasons.filters.clear",
}

TERM_REASON_CARD_PERMISSIONS = {
    "total": "termination_reasons.card.total",
    "terminated": "termination_reasons.card.terminated",
    "quota": "termination_reasons.card.quota",
    "quality": "termination_reasons.card.quality",
}

TERM_REASON_TABLE_DETAIL_PERMISSIONS = {
    "provider_status": "termination_reasons.table.provider_status",
    "reason": "termination_reasons.table.reason",
}

TERM_REASON_STATUS_SOURCE_EXPORT_PERMISSION = "termination_reasons.export.status_source"

PRESCREENER_DATA_FILTER_PERMISSIONS = {
    "search": "prescreener_data.filter.search",
    "country": "prescreener_data.filter.country",
    "language": "prescreener_data.filter.language",
    "age_group": "prescreener_data.filter.age_group",
    "gender": "prescreener_data.filter.gender",
    "clear": "prescreener_data.filters.clear",
}

PRESCREENER_DATA_COLUMN_PERMISSIONS = {
    "uid": "prescreener_data.column.uid",
    "market": "prescreener_data.column.market",
    "profile": "prescreener_data.column.profile",
    "captured": "prescreener_data.column.captured",
    "usage_count": "prescreener_data.column.usage_count",
    "answers": "prescreener_data.column.answers",
}

PRESCREENER_DATA_CARD_PERMISSIONS = {
    "records": "prescreener_data.card.records",
    "countries": "prescreener_data.card.countries",
    "age_groups": "prescreener_data.card.age_groups",
    "genders": "prescreener_data.card.genders",
}

UNSUCCESSFUL_STATUS_LABELS = {
    SurveyAttempt.Status.TERMINATED: "Terminated",
    SurveyAttempt.Status.OVER_QUOTA: "Quota full",
    SurveyAttempt.Status.QUALITY_TERMINATED: "Quality / security",
}
UNSUCCESSFUL_ATTEMPT_STATUSES = set(UNSUCCESSFUL_STATUS_LABELS)


def _project_columns_for_user(user):
    codes = effective_permission_codes(user)
    columns = [name for name, code in PROJECT_COLUMN_PERMISSIONS.items() if code in codes]
    if "entry_link" in columns and "survey_links.copy" not in codes:
        columns.remove("entry_link")
    if "actions" in columns and "survey_details.view" not in codes:
        columns.remove("actions")
    if "actions" in columns and "project_id" not in columns:
        # Detail routes currently use the project identifier. Do not leak that
        # route key through an action grant when its display permission is denied.
        columns.remove("actions")
    return columns


def _component_access(codes, permissions):
    return {name: code in codes for name, code in permissions.items()}


def _permitted_columns(codes, permissions):
    return [name for name, code in permissions.items() if code in codes]


def _enforce_query_permissions(request, permission_parameters):
    codes = effective_permission_codes(request.user)
    for code, parameters in permission_parameters.items():
        if any(request.query_params.get(parameter) not in {None, ""} for parameter in parameters):
            if not request.user.is_superuser and code not in codes:
                raise PermissionDenied(f"Your account cannot use the {code} filter.")


@function_permission_required("dashboard.view")
def dashboard_page(request):
    codes = effective_permission_codes(request.user)
    return render(request, "surveys/dashboard.html", {
        "active_page": "dashboard",
        "dashboard_cards": _permitted_columns(codes, DASHBOARD_CARD_PERMISSIONS),
        "dashboard_charts": _permitted_columns(codes, DASHBOARD_CHART_PERMISSIONS),
        "dashboard_graph_filters": _permitted_columns(
            codes, DASHBOARD_GRAPH_FILTER_PERMISSIONS
        ),
    })


@function_permission_required("projects.view")
def projects_page(request):
    codes = effective_permission_codes(request.user)
    visible_surveys = scope_surveys_for_user(Survey.objects.all(), request.user)
    scoped_vendor_id = vendor_scope_user_id(request.user)
    is_client_scoped_panel = bool(
        scoped_vendor_id or organization_client_ids_for_user(request.user) is not None
    )
    project_columns = _project_columns_for_user(request.user)
    project_filters = _component_access(codes, PROJECT_FILTER_PERMISSIONS)
    can_sort_cpi = project_filters["cpi"]
    # Supplier-visible CPI can include client and per-project cuts. Calculating
    # exact slider endpoints used to run those correlated expressions across
    # the whole inventory before the page could render. A supplier cut cannot
    # increase source CPI, so the inexpensive source-CPI maximum is a safe
    # slider ceiling; the API still applies the exact visible-price expression
    # for every CPI filter and sort.
    load_exact_visible_cpi_bounds = can_sort_cpi and not scoped_vendor_id
    cpi_surveys = visible_surveys
    cpi_field = "cpi"
    if load_exact_visible_cpi_bounds:
        cpi_surveys = annotate_survey_pricing_for_user(
            visible_surveys, request.user
        )
        cpi_field = "visible_cpi"
    metadata = project_filter_metadata(
        visible_surveys,
        user_id=request.user.pk,
        client_scoped=is_client_scoped_panel,
        include_cpi=can_sort_cpi,
        cpi_field=cpi_field,
        cpi_queryset=cpi_surveys,
    )
    cpi_min, cpi_max = 0, 100
    if can_sort_cpi:
        # A supplier cut can lower the visible minimum below the raw source
        # minimum, so retain zero as the safe lower control bound for vendors.
        cpi_min = 0 if scoped_vendor_id else (metadata["cpi_min"] or 0)
        cpi_max = metadata["cpi_max"] or 100
        if cpi_max <= cpi_min:
            cpi_max = cpi_min + 1
    return render(request, "surveys/projects.html", {
        "active_page": "projects",
        "countries": metadata["countries"],
        "companies": metadata["companies"],
        "buyer_options": metadata["buyer_options"],
        "survey_types": metadata["survey_types"],
        "company_filter_label": "Client",
        "company_filter_param": "client_name" if is_client_scoped_panel else "company",
        "company_filter_default": "All clients",
        "project_columns": project_columns, "project_column_count": max(1, len(project_columns)),
        "can_view_project_client_name": "projects.column.client_name" in codes,
        "project_filters": project_filters,
        "can_sync": "sync.run" in codes,
        "can_export_projects": "projects.export" in codes,
        "can_change_project_page_size": "projects.control.page_size" in codes,
        "can_paginate_projects": "projects.control.pagination" in codes,
        "can_open_project_studies": "attempts.view" in codes and "studies.filter.project" in codes,
        "can_sort_cpi": can_sort_cpi, "cpi_min_bound": cpi_min, "cpi_max_bound": cpi_max,
    })


@function_permission_required("attempts.view")
def studies_page(request):
    codes = effective_permission_codes(request.user)
    study_columns = _permitted_columns(codes, STUDY_COLUMN_PERMISSIONS)
    if (
        "pid" in study_columns
        and "respondent_id" in study_columns
        and not is_super_admin_account(request.user)
    ):
        study_columns.remove("respondent_id")
    user_ids = activity_visible_user_ids(request.user)
    visible_attempts = SurveyAttempt.objects.all()
    if not request.user.is_superuser:
        visible_attempts = visible_attempts.filter(platform_user_id__in=user_ids)
    visible_surveys = scope_surveys_for_user(Survey.objects.all(), request.user)
    metadata = traffic_filter_metadata(request.user, visible_attempts, visible_surveys)
    return render(request, "surveys/studies.html", {
        "active_page": "studies",
        "tracked_users": metadata["users"],
        "study_branches": metadata["branches"],
        "study_sub_branches": metadata["sub_branches"],
        "study_shifts": metadata["shifts"],
        "study_suppliers": metadata["suppliers"],
        "study_countries": metadata["countries"],
        "study_clients": metadata["clients"],
        "study_buyers": metadata["buyers"],
        "attempt_statuses": [
            ("initiated,redirected", "Initiated"),
            (SurveyAttempt.Status.COMPLETED, "Completed"),
            (SurveyAttempt.Status.TERMINATED, "Terminated"),
            (SurveyAttempt.Status.OVER_QUOTA, "Over quota"),
            (SurveyAttempt.Status.QUALITY_TERMINATED, "Quality terminated"),
        ],
        "study_filters": _component_access(codes, STUDY_FILTER_PERMISSIONS),
        "study_columns": study_columns,
        "study_column_count": max(1, len(study_columns)),
        "can_view_study_client_name": STUDY_CLIENT_NAME_PERMISSION in codes,
        "can_view_study_provider_status": STUDY_PROVIDER_STATUS_PERMISSION in codes,
        "study_cards": _permitted_columns(codes, STUDY_CARD_PERMISSIONS),
        "can_export": "attempts.export" in codes,
        "can_import_final_ids": "attempts.final_ids.import" in codes,
        "final_id_clients": list(
            Client.objects.filter(is_active=True).only("id", "name").order_by("name", "id")
        ) if "attempts.final_ids.import" in codes else [],
        "final_id_months": [
            (1, "January"), (2, "February"), (3, "March"), (4, "April"),
            (5, "May"), (6, "June"), (7, "July"), (8, "August"),
            (9, "September"), (10, "October"), (11, "November"), (12, "December"),
        ],
        "final_id_years": range(timezone.localdate().year - 3, timezone.localdate().year + 3),
        "final_id_current_month": timezone.localdate().month,
        "final_id_current_year": timezone.localdate().year,
        "can_change_study_page_size": "studies.control.page_size" in codes,
        "can_paginate_studies": "studies.control.pagination" in codes,
    })


@require_POST
@function_permission_required("attempts.final_ids.import")
def final_ids_import(request):
    """Apply one accepted/rejected client final-ID file by immutable RID."""

    client_value = str(request.POST.get("client") or "").strip()
    year_value = str(request.POST.get("year") or "").strip()
    month_value = str(request.POST.get("month") or "").strip()
    decision = str(request.POST.get("status") or "").strip().lower()
    uploaded_file = request.FILES.get("file")
    try:
        client_id = int(client_value)
        year = int(year_value)
        month = int(month_value)
        if not 2000 <= year <= 2100:
            raise ValueError
        accounting_month = date(year, month, 1)
    except (TypeError, ValueError):
        return JsonResponse({"detail": "Choose a valid client, month and year."}, status=400)
    client = Client.objects.filter(pk=client_id, is_active=True).first()
    if client is None:
        return JsonResponse({"detail": "Choose an active client."}, status=400)
    if uploaded_file is None:
        return JsonResponse({"detail": "Choose a CSV or Excel file."}, status=400)
    try:
        result = import_final_ids(
            uploaded_file=uploaded_file,
            client=client,
            accounting_month=accounting_month,
            decision=decision,
            uploaded_by=request.user,
        )
    except FinalIDImportError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    return JsonResponse({
        "message": (
            f"{result['applied']:,} RID(s) marked {decision}. "
            f"{result['not_found']:,} not found, "
            f"{result['client_mismatch']:,} client mismatch, "
            f"{result['not_completed']:,} not completed."
        ),
        "result": result,
    })


@function_permission_required("user_hits.view")
def user_hits_page(request):
    codes = effective_permission_codes(request.user)
    hit_columns = _permitted_columns(codes, USER_HIT_COLUMN_PERMISSIONS)
    filter_options = cached_user_metadata(
        "user-hit-filters",
        request.user,
        lambda: user_hit_filter_options(request.user),
    )
    return render(request, "surveys/user_hits.html", {
        "active_page": "user-hits",
        "hit_filters": _component_access(codes, USER_HIT_FILTER_PERMISSIONS),
        "hit_columns": hit_columns,
        "hit_column_count": max(1, len(hit_columns)),
        "hit_cards": _permitted_columns(codes, USER_HIT_CARD_PERMISSIONS),
        "can_change_hit_page_size": "user_hits.control.page_size" in codes,
        "can_paginate_hits": "user_hits.control.pagination" in codes,
        **filter_options,
    })


@function_permission_required("user_dashboard.view")
def user_dashboard_page(request):
    local_today = timezone.localdate()
    filter_options = cached_user_metadata(
        "user-dashboard-filters-v1",
        request.user,
        lambda: user_dashboard_filter_options(request.user),
    )
    return render(request, "surveys/user_dashboard.html", {
        "active_page": "user-dashboard",
        "selected_month": local_today.month,
        "selected_year": local_today.year,
        "month_options": [
            {"value": month, "label": date(2000, month, 1).strftime("%B")}
            for month in range(1, 13)
        ],
        "year_options": range(local_today.year, max(2019, local_today.year - 5), -1),
        **filter_options,
    })


@function_permission_required("prescreener_data.view")
def prescreener_data_page(request):
    """Read-only, permission-scoped Panelist Data browser for the isolated vault."""

    codes = effective_permission_codes(request.user)
    filters_access = _component_access(codes, PRESCREENER_DATA_FILTER_PERMISSIONS)
    columns = _permitted_columns(codes, PRESCREENER_DATA_COLUMN_PERMISSIONS)
    selected = {
        "search": request.GET.get("search", "").strip(),
        "country": request.GET.get("country", "").strip(),
        "language": request.GET.get("language", "").strip(),
        "age_group": request.GET.get("age_group", "").strip(),
        "gender": request.GET.get("gender", "").strip(),
    }
    for name, value in selected.items():
        if value and not filters_access[name]:
            raise PermissionDenied(f"Your account cannot use the {name.replace('_', ' ')} filter.")

    page_obj = None
    summary = {"total": 0, "countries": 0, "age_groups": 0, "genders": 0}
    options = {"countries": [], "languages": [], "age_groups": [], "genders": []}
    vault_error = ""
    if not getattr(settings, "PRESCREENER_VAULT_ENABLED", False):
        vault_error = "The pre-screener vault is not enabled on this environment."
    else:
        try:
            vault_fields = {"uid", "submitted_at"}
            if "market" in columns:
                vault_fields.update({
                    "country", "country_code", "language", "language_code",
                })
            if "profile" in columns:
                vault_fields.update({
                    "respondent_age", "respondent_age_group", "respondent_gender",
                    "respondent_ethnicity", "respondent_postal_code",
                })
            if "usage_count" in columns:
                vault_fields.add("usage_count")
            base = (
                PrescreenerSubmission.objects.using("prescreener_vault")
                .only(*sorted(vault_fields))
            )
            options = vault_filter_options()
            if "answers" in columns:
                base = base.prefetch_related("question_answers")
            queryset = apply_submission_filters(base, selected)
            summary = vault_filtered_summary(selected)
            paginator = Paginator(queryset.order_by("-submitted_at"), 20)
            # The cached summary already contains the exact filtered total, so
            # avoid a second COUNT against the isolated vault database.
            paginator.__dict__["count"] = int(summary["total"])
            page_obj = paginator.get_page(request.GET.get("page", 1))
        except (DatabaseError, PrescreenerVaultError) as exc:
            logger.exception("Unable to read the pre-screener vault")
            vault_error = f"Vault data is temporarily unavailable: {exc}"

    query_without_page = request.GET.copy()
    query_without_page.pop("page", None)
    return render(request, "surveys/prescreened_data.html", {
        "active_page": "prescreened-data",
        "vault_error": vault_error,
        "page_obj": page_obj,
        "summary": summary,
        "options": options,
        "selected": selected,
        "vault_filters": filters_access,
        "vault_columns": columns,
        "vault_column_count": max(1, len(columns)),
        "vault_cards": _permitted_columns(codes, PRESCREENER_DATA_CARD_PERMISSIONS),
        "can_export_vault": "prescreener_data.export" in codes,
        "can_paginate_vault": "prescreener_data.control.pagination" in codes,
        "page_query": query_without_page.urlencode(),
    })


@function_permission_required("prescreener_data.export")
def prescreener_data_export(request):
    """Export only Panelist Data columns granted to the requesting account."""

    if not getattr(settings, "PRESCREENER_VAULT_ENABLED", False):
        return HttpResponse("The pre-screener vault is not enabled.", status=503)

    codes = effective_permission_codes(request.user)
    filters_access = _component_access(codes, PRESCREENER_DATA_FILTER_PERMISSIONS)
    permitted = set(_permitted_columns(codes, PRESCREENER_DATA_COLUMN_PERMISSIONS))
    selected = {
        "search": request.GET.get("search", "").strip(),
        "country": request.GET.get("country", "").strip(),
        "language": request.GET.get("language", "").strip(),
        "age_group": request.GET.get("age_group", "").strip(),
        "gender": request.GET.get("gender", "").strip(),
    }
    for name, value in selected.items():
        if value and not filters_access[name]:
            raise PermissionDenied(f"Your account cannot use the {name.replace('_', ' ')} filter.")

    submission_specs = {
        "uid": (["UID"], [22]),
        "market": (["Country", "Country code", "Language", "Language code"], [20, 13, 17, 14]),
        "profile": (
            ["Age", "Age group", "Gender", "Ethnicity", "ZIP / postal code"],
            [9, 13, 14, 24, 18],
        ),
        "captured": (["Registered at (IST)"], [22]),
        "usage_count": (["Visits"], [13]),
    }
    submission_columns = [
        name for name in PRESCREENER_DATA_COLUMN_PERMISSIONS
        if name in permitted and name in submission_specs
    ]
    base_queryset = apply_submission_filters(
        PrescreenerSubmission.objects.using("prescreener_vault").all(), selected
    ).order_by("-submitted_at")
    submission_fields = {"uid", "submitted_at"}
    if "market" in submission_columns:
        submission_fields.update({
            "country", "country_code", "language", "language_code",
        })
    if "profile" in submission_columns:
        submission_fields.update({
            "respondent_age", "respondent_age_group", "respondent_gender",
            "respondent_ethnicity", "respondent_postal_code",
        })
    if "usage_count" in submission_columns:
        submission_fields.add("usage_count")
    submission_queryset = base_queryset.only(*sorted(submission_fields))
    answer_queryset = base_queryset.only("uid").prefetch_related("question_answers")

    def submission_rows():
        for submission in submission_queryset.iterator(chunk_size=500):
            values_by_column = {}
            if "uid" in submission_columns:
                values_by_column["uid"] = [submission.uid]
            if "market" in submission_columns:
                values_by_column["market"] = [
                    submission.country, submission.country_code,
                    submission.language, submission.language_code,
                ]
            if "profile" in submission_columns:
                values_by_column["profile"] = [
                    submission.respondent_age, submission.respondent_age_group,
                    submission.respondent_gender, submission.respondent_ethnicity,
                    submission.respondent_postal_code,
                ]
            if "captured" in submission_columns:
                values_by_column["captured"] = [
                    _excel_datetime(submission.submitted_at)
                ]
            if "usage_count" in submission_columns:
                values_by_column["usage_count"] = [submission.usage_count]
            yield [value for name in submission_columns for value in values_by_column[name]]

    def answer_rows():
        for submission in answer_queryset.iterator(chunk_size=250):
            for answer in submission.question_answers.all():
                yield ([submission.uid] if "uid" in permitted else []) + [
                    answer.position, answer.question_id,
                    answer.question_key, answer.question_text, answer.question_type,
                    answer.question_category, answer.canonical_attribute,
                    ", ".join(str(value) for value in answer.answer_values),
                    ", ".join(str(value) for value in answer.answer_labels),
                    ", ".join(str(value) for value in answer.upstream_values),
                ]

    sheets = []
    if submission_columns:
        sheets.append(
            ExcelSheet(
                "Submissions",
                [header for name in submission_columns for header in submission_specs[name][0]],
                submission_rows(),
                [width for name in submission_columns for width in submission_specs[name][1]],
            )
        )
    if "answers" in permitted:
        sheets.append(
            ExcelSheet(
                "Answers",
                (["UID"] if "uid" in permitted else []) + [
                    "Position", "Question ID", "Question key", "Question", "Question type",
                    "Category", "Reusable attribute", "Answer values", "Answer labels", "Upstream values",
                ],
                answer_rows(),
                ([22] if "uid" in permitted else []) + [10, 16, 22, 48, 18, 18, 20, 28, 34, 25],
            )
        )
    if not sheets:
        raise PermissionDenied("No Panelist Data columns are assigned to your account.")

    local_now = timezone.localtime()
    return build_excel_response(
        f"panelist-data-{local_now:%Y%m%d-%H%M%S}-IST.xlsx",
        sheets,
    )


def _refresh_provider_outcome(attempt, integration):
    """Fetch one provider transaction without coupling custom clients to Innovate status rules."""

    provider_code = (integration.provider_code if integration else "innovatemr").lower()
    client = InnovateMRClient(integration=integration)
    if provider_code == "innovatemr":
        reconcile_attempt_status(client, attempt)
        attempt.refresh_from_db()
        return

    survey_identifier = attempt.survey.source_id or attempt.survey.source_key
    transactions = client.get_survey_transactions_by_pid(survey_identifier, attempt.rid)
    if not transactions:
        attempt.upstream_checked_at = timezone.now()
        attempt.save(update_fields=["upstream_checked_at", "updated_at"])
        return

    respondent_keys = ("PID", "pid", "trackId", "rid", "RID", "respondentId")
    transaction_row = next(
        (
            row for row in transactions
            if any(str(row.get(key) or "") == attempt.rid for key in respondent_keys)
        ),
        transactions[0],
    )
    attempt.upstream_transaction_data = transaction_row
    attempt.upstream_checked_at = timezone.now()
    attempt.save(update_fields=["upstream_transaction_data", "upstream_checked_at", "updated_at"])


def _term_report_values(request, name):
    """Return stable, de-duplicated values from repeated or CSV query params."""

    values = []
    for raw_value in request.GET.getlist(name):
        for value in str(raw_value or "").split(","):
            value = value.strip()
            if value and value not in values:
                values.append(value)
    return values


def _term_report_filter_state(request, filters_access):
    selected = {
        "search": request.GET.get("search", "").strip(),
        "branch": _term_report_values(request, "branch"),
        "sub_branch": _term_report_values(request, "sub_branch"),
        "shift": _term_report_values(request, "shift"),
        "user": _term_report_values(request, "user"),
        "supplier": _term_report_values(request, "supplier"),
        "status": _term_report_values(request, "status"),
        "country": _term_report_values(request, "country"),
        "client": _term_report_values(request, "client"),
        "buyer_id": _term_report_values(request, "buyer_id"),
        "date_field": request.GET.get("date_field", "callback").strip() or "callback",
        "date_from": request.GET.get("date_from", "").strip(),
        "date_to": request.GET.get("date_to", "").strip(),
    }
    supplied_by_permission = {
        "rid": selected["search"],
        "branch": selected["branch"],
        "sub_branch": selected["sub_branch"],
        "shift": selected["shift"],
        "user": selected["user"],
        "supplier": selected["supplier"],
        "status": selected["status"],
        "country": selected["country"],
        "client": selected["client"],
        "buyer": selected["buyer_id"],
        "date": selected["date_from"] or selected["date_to"],
    }
    for filter_name, value in supplied_by_permission.items():
        if value and not filters_access.get(filter_name, False):
            raise PermissionDenied(
                f"Your account cannot use the {filter_name.replace('_', ' ')} filter."
            )
    if selected["date_field"] not in {"initiated", "callback"}:
        selected["date_field"] = "callback"
    return selected


def _scope_attempt_queryset_to_user(queryset, user):
    """Apply the same hierarchy boundary to every attempt-backed report.

    The FK is authoritative for current journeys. Numeric legacy ``user_id``
    snapshots are included only when their FK is empty so older records remain
    visible to the correct user without exposing them outside that hierarchy.
    """

    if user.is_superuser:
        return queryset
    visible_user_ids = activity_visible_user_ids(user)
    return queryset.filter(
        Q(platform_user_id__in=visible_user_ids)
        | Q(
            platform_user_id__isnull=True,
            user_id__in=[str(user_id) for user_id in visible_user_ids],
        )
    )


def _term_report_base_queryset(user):
    queryset = SurveyAttempt.objects.select_related(
        "survey__integration__client",
        "survey__client",
        "platform_user__employee_profile__organization_unit__parent__parent",
    ).filter(status__in=UNSUCCESSFUL_ATTEMPT_STATUSES)
    return _scope_attempt_queryset_to_user(queryset, user)


def _project_term_report_queryset(
    queryset,
    *,
    columns,
    include_provider_outcome=False,
    include_status_source=False,
):
    """Select only fields consumed by the Term Report table/export."""

    columns = set(columns)
    fields = {
        "id", "survey_id", "platform_user_id", "status",
        "initiated_at", "callback_at",
        "survey__id",
    }
    relations = {"survey"}

    if "rid" in columns:
        fields.update({"rid", "pid", "prescreener_uid"})
    elif "actions" in columns or include_provider_outcome:
        fields.add("rid")
    if "survey" in columns:
        fields.update({"survey__local_id", "survey__source_key"})
    if "client" in columns:
        relations.update({
            "survey__client", "survey__integration", "survey__integration__client",
        })
        fields.update({
            "survey__client_id", "survey__client__id", "survey__client__name",
            "survey__integration_id", "survey__integration__id",
            "survey__integration__provider_code", "survey__integration__client_id",
            "survey__integration__client__id", "survey__integration__client__name",
            "survey__company_name",
        })
    if "respondent" in columns:
        relations.add("platform_user")
        fields.update({
            "platform_user__id", "platform_user__username",
            "platform_user__first_name", "platform_user__last_name",
            "platform_user__email", "initiation_ip", "callback_ip",
        })
    if "ended" in columns:
        fields.update({"last_callback_at", "loi_seconds"})
    if include_provider_outcome:
        relations.add("survey__integration")
        fields.update({
            "upstream_transaction_data", "exit_client_data", "is_verified",
            "survey__integration_id", "survey__integration__id",
            "survey__integration__provider_code", "survey__integration__config",
            "survey__integration__field_mapping",
        })
    if include_status_source:
        relations.add("survey__integration")
        fields.update({
            "status_source", "survey__integration_id", "survey__integration__id",
            "survey__integration__provider_code",
        })

    return queryset.select_related(None).select_related(*sorted(relations)).only(*sorted(fields))


def _term_report_datetime(value, label):
    if not value:
        return None
    parsed = parse_datetime(value)
    if parsed is None:
        raise PermissionDenied(f"{label} must use a valid date and time.")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _filtered_term_report_queryset(request, filters_access):
    selected = _term_report_filter_state(request, filters_access)
    queryset = _term_report_base_queryset(request.user)
    search = selected["search"]
    if search:
        queryset = queryset.filter(
            Q(rid__icontains=search)
            | Q(pid__icontains=search)
            | Q(prescreener_uid__icontains=search)
            | Q(provider_profile_uid__icontains=search)
            | Q(survey__local_id__icontains=search)
            | Q(survey__source_key__icontains=search)
            | Q(survey__buyer_id__icontains=search)
            | Q(survey__client__name__icontains=search)
            | Q(platform_user__username__icontains=search)
            | Q(platform_user__first_name__icontains=search)
            | Q(platform_user__last_name__icontains=search)
            | Q(platform_user__email__icontains=search)
            | Q(initiation_ip__icontains=search)
            | Q(callback_ip__icontains=search)
        )

    filter_data = {
        name: ",".join(selected[name])
        for name in ("branch", "sub_branch", "shift", "user", "supplier", "status", "country", "client", "buyer_id")
        if selected[name]
    }
    if filter_data:
        queryset = SurveyAttemptFilter(filter_data, queryset=queryset).qs
    lower = _term_report_datetime(selected["date_from"], "From date and time")
    upper = _term_report_datetime(selected["date_to"], "To date and time")
    if lower and upper and lower > upper:
        raise PermissionDenied("From date and time cannot be after To date and time.")
    date_column = "initiated_at" if selected["date_field"] == "initiated" else "callback_at"
    if lower:
        queryset = queryset.filter(**{f"{date_column}__gte": lower})
    if upper:
        queryset = queryset.filter(**{f"{date_column}__lte": upper})
    return queryset, selected


def _term_report_options(base_queryset, user):
    hierarchy = user_hit_filter_options(user)
    return {
        **hierarchy,
        "countries": list(
            base_queryset.exclude(survey__country_code="")
            .values("survey__country_code", "survey__country")
            .distinct().order_by("survey__country_code")
        ),
        "clients": list(
            base_queryset.filter(survey__client__isnull=False)
            .values("survey__client_id", "survey__client__name")
            .distinct().order_by("survey__client__name")
        ),
        "buyers": list(
            base_queryset.exclude(survey__buyer_id="")
            .values("survey__client_id", "survey__buyer_id")
            .distinct().order_by("survey__buyer_id")
        ),
    }


@function_permission_required("termination_reasons.view")
def termination_reasons_page(request):
    codes = effective_permission_codes(request.user)
    filters_access = _component_access(codes, TERM_REASON_FILTER_PERMISSIONS)
    columns = _permitted_columns(codes, TERM_REASON_COLUMN_PERMISSIONS)
    table_details = _component_access(codes, TERM_REASON_TABLE_DETAIL_PERMISSIONS)
    queryset, selected = _filtered_term_report_queryset(request, filters_access)
    detail_rid = (request.GET.get("detail") or request.GET.get("rid") or "").strip()
    detail_attempt = None
    detail_outcome = None
    lookup_error = ""

    if detail_rid and "termination_reasons.action.details" not in codes:
        raise PermissionDenied("Your account cannot open outcome details.")

    base_queryset = _term_report_base_queryset(request.user)
    filter_options = term_filter_metadata(request.user, base_queryset)

    summary = cached_report_payload(
        "term-summary-v2",
        request,
        lambda: queryset.aggregate(
            total=Count("id"),
            terminated=Count("id", filter=Q(status=SurveyAttempt.Status.TERMINATED)),
            quota=Count("id", filter=Q(status=SurveyAttempt.Status.OVER_QUOTA)),
            quality=Count("id", filter=Q(status=SurveyAttempt.Status.QUALITY_TERMINATED)),
        ),
        neutral_parameters=("page", "detail", "rid", "format", "ordering"),
    )
    include_table_outcome = bool(
        table_details["provider_status"] or table_details["reason"]
    )
    queryset = _project_term_report_queryset(
        queryset,
        columns=columns,
        include_provider_outcome=include_table_outcome,
        include_status_source=True,
    )
    paginator = Paginator(queryset.order_by("-callback_at", "-initiated_at"), 20)
    page_obj = paginator.get_page(request.GET.get("page", 1))
    for row in page_obj.object_list:
        row.reason_outcome = provider_outcome(row) if include_table_outcome else {}
        row.reason_status_label = UNSUCCESSFUL_STATUS_LABELS.get(row.status, row.get_status_display())
        row.termination_origin = termination_origin(row)

    if detail_rid:
        if len(detail_rid) != 10 or not detail_rid.isalnum():
            lookup_error = "The requested RID must contain exactly 10 letters and numbers."
        else:
            detail_attempt = base_queryset.filter(rid=detail_rid).first()
            if detail_attempt is None:
                non_terminal_attempt = _scope_attempt_queryset_to_user(
                    SurveyAttempt.objects.select_related(
                    "survey__integration__client", "survey__client", "platform_user"
                    ),
                    request.user,
                ).filter(rid=detail_rid).first()
                if non_terminal_attempt:
                    lookup_error = (
                        f"This RID is currently {non_terminal_attempt.get_status_display().lower()}; "
                        "provider outcome details become available after a final unsuccessful status."
                    )
        if not lookup_error and detail_attempt is None:
            lookup_error = "No survey attempt was found for this RID."
        elif detail_attempt:
            detail_attempt.reason_status_label = UNSUCCESSFUL_STATUS_LABELS.get(
                detail_attempt.status, detail_attempt.get_status_display()
            )
            detail_attempt.termination_origin = termination_origin(detail_attempt)
            detail_outcome = provider_outcome(detail_attempt)
            integration = detail_attempt.survey.integration if detail_attempt.survey.integration_id else None
            provider_code = (integration.provider_code if integration else "innovatemr").lower()
            supports_lookup = provider_code == "innovatemr" or bool(
                integration and integration.transaction_endpoint_template
            )
            if (
                supports_lookup
                and "termination_reasons.action.refresh" in codes
                and (not detail_outcome["status"] or not detail_outcome["reason"])
            ):
                try:
                    _refresh_provider_outcome(detail_attempt, integration)
                    detail_outcome = provider_outcome(detail_attempt)
                except (InnovateMRAPIError, ValueError) as exc:
                    provider_label = integration.client.name if integration else "InnovateMR"
                    lookup_error = (
                        f"The attempt was found, but {provider_label} could not return its detailed "
                        f"transaction yet: {exc}"
                    )

    link_params = request.GET.copy()
    for parameter in ("detail", "rid"):
        link_params.pop(parameter, None)
    detail_query = link_params.urlencode()
    page_params = link_params.copy()
    page_params.pop("page", None)
    page_query = page_params.urlencode()

    return render(request, "surveys/termination_reasons.html", {
        "active_page": "termination-reasons",
        "selected": selected,
        "search_query": selected["search"],
        "client_options": filter_options["clients"],
        "term_reason_clients": filter_options["clients"],
        "term_branches": filter_options["branches"],
        "term_sub_branches": filter_options["sub_branches"],
        "term_shifts": filter_options["shifts"],
        "term_users": filter_options["users"],
        "term_suppliers": filter_options["suppliers"],
        "term_countries": filter_options["countries"],
        "term_buyers": filter_options["buyers"],
        "attempt_statuses": list(UNSUCCESSFUL_STATUS_LABELS.items()),
        "summary": summary,
        "page_obj": page_obj,
        "reason_columns": columns,
        "reason_column_count": max(
            1,
            len(columns) + (1 if "status" not in columns and any(table_details.values()) else 0),
        ),
        "reason_table_details": table_details,
        "show_reason_status_cell": "status" in columns or any(table_details.values()),
        "reason_filters": filters_access,
        "reason_cards": _permitted_columns(codes, TERM_REASON_CARD_PERMISSIONS),
        "can_paginate_reasons": "termination_reasons.control.pagination" in codes,
        "can_view_reason_details": "termination_reasons.action.details" in codes,
        "detail_attempt": detail_attempt,
        "detail_outcome": detail_outcome,
        "detail_query": detail_query,
        "page_query": page_query,
        "lookup_error": lookup_error,
        "can_refresh_reasons": "termination_reasons.action.refresh" in codes,
        "can_export_reasons": "termination_reasons.export" in codes,
        "reason_fields": _component_access(codes, TERM_REASON_FIELD_PERMISSIONS),
    })


@function_permission_required("termination_reasons.export")
def termination_reasons_export(request):
    """Export filtered Term Reports using the viewer's table-column grants."""

    codes = effective_permission_codes(request.user)
    filters_access = _component_access(codes, TERM_REASON_FILTER_PERMISSIONS)
    queryset, _selected = _filtered_term_report_queryset(request, filters_access)
    queryset = queryset.order_by("-callback_at", "-initiated_at")

    permitted = set(_permitted_columns(codes, TERM_REASON_COLUMN_PERMISSIONS))
    specs = {
        "rid": (["RID", "PID", "UID"], [15, 15, 21]),
        "survey": (["Project ID", "Client survey ID"], [19, 20]),
        "client": (["Client", "Provider"], [22, 18]),
        "respondent": (["Respondent", "Email", "Entry IP", "Exit IP"], [22, 30, 17, 17]),
        "status": (["Platform status"], [20]),
        "ended": (["Started at", "Ended at", "LOI (minutes)"], [24, 24, 15]),
    }
    ordered_columns = [name for name in TERM_REASON_COLUMN_PERMISSIONS if name in permitted and name in specs]
    table_details = _component_access(codes, TERM_REASON_TABLE_DETAIL_PERMISSIONS)
    extra_status_fields = []
    if table_details["provider_status"]:
        extra_status_fields.append(("provider_status", "Provider status", 27))
    if table_details["reason"]:
        extra_status_fields.extend([
            ("reason", "Term reason", 44),
            ("category", "Term category", 22),
        ])
    if TERM_REASON_STATUS_SOURCE_EXPORT_PERMISSION in codes:
        extra_status_fields.append(("status_source", "Termination location", 24))
    export_fields = []
    for name in ordered_columns:
        export_fields.append(("column", name))
        if name == "status":
            export_fields.extend(("status_detail", key) for key, _header, _width in extra_status_fields)
    if "status" not in ordered_columns:
        export_fields.extend(("status_detail", key) for key, _header, _width in extra_status_fields)
    if not export_fields:
        raise PermissionDenied("No Term Report export fields are assigned to your account.")
    extra_specs = {key: (header, width) for key, header, width in extra_status_fields}
    headers = []
    widths = []
    for field_type, name in export_fields:
        if field_type == "column":
            headers.extend(specs[name][0])
            widths.extend(specs[name][1])
        else:
            header, width = extra_specs[name]
            headers.append(header)
            widths.append(width)

    include_provider_outcome = bool(
        table_details["provider_status"] or table_details["reason"]
    )
    queryset = _project_term_report_queryset(
        queryset,
        columns=ordered_columns,
        include_provider_outcome=include_provider_outcome,
        include_status_source=TERM_REASON_STATUS_SOURCE_EXPORT_PERMISSION in codes,
    )

    def rows():
        for attempt in queryset.iterator(chunk_size=500):
            outcome = provider_outcome(attempt) if include_provider_outcome else {}
            values_by_column = {}
            if "rid" in ordered_columns:
                values_by_column["rid"] = [
                    attempt.rid, attempt.pid, attempt.prescreener_uid or "",
                ]
            if "survey" in ordered_columns:
                values_by_column["survey"] = [attempt.survey.local_id, attempt.survey.source_key]
            if "client" in ordered_columns:
                survey = attempt.survey
                client = survey.client or (
                    survey.integration.client if survey.integration_id else None
                )
                provider = survey.integration.provider_code if survey.integration_id else "innovatemr"
                values_by_column["client"] = [
                    client.name if client else survey.company_name,
                    provider,
                ]
            if "respondent" in ordered_columns:
                respondent = ""
                email = ""
                if attempt.platform_user_id:
                    respondent = (
                        attempt.platform_user.get_full_name()
                        or attempt.platform_user.username
                    )
                    email = attempt.platform_user.email
                values_by_column["respondent"] = [
                    respondent, email, attempt.initiation_ip or "", attempt.callback_ip or "",
                ]
            if "status" in ordered_columns:
                values_by_column["status"] = [
                    UNSUCCESSFUL_STATUS_LABELS.get(
                        attempt.status, attempt.get_status_display()
                    )
                ]
            if "ended" in ordered_columns:
                ended_at = (
                    attempt.callback_at or attempt.last_callback_at or attempt.initiated_at
                )
                values_by_column["ended"] = [
                    _excel_datetime(attempt.initiated_at),
                    _excel_datetime(ended_at),
                    round(attempt.loi_seconds / 60, 2)
                    if attempt.loi_seconds is not None else "",
                ]
            status_detail_values = {}
            if table_details["provider_status"]:
                status_detail_values["provider_status"] = (
                    outcome.get("status") or "Not supplied"
                )
            if table_details["reason"]:
                status_detail_values.update({
                    "reason": outcome.get("reason") or "",
                    "category": outcome.get("category") or "",
                })
            if TERM_REASON_STATUS_SOURCE_EXPORT_PERMISSION in codes:
                status_detail_values["status_source"] = termination_origin(attempt)["label"]
            values = []
            for field_type, name in export_fields:
                if field_type == "column":
                    values.extend(values_by_column[name])
                else:
                    values.append(status_detail_values[name])
            yield values

    local_now = timezone.localtime()
    return build_excel_response(
        f"term-reports-{local_now:%Y%m%d-%H%M%S}-IST.xlsx",
        [ExcelSheet("Term Reports", headers, rows(), widths)],
    )


EXPORT_JOB_PERMISSION = {
    ExportJob.Kind.PROJECTS: "projects.export",
    ExportJob.Kind.TRAFFIC: "attempts.export",
    ExportJob.Kind.TERMS: "termination_reasons.export",
    ExportJob.Kind.PANELIST: "prescreener_data.export",
}


def _export_job_for_request(request, public_id):
    try:
        job = ExportJob.objects.get(public_id=public_id)
    except (ExportJob.DoesNotExist, ValueError):
        raise Http404("Export not found.")
    # A completed export can contain PII. Do not expose job existence or allow
    # a different privileged account to retrieve somebody else's workbook.
    if job.requested_by_id != request.user.id:
        raise Http404("Export not found.")
    return job


@require_POST
def export_job_create(request, kind):
    """Queue an export so browser navigation cannot cancel the workbook build."""

    if not request.user.is_authenticated:
        return JsonResponse({"detail": "Authentication is required."}, status=401)
    if kind not in EXPORT_JOB_PERMISSION:
        return JsonResponse({"detail": "Unsupported export type."}, status=404)
    if not has_function_access(request.user, EXPORT_JOB_PERMISSION[kind]):
        raise PermissionDenied("Your account cannot export this report.")

    # Preserve repeated filter values exactly, discard pagination (exports are
    # always the full filtered result), and cap the payload to a normal URL.
    query = {
        key: values[:100]
        for key, values in request.GET.lists()
        if key not in {"page", "page_size"} and len(key) <= 80
    }
    if sum(len(value) for values in query.values() for value in values) > 16_000:
        return JsonResponse({"detail": "Export filters are too large."}, status=400)
    now = timezone.now()
    # Reusing a pending or ready workbook prevents accidental double-clicks
    # (and direct duplicate API calls) from creating costly duplicate exports.
    active_statuses = [
        ExportJob.Status.QUEUED,
        ExportJob.Status.RUNNING,
        ExportJob.Status.COMPLETED,
    ]
    candidates = ExportJob.objects.filter(
        requested_by=request.user,
        kind=kind,
        status__in=active_statuses,
        downloaded_at__isnull=True,
        expires_at__gt=now,
    ).order_by("-created_at")
    existing = next((item for item in candidates if item.query == query), None)
    if existing:
        return JsonResponse({
            "id": str(existing.public_id), "status": existing.status,
            "status_url": reverse("export-job-status", kwargs={"public_id": existing.public_id}),
            "download_url": reverse("export-job-download", kwargs={"public_id": existing.public_id}),
            "reused": True,
        })
    job = ExportJob.objects.create(
        requested_by=request.user,
        kind=kind,
        query=query,
        expires_at=now + timedelta(hours=settings.EXPORT_JOB_RETENTION_HOURS),
    )
    try:
        from .tasks import build_export_job
        transaction.on_commit(lambda: build_export_job.delay(str(job.public_id)))
    except Exception:
        logger.exception("Could not queue export job %s", job.public_id)
        job.status = ExportJob.Status.FAILED
        job.error = "The export queue is temporarily unavailable. Please retry."
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "error", "finished_at"])
        return JsonResponse({"detail": job.error}, status=503)
    return JsonResponse({
        "id": str(job.public_id), "status": job.status,
        "status_url": reverse("export-job-status", kwargs={"public_id": job.public_id}),
        "download_url": reverse("export-job-download", kwargs={"public_id": job.public_id}),
        "reused": False,
    }, status=202)


@require_GET
def export_job_status(request, public_id):
    if not request.user.is_authenticated:
        return JsonResponse({"detail": "Authentication is required."}, status=401)
    job = _export_job_for_request(request, public_id)
    payload = {
        "id": str(job.public_id), "kind": job.kind, "status": job.status,
        "filename": job.filename, "error": job.error,
        "expires_at": job.expires_at.isoformat(), "downloaded": bool(job.downloaded_at),
    }
    if job.status == ExportJob.Status.COMPLETED and job.storage_key:
        payload["download_url"] = reverse("export-job-download", kwargs={"public_id": job.public_id})
    return JsonResponse(payload)


@require_GET
def export_job_download(request, public_id):
    if not request.user.is_authenticated:
        return JsonResponse({"detail": "Authentication is required."}, status=401)
    job = _export_job_for_request(request, public_id)
    if job.status != ExportJob.Status.COMPLETED or not job.storage_key:
        return JsonResponse({"detail": "This export is not ready yet."}, status=409)
    if job.expires_at <= timezone.now():
        return JsonResponse({"detail": "This export has expired. Please create a new one."}, status=410)
    path = Path(settings.EXPORT_JOB_DIR) / job.storage_key
    if not path.is_file():
        return JsonResponse({"detail": "The export file is no longer available. Please create a new one."}, status=410)
    # A completed export is reusable until its first download begins.  Mark it
    # before streaming so a double click cannot start another export job.
    ExportJob.objects.filter(pk=job.pk, downloaded_at__isnull=True).update(downloaded_at=timezone.now())
    response = FileResponse(path.open("rb"), as_attachment=True, filename=job.filename)
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


def workspace_home(request):
    if not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login
        return redirect_to_login(request.get_full_path())
    if has_function_access(request.user, "projects.view"):
        return HttpResponseRedirect(reverse("projects"))
    if has_function_access(request.user, "dashboard.view"):
        return HttpResponseRedirect(reverse("dashboard"))
    if has_function_access(request.user, "attempts.view"):
        return HttpResponseRedirect(reverse("traffic-reports"))
    if has_function_access(request.user, "termination_reasons.view"):
        return HttpResponseRedirect(reverse("termination-reasons"))
    if has_function_access(request.user, "user_hits.view"):
        return HttpResponseRedirect(reverse("user-hits"))
    if has_function_access(request.user, "prescreener_data.view"):
        return HttpResponseRedirect(reverse("prescreened-data"))
    if any(has_function_access(request.user, code) for code in ("vendors.view", "vendors.manage", "allocations.view", "allocations.manage")):
        return HttpResponseRedirect(reverse("vendor-management"))
    if any(has_function_access(request.user, code) for code in ("access.manage", "users.view", "users.create", "roles.view", "roles.create")):
        return HttpResponseRedirect(reverse("access-control"))
    from django.core.exceptions import PermissionDenied
    raise PermissionDenied("No workspace page is assigned to this account.")


def _qualifying_option_values(question):
    """Return provider-approved option IDs, translating RFG gender IDs for UI."""

    raw = question.raw_data or {}
    if "targeting_choices" not in raw:
        return None
    allowed = {str(value) for value in raw.get("targeting_choices") or []}
    if not allowed:
        return None
    if question.key == "RFG_GENDER":
        return {
            "M" if value == "1" else "F" if value == "2" else value
            for value in allowed
        }
    return allowed


def _is_postal_targeting_question(key, text):
    """Recognize provider ZIP/postal/PIN qualifications across naming variants."""

    normalized = re.sub(
        r"[^a-z0-9]+",
        " ",
        f"{key or ''} {text or ''}".lower(),
    ).strip()
    return bool(
        re.search(
            r"\b(?:zip\s*codes?|postal\s*codes?|post\s*codes?|pin\s*codes?|pincodes?)\b",
            normalized,
        )
    )


def _innovatemr_postal_targeting_values(question):
    """Return the actual InnovateMR ZIP values, never its sequence OptionIds.

    InnovateMR models a numeric-open-ended ZIP qualification as a very large
    option list. ``OptionId`` is only a row identifier (1, 2, 3, ...), while
    ``OptionText`` is the ZIP/PIN/postal value the respondent must submit.
    """

    values = []
    seen = set()
    for option in question.options or []:
        if isinstance(option, dict):
            value = option.get("OptionText")
            if value in (None, ""):
                value = (
                    option.get("OptionCode")
                    or option.get("OptionValue")
                    or option.get("Value")
                )
        else:
            value = option
        value = clean_rfg_display_text(str(value or "")).strip()
        normalized = value.casefold()
        if value and normalized not in seen:
            seen.add(normalized)
            values.append(value)
    return values


def _normalized_postal_targeting_value(value):
    """Normalize harmless respondent formatting for postal target matching."""

    return re.sub(r"[\s-]+", "", str(value or "")).casefold()


def _postal_targeting_note(values):
    """Build a useful, bounded hint for provider-required postal values."""

    if not values:
        return ""
    preview_limit = 12
    preview = ", ".join(values[:preview_limit])
    if len(values) <= preview_limit:
        return f"Required ZIP/postal codes: {preview}"
    return (
        f"Required ZIP/postal codes: {len(values):,} provider-approved codes "
        f"(examples: {preview})"
    )


PRESCREENER_MAX_AGE = 99


def _age_range_from_label(value):
    """Decode closed and open-ended provider age labels into a safe range."""

    label = clean_rfg_display_text(str(value or "")).strip().lower()
    if not label:
        return None
    label = label.replace("&", " and ")
    open_match = re.search(
        r"(?<!\d)(\d{1,3})\s*(?:years?|yrs?)?\s*(?:\+|plus\b|"
        r"(?:(?:and|or)\s+)?(?:older|over|above|more|up)\b)",
        label,
    )
    if not open_match:
        open_match = re.search(
            r"\b(?:over|above|older\s+than)\s*(\d{1,3})\b",
            label,
        )
    if open_match:
        start = int(open_match.group(1))
        return (
            {"ageStart": start, "ageEnd": PRESCREENER_MAX_AGE}
            if 0 <= start <= PRESCREENER_MAX_AGE else None
        )
    closed_match = re.search(
        r"(?<!\d)(\d{1,3})\s*(?:-|\u2013|\u2014|to)\s*(\d{1,3})(?!\d)",
        label,
        re.IGNORECASE,
    )
    if closed_match:
        start, end = int(closed_match.group(1)), int(closed_match.group(2))
        end = min(end, PRESCREENER_MAX_AGE)
        return {"ageStart": start, "ageEnd": end} if 0 <= start <= end else None
    exact_match = re.fullmatch(r"\s*(\d{1,3})\s*(?:years?|yrs?)?\s*", label)
    if exact_match:
        age = int(exact_match.group(1))
        return (
            {"ageStart": age, "ageEnd": age}
            if 0 <= age <= PRESCREENER_MAX_AGE else None
        )
    return None


def _age_range_from_payload(item):
    """Normalize one provider option/range without widening closed ranges."""

    if not isinstance(item, dict):
        return _age_range_from_label(item)
    for key in (
        "OptionText", "Translation", "Label", "label", "Range", "range",
        "DisplayText", "display_text", "Name", "name",
    ):
        parsed = _age_range_from_label(item.get(key))
        if parsed:
            return parsed

    def integer(*keys):
        for key in keys:
            value = item.get(key)
            if value not in (None, ""):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
        return None

    start = integer("min", "ageStart", "start", "from")
    end = integer("max", "ageEnd", "end", "to")
    if start is None:
        return None
    # A missing upper bound is the provider-neutral representation of `N+`.
    if end is None:
        end = PRESCREENER_MAX_AGE
    end = min(end, PRESCREENER_MAX_AGE)
    return {"ageStart": start, "ageEnd": end} if 0 <= start <= end else None


def _rfg_profile_dimension(question):
    """Return the mandatory profile dimension represented by an RFG row."""

    key = re.sub(r"[^a-z0-9]+", " ", str(question.key or "").lower()).strip()
    text = re.sub(
        r"[^a-z0-9]+",
        " ",
        clean_rfg_display_text(question.text or "").lower(),
    ).strip()
    combined = f"{key} {text}"
    if re.search(r"\b(gender|sex)\b", combined):
        return "gender"
    if re.search(r"\b(date of birth|birthday|dob|age)\b", combined):
        return "age"
    if re.search(r"\b(postal code|postcode|zip code|zipcode|zip)\b", combined):
        return "postal"
    return ""


def _rfg_alias_allowed_values(question, dimension):
    """Translate targeting choices to the mandatory profile control values."""

    choices = {
        str(value) for value in (question.raw_data or {}).get("targeting_choices") or []
    }
    if dimension == "gender":
        return {
            "M" if value == "1" else "F" if value == "2" else value
            for value in choices
        }
    return choices


def _rfg_alias_upstream_values(alias, dimension, values):
    """Map one displayed profile answer back to a hidden RFG targeting code."""

    if dimension != "gender" or not values:
        return list(values)
    selected = str(values[0]).upper()
    wanted_label = "male" if selected in {"M", "1"} else "female"
    for option in alias.options or []:
        label = clean_rfg_display_text(option.get("OptionText") or "").lower().strip()
        if label == wanted_label:
            option_id = option.get("OptionId")
            if option_id not in (None, ""):
                return [str(option_id)]
    return ["1" if wanted_label == "male" else "2"]


def _prescreener_questions(survey, submitted_data=None, *, qualifying_options_only=True):
    """Prepare provider targeting rows as safe, responsive form controls and hints."""

    prepared = []
    provider_code = str(
        survey.integration.provider_code
        if survey.integration_id else "innovatemr"
    ).lower()
    question_rows = list(survey.targeting_questions.all())
    if provider_code == "cint" and not question_rows:
        # Some open Cint opportunities genuinely have no qualifications. We
        # still need a minimal reusable profile, so collect age and gender as
        # platform-only answers. Empty question IDs/upstream values guarantee
        # these controls are never appended to the signed Cint entry URL.
        question_rows = [
            SimpleNamespace(
                pk="platform_profile_age",
                question_id="",
                key="AGE",
                text="What is your age?",
                question_type="Numeric",
                category="Required profile",
                options=[],
                raw_data={
                    "platform_only": True,
                    "targeting_age_ranges": [{"min": 13, "max": 120}],
                },
            ),
            SimpleNamespace(
                pk="platform_profile_gender",
                question_id="",
                key="GENDER",
                text="What is your gender?",
                question_type="Single Punch",
                category="Required profile",
                options=[
                    {"OptionId": "male", "OptionText": "Male"},
                    {"OptionId": "female", "OptionText": "Female"},
                ],
                raw_data={"platform_only": True},
            ),
        ]
    profile_aliases = {}
    aliased_question_ids = set()
    if provider_code == "rfg":
        required = {}
        for question in question_rows:
            dimension = _rfg_profile_dimension(question)
            is_required = (
                str(question.category or "").strip().lower() == "required profile"
                or str(question.key or "").upper()
                in {"RFG_BIRTHDAY", "RFG_GENDER", "RFG_POSTAL_CODE"}
            )
            if dimension and is_required:
                required[dimension] = question
        for question in question_rows:
            dimension = _rfg_profile_dimension(question)
            primary = required.get(dimension)
            if primary and primary.pk != question.pk:
                profile_aliases.setdefault(primary.pk, []).append(question)
                aliased_question_ids.add(question.pk)

    for question in question_rows:
        if question.pk in aliased_question_ids:
            continue
        display_text = clean_rfg_display_text(question.text or question.key)
        lowered_type = question.question_type.lower()
        normalized_key = str(question.key or "").upper()
        normalized_text = display_text.lower()
        is_dob_question = (
            normalized_key in {"DOB", "BIRTHDAY", "RFG_BIRTHDAY"}
            or "date of birth" in normalized_text
            or "birthday" in normalized_text
        )
        is_age_question = (
            normalized_key == "AGE"
            or ("your age" in normalized_text and not is_dob_question)
        )
        is_postal_question = _is_postal_targeting_question(
            normalized_key,
            normalized_text,
        )
        options = []
        provider_option_codes = {}
        age_ranges = []
        allowed_values = _qualifying_option_values(question)
        postal_targeting_values = []
        if provider_code == "innovatemr" and is_postal_question:
            postal_targeting_values = _innovatemr_postal_targeting_values(question)
            if postal_targeting_values:
                # Innovate's ZIP OptionIds are sequence numbers. Validate the
                # respondent against the corresponding OptionText values.
                allowed_values = set(postal_targeting_values)
        dimension = _rfg_profile_dimension(question) if provider_code == "rfg" else ""
        alias_allowed_sets = [
            _rfg_alias_allowed_values(alias, dimension)
            for alias in profile_aliases.get(question.pk, [])
            if (alias.raw_data or {}).get("targeting_choices")
        ]
        for alias_allowed in alias_allowed_sets:
            allowed_values = (
                set(alias_allowed)
                if allowed_values is None
                else set(allowed_values).intersection(alias_allowed)
            )
        # InnovateMR ZIP qualifications can contain tens of thousands of
        # values. They are validated from ``postal_targeting_values`` above;
        # do not build a duplicate choice structure for an open text input.
        rendered_option_rows = (
            []
            if provider_code == "innovatemr" and is_postal_question
            else question.options or []
        )
        for option in rendered_option_rows:
            # Legacy BioBrain rows stored bare OptionIds. Treat them as safe
            # fallback choices until the localized qualification refresh below
            # replaces them with `{OptionId, OptionText}` objects.
            if not isinstance(option, dict):
                option = {"OptionId": option, "OptionText": str(option)}
            option_id = option.get("OptionId")
            if option.get("ageStart") is not None:
                label = clean_rfg_display_text(
                    option.get("OptionText")
                    or f"{option.get('ageStart')}–{option.get('ageEnd')}"
                )
            else:
                label = clean_rfg_display_text(
                    option.get("OptionText") or str(option_id or "Option")
                )
            value = str(option_id if option_id is not None else label)
            option_code = option.get("OptionCode")
            if option_code not in (None, ""):
                provider_option_codes[value] = str(option_code)
            option_is_qualified = not allowed_values or value in allowed_values
            if (is_dob_question or is_age_question) and option_is_qualified:
                normalized_range = _age_range_from_payload(option)
                if normalized_range:
                    age_ranges.append(normalized_range)
            if qualifying_options_only and allowed_values and value not in allowed_values:
                continue
            options.append({"value": value, "label": label})
        if is_dob_question or is_age_question:
            for item in (question.raw_data or {}).get("targeting_age_ranges") or []:
                normalized_range = _age_range_from_payload(item)
                if normalized_range:
                    age_ranges.append(normalized_range)
            for alias in profile_aliases.get(question.pk, []):
                for item in (alias.raw_data or {}).get("targeting_age_ranges") or []:
                    normalized_range = _age_range_from_payload(item)
                    if normalized_range:
                        age_ranges.append(normalized_range)
        if (is_dob_question or is_age_question) and not age_ranges and allowed_values:
            # Compatibility for Cint targeting stored before explicit age
            # ranges were normalized. Prefer the provider's visible labels so
            # grouped precodes such as "18-24" are not mistaken for ages 1/2.
            for option in options:
                if allowed_values and option["value"] not in allowed_values:
                    continue
                label = str(option["label"]).strip()
                normalized_range = _age_range_from_label(label)
                if normalized_range:
                    age_ranges.append(normalized_range)
        if age_ranges:
            merged_age_ranges = []
            for item in sorted(age_ranges, key=lambda row: int(row["ageStart"])):
                start, end = int(item["ageStart"]), int(item["ageEnd"])
                if merged_age_ranges and start <= merged_age_ranges[-1]["ageEnd"] + 1:
                    merged_age_ranges[-1]["ageEnd"] = max(
                        merged_age_ranges[-1]["ageEnd"], end
                    )
                else:
                    merged_age_ranges.append({"ageStart": start, "ageEnd": end})
            age_ranges = merged_age_ranges
        if is_dob_question:
            input_kind = "date_mask"
            display_text = "What is your date of birth?"
        elif is_age_question:
            input_kind = "number"
            display_text = "What is your age?"
        elif is_postal_question:
            # Postal codes are identifiers, not numbers: leading zeroes and
            # country-specific letters must survive exactly as entered.
            input_kind = "text"
        elif "date" in lowered_type:
            input_kind = "date_mask"
        elif "multi" in lowered_type:
            input_kind = "checkbox"
        elif "single" in lowered_type and options:
            input_kind = "radio"
        elif options:
            # Providers sometimes label derived/boolean qualifications as
            # ``Dummy`` (for example Region or Mobile Device) even though
            # they supply a closed option list. A fixed list must never fall
            # back to a free-text field: one respondent value is selected.
            input_kind = "radio"
        elif question.key.upper() == "AGE" or "numeric" in lowered_type:
            input_kind = "number"
        else:
            input_kind = "text"
        field_name = f"question_{question.pk}"
        selected_values = submitted_data.getlist(field_name) if submitted_data is not None else []
        current_value = selected_values[0] if selected_values else ""
        if input_kind == "date_mask" and current_value:
            try:
                current_value = date.fromisoformat(current_value).strftime("%d-%m-%Y")
            except ValueError:
                pass
        for option in options:
            option["selected"] = option["value"] in selected_values
        min_value = min((int(item["ageStart"]) for item in age_ranges), default=None)
        max_value = max((int(item["ageEnd"]) for item in age_ranges), default=None)
        if is_age_question:
            max_value = min(
                max_value if max_value is not None else PRESCREENER_MAX_AGE,
                PRESCREENER_MAX_AGE,
            )
        age_range_labels = [
            f"{int(item['ageStart'])}\u2013{int(item['ageEnd'])}"
            for item in age_ranges
        ]
        qualifying_labels = [
            option["label"] for option in options
            if not allowed_values or option["value"] in allowed_values
        ]
        if (
            allowed_values
            and qualifying_options_only
            and not qualifying_labels
            and not postal_targeting_values
        ):
            qualifying_labels = sorted(allowed_values)
        if qualifying_labels:
            answer_word = "answer" if len(qualifying_labels) == 1 else "answers"
            qualifying_answer_note = (
                f"Qualifying {answer_word}: {', '.join(qualifying_labels)}"
                if len(qualifying_labels) <= 6
                else f"{len(qualifying_labels)} provider-approved answers are shown."
            )
        else:
            qualifying_answer_note = ""
        prepared.append({
            "model": question,
            "profile_dimension": dimension,
            "aliases": profile_aliases.get(question.pk, []),
            "display_text": display_text,
            "field_name": field_name,
            "input_kind": input_kind,
            "type_label": (
                "Date of birth" if is_dob_question
                else "Age" if is_age_question
                else "Postal code" if is_postal_question
                else "Date" if input_kind == "date_mask"
                else (question.question_type or "Question")
            ),
            "options": options,
            # Keep provider codes separate from the browser-visible choices.
            # BioBrain needs the standard gender code in addition to its
            # qualification OptionId, but the form must still submit the
            # original option value used by every provider mapping.
            "provider_option_codes": provider_option_codes,
            "current_value": current_value,
            "min_value": min_value,
            "max_value": max_value,
            "input_label": (
                "Age" if is_age_question
                else "ZIP / postal code" if is_postal_question
                else "Your answer"
            ),
            "placeholder": (
                "Enter your age" if is_age_question
                else "Enter your ZIP / postal code" if is_postal_question
                else "Type your answer"
            ),
            "is_dob_question": is_dob_question,
            "is_postal_question": is_postal_question,
            "allowed_values": (
                postal_targeting_values
                if postal_targeting_values
                else sorted(allowed_values or [])
            ),
            "qualifying_options_only": bool(
                qualifying_options_only and allowed_values
            ),
            "targeting_note": (
                clean_rfg_display_text(
                    (question.raw_data or {}).get("targeting_note") or ""
                )
                or (
                    f"Qualifying age: {', '.join(age_range_labels)}"
                    if (is_age_question or is_dob_question) and age_range_labels
                    else _postal_targeting_note(postal_targeting_values)
                    if postal_targeting_values
                    else "Only answers accepted by this survey are shown."
                    if provider_code == "rfg" and qualifying_options_only and allowed_values
                    else "Enter a ZIP/postal code accepted by this survey."
                    if is_postal_question and qualifying_options_only and allowed_values
                    else qualifying_answer_note
                    if qualifying_options_only and allowed_values else ""
                )
            ),
        })
    return prepared


PRESCREENER_MAX_TEXT_LENGTH = 1000
PRESCREENER_MAX_LIST_VALUES = 100
PRESCREENER_MAX_NUMERIC_LENGTH = 32


def _collect_prescreener_answers(request, survey):
    """Validate submitted controls and produce vault plus provider answer values."""

    answers = {}
    errors = []
    provider_code = str(
        getattr(getattr(survey, "integration", None), "provider_code", "") or ""
    ).lower()
    # BioBrain is the authoritative qualifier for its own inventory. Keep
    # basic form safety (required fields, types and real rendered options),
    # but do not locally stop a respondent for a provider age/ZIP requirement.
    # The upstream survey must receive the profile and make that decision.
    bypass_provider_qualification_checks = provider_code in {"biobrain", "voqall"}
    for prepared in _prescreener_questions(
        survey, qualifying_options_only=False
    ):
        question = prepared["model"]
        input_kind = prepared["input_kind"]
        raw_values = request.POST.getlist(prepared["field_name"])
        values = [str(value).strip() for value in raw_values if str(value).strip()]
        if not values:
            errors.append(f"Please answer: {prepared['display_text']}")
            continue

        # Browsers submit one value for scalar controls. Reject an edited POST
        # containing repeated scalar keys instead of letting QueryDict choose
        # one value. Multi-select controls deliberately accept repeated keys,
        # but normalize duplicates before provider mapping.
        if input_kind != "checkbox" and len(raw_values) != 1:
            errors.append(
                f"Submit exactly one answer for: {prepared['display_text']}"
            )
            continue
        if input_kind == "checkbox":
            list_limit = max(PRESCREENER_MAX_LIST_VALUES, len(prepared["options"]))
            if len(raw_values) > list_limit:
                errors.append(
                    f"Too many answers were submitted for: {prepared['display_text']}"
                )
                continue
            values = list(dict.fromkeys(values))

        if any(len(value) > PRESCREENER_MAX_TEXT_LENGTH for value in values):
            errors.append(f"Answer is too long for: {prepared['display_text']}")
            continue

        if input_kind == "date_mask":
            raw_date = values[0]
            try:
                parts = raw_date.split("-")
                if len(parts) != 3:
                    raise ValueError
                if len(parts[0]) == 4:
                    year, month, day = parts
                else:
                    day, month, year = parts
                normalized_date = date(int(year), int(month), int(day)).isoformat()
            except (TypeError, ValueError):
                errors.append(
                    f"Enter a valid date in DD-MM-YYYY format for: {prepared['display_text']}"
                )
                continue
            values = [normalized_date]

        valid_options = {item["value"] for item in prepared["options"]}
        upstream_values = values.copy()
        if input_kind in {"radio", "checkbox"}:
            invalid = [value for value in values if value not in valid_options]
            if invalid:
                errors.append(f"Invalid answer for: {prepared['display_text']}")
                continue
        elif input_kind == "number":
            if len(values[0]) > PRESCREENER_MAX_NUMERIC_LENGTH:
                errors.append(f"Enter a valid number for: {prepared['display_text']}")
                continue
            try:
                numeric_value = int(values[0])
            except ValueError:
                errors.append(f"Enter a valid number for: {prepared['display_text']}")
                continue
            min_value = prepared.get("min_value")
            max_value = prepared.get("max_value")
            if (
                not bypass_provider_qualification_checks
                and (
                    (min_value is not None and numeric_value < min_value)
                    or (max_value is not None and numeric_value > max_value)
                )
            ):
                if min_value is not None and max_value is not None:
                    requirement = f"between {min_value} and {max_value}"
                elif min_value is not None:
                    requirement = f"of at least {min_value}"
                else:
                    requirement = f"of at most {max_value}"
                errors.append(
                    f"Enter a number {requirement} for: {prepared['display_text']}"
                )
                continue
            # AGE and other numeric-open-ended qualifications must carry the
            # respondent's actual answer. Targeting OptionIds identify the
            # provider's accepted range, not the respondent's age.
            upstream_values = [str(numeric_value)]
        elif (
            not bypass_provider_qualification_checks
            and prepared.get("is_postal_question")
            and prepared.get("allowed_values")
        ):
            accepted = {}
            for value in prepared["allowed_values"]:
                accepted.setdefault(
                    _normalized_postal_targeting_value(value),
                    str(value),
                )
            canonical_value = accepted.get(
                _normalized_postal_targeting_value(values[0])
            )
            if canonical_value is None:
                errors.append(
                    f"Enter a ZIP/postal code accepted by this survey for: {prepared['display_text']}"
                )
                continue
            # Preserve the provider's exact value/casing/spacing in the vault
            # and outbound query even when the respondent varies letter case.
            values = [canonical_value]
            upstream_values = [canonical_value]

        platform_only = bool((question.raw_data or {}).get("platform_only"))
        if platform_only:
            upstream_values = []

        answers[str(question.pk)] = {
            "question_id": question.question_id,
            "question_key": question.key,
            "question_text": prepared["display_text"],
            "question_type": question.question_type,
            "question_category": question.category,
            "values": values,
            "upstream_values": upstream_values,
            "provider_option_codes": [
                prepared["provider_option_codes"][value]
                for value in values
                if value in prepared.get("provider_option_codes", {})
            ],
            "platform_only": platform_only,
        }
        for alias in prepared.get("aliases", []):
            alias_upstream_values = _rfg_alias_upstream_values(
                alias,
                prepared.get("profile_dimension", ""),
                upstream_values,
            )
            answers[str(alias.pk)] = {
                "question_id": alias.question_id,
                "question_key": alias.key,
                "question_text": clean_rfg_display_text(alias.text or alias.key),
                "values": values,
                "upstream_values": alias_upstream_values,
                "profile_alias": question.key,
            }
    return answers, errors


def _invalid_survey_link(request, message="This link is invalid or is no longer available.", status_code=400):
    """Render the generic public error without leaking which validation failed."""

    return render(request, "surveys/flow_error.html", {
        "title": "Invalid survey link",
        "message": message,
    }, status=status_code)


def _has_exact_query(request, expected_names):
    """Reject duplicated or client-injected start-link parameters."""
    return set(request.GET.keys()) == set(expected_names) and all(
        len(request.GET.getlist(name)) == 1 for name in expected_names
    )


def _rfg_result_url(identifier, result):
    """Build the local RFG browser-result URL with the public platform PID."""

    return f"{reverse('rfg-result')}?{urlencode({'pid': identifier, 'result': result})}"


def _finish_local_rfg_attempt(attempt, answers, request, *, result, reason):
    """Atomically finalize a strict-mode RFG rejection before provider redirect."""

    now = timezone.now()
    with transaction.atomic():
        locked = SurveyAttempt.objects.select_for_update().get(pk=attempt.pk)
        if locked.status != SurveyAttempt.Status.INITIATED:
            transaction.on_commit(
                lambda locked=locked: queue_supplier_result_callback(locked)
            )
            return locked
        client_data = get_request_client_data(request)
        locked.answers = operational_answer_value(answers)
        locked.submitted_at = now
        locked.callback_at = now
        locked.last_callback_at = now
        locked.callback_ip = get_request_ip(request) or locked.initiation_ip
        locked.exit_user_agent = client_data.get("user_agent", "")
        locked.exit_browser = client_data.get("browser", "")
        locked.exit_device = client_data.get("device", "")
        locked.exit_os = client_data.get("os", "")
        locked.exit_client_data = client_data
        locked.status = RFG_STATUS_MAP[result]
        locked.status_source = "local_prescreener"
        locked.loi_seconds = locked.calculate_loi_seconds(now)
        locked.upstream_transaction_data = {
            **(locked.upstream_transaction_data or {}),
            "rfg_local_outcome": {"result": result, "local_reason": reason},
        }
        locked.save(update_fields=[
            "answers", "submitted_at", "callback_at", "last_callback_at", "callback_ip",
            "exit_user_agent", "exit_browser", "exit_device", "exit_os", "exit_client_data",
            "status", "status_source", "loi_seconds", "upstream_transaction_data", "updated_at",
        ])
        finalize_attempt_capacity(locked)
    queue_supplier_result_callback(locked)
    return locked


def _finish_wrong_target_country_attempt(attempt, request, location):
    """Record a local S4 before any prescreener question or provider redirect."""

    now = timezone.now()
    expected = survey_target_country_code(attempt.survey)
    actual = str((location or {}).get("country_code") or "").upper()
    vault_answers = wrong_target_country_answers(attempt, location)
    if settings.PRESCREENER_VAULT_ENABLED:
        try:
            capture_prescreener_submission(attempt, vault_answers, submitted_at=now)
        except PrescreenerVaultError:
            # Country enforcement must still protect the provider contract. The
            # failed vault write remains visible in logs for operational retry.
            logger.exception(
                "Wrong-target-country vault capture failed for rid=%s", attempt.rid
            )

    with transaction.atomic():
        locked = SurveyAttempt.objects.select_for_update().get(pk=attempt.pk)
        if locked.status != SurveyAttempt.Status.INITIATED:
            transaction.on_commit(
                lambda locked=locked: queue_supplier_result_callback(locked)
            )
            return locked
        client_data = {
            **get_request_client_data(request),
            **geolocation_client_data(location),
        }
        locked.answers = operational_answer_value(vault_answers)
        locked.submitted_at = now
        locked.callback_at = now
        locked.last_callback_at = now
        locked.callback_ip = get_request_ip(request) or locked.initiation_ip
        locked.exit_user_agent = client_data.get("user_agent", "")
        locked.exit_browser = client_data.get("browser", "")
        locked.exit_device = client_data.get("device", "")
        locked.exit_os = client_data.get("os", "")
        locked.exit_client_data = client_data
        locked.status = SurveyAttempt.Status.QUALITY_TERMINATED
        locked.status_source = "local_country_guard"
        locked.loi_seconds = locked.calculate_loi_seconds(now)
        locked.upstream_transaction_data = {
            **(locked.upstream_transaction_data or {}),
            "local_country_guard": {
                "status": "Wrong target country",
                "reason": "Wrong target country",
                "expected_country": expected,
                "detected_country": actual,
                "geo_source": str((location or {}).get("source") or ""),
            },
        }
        locked.save(update_fields=[
            "answers", "submitted_at", "callback_at", "last_callback_at", "callback_ip",
            "exit_user_agent", "exit_browser", "exit_device", "exit_os", "exit_client_data",
            "status", "status_source", "loi_seconds", "upstream_transaction_data", "updated_at",
        ])
        finalize_attempt_capacity(locked)
    queue_supplier_result_callback(locked)
    return locked


def _finish_duplicate_ip_attempt(attempt, request, prior_attempt=None):
    """Record a same-project duplicate entry IP as an immediate local S4."""

    now = timezone.now()
    with transaction.atomic():
        locked = SurveyAttempt.objects.select_for_update().get(pk=attempt.pk)
        if locked.status != SurveyAttempt.Status.INITIATED:
            transaction.on_commit(
                lambda locked=locked: queue_supplier_result_callback(locked)
            )
            return locked
        client_data = get_request_client_data(request)
        locked.submitted_at = now
        locked.callback_at = now
        locked.last_callback_at = now
        locked.callback_ip = get_request_ip(request) or locked.initiation_ip
        locked.exit_user_agent = client_data.get("user_agent", "")
        locked.exit_browser = client_data.get("browser", "")
        locked.exit_device = client_data.get("device", "")
        locked.exit_os = client_data.get("os", "")
        locked.exit_client_data = client_data
        locked.status = SurveyAttempt.Status.QUALITY_TERMINATED
        locked.status_source = "local_duplicate_ip_guard"
        locked.loi_seconds = locked.calculate_loi_seconds(now)
        locked.upstream_transaction_data = {
            **(locked.upstream_transaction_data or {}),
            "local_ip_guard": {
                "status": "Security terminated",
                "reason": "Duplicate IP address",
                "first_attempt_rid": getattr(prior_attempt, "rid", ""),
            },
        }
        locked.save(update_fields=[
            "submitted_at", "callback_at", "last_callback_at", "callback_ip",
            "exit_user_agent", "exit_browser", "exit_device", "exit_os", "exit_client_data",
            "status", "status_source", "loi_seconds", "upstream_transaction_data", "updated_at",
        ])
        finalize_attempt_capacity(locked)
    queue_supplier_result_callback(locked)
    return locked


def _finish_verisoul_attempt(attempt, request, *, reason, decision="", score=None, request_id=""):
    """Fail closed as S4 when Verisoul does not approve the browser session."""

    now = timezone.now()
    with transaction.atomic():
        locked = SurveyAttempt.objects.select_for_update().get(pk=attempt.pk)
        if locked.status != SurveyAttempt.Status.INITIATED:
            transaction.on_commit(
                lambda locked=locked: queue_supplier_result_callback(locked)
            )
            return locked
        client_data = get_request_client_data(request)
        locked.submitted_at = now
        locked.callback_at = now
        locked.last_callback_at = now
        locked.callback_ip = get_request_ip(request) or locked.initiation_ip
        locked.exit_user_agent = client_data.get("user_agent", "")
        locked.exit_browser = client_data.get("browser", "")
        locked.exit_device = client_data.get("device", "")
        locked.exit_os = client_data.get("os", "")
        locked.exit_client_data = client_data
        locked.status = SurveyAttempt.Status.QUALITY_TERMINATED
        locked.status_source = "verisoul_security"
        locked.loi_seconds = locked.calculate_loi_seconds(now)
        locked.upstream_transaction_data = {
            **(locked.upstream_transaction_data or {}),
            "verisoul": {
                "status": "Security terminated",
                "reason": str(reason or "Verisoul verification was not approved.")[:240],
                "decision": str(decision or "")[:40],
                "account_score": str(score) if score is not None else "",
                "request_id": str(request_id or "")[:160],
            },
        }
        locked.save(update_fields=[
            "submitted_at", "callback_at", "last_callback_at", "callback_ip",
            "exit_user_agent", "exit_browser", "exit_device", "exit_os", "exit_client_data",
            "status", "status_source", "loi_seconds", "upstream_transaction_data", "updated_at",
        ])
        finalize_attempt_capacity(locked)
    queue_supplier_result_callback(locked)
    return locked


def _verisoul_status_url(attempt):
    return _recorded_status_url(attempt, "4")


def _recorded_status_url(attempt, status_code):
    """Build a trusted local result URL without invoking provider callback checks."""

    return f"{reverse('survey-status')}?{urlencode({'status': str(status_code), 'pid': attempt.pid})}"


JOURNEY_SESSION_KEY = "survey_journeys_v1"
MAX_SESSION_JOURNEYS = 20


def _issue_attempt_journey(request, attempt):
    """Bind an opaque attempt continuation to the current browser session."""

    journeys = dict(request.session.get(JOURNEY_SESSION_KEY) or {})
    nonce = secrets.token_urlsafe(32)
    journeys[str(attempt.pk)] = nonce
    while len(journeys) > MAX_SESSION_JOURNEYS:
        journeys.pop(next(iter(journeys)))
    request.session[JOURNEY_SESSION_KEY] = journeys
    return issue_journey_token(attempt_id=attempt.pk, nonce=nonce)


def _journey_attempt_id(request, token):
    """Return the attempt id only when token integrity and session binding match."""

    try:
        payload = decode_journey_token(token)
    except EntryTokenError:
        return None
    expected = str(
        (request.session.get(JOURNEY_SESSION_KEY) or {}).get(
            str(payload["attempt_id"]), ""
        )
    )
    if not expected or not hmac.compare_digest(expected, payload["nonce"]):
        return None
    return payload["attempt_id"]


def _attempt_start_url(attempt, tracking_name="pid", *, journey_token=""):
    """Return an opaque continuation, or an explicitly enabled legacy URL."""

    if journey_token:
        return f"{reverse('survey-start')}?{urlencode({'journey': journey_token})}"
    if tracking_name == "rid":
        return f"{reverse('survey-start')}?{urlencode({'rid': attempt.rid})}"
    return f"{reverse('survey-start')}?{urlencode({'pid': attempt.pid})}"


@require_http_methods(["POST"])
def survey_security_check(request):
    """Authenticate a Verisoul browser session server-side and return only a route decision."""

    try:
        payload = json.loads(request.body or b"{}")
    except (TypeError, ValueError):
        return JsonResponse({"detail": "Invalid request."}, status=400)
    journey_token = str(payload.get("journey") or "").strip()
    posted_pid = str(payload.get("pid") or "").strip()
    posted_rid = str(payload.get("rid") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    attempt_filter = None
    tracking_name = "pid"
    if journey_token:
        if posted_pid or posted_rid:
            return JsonResponse({"detail": "Invalid attempt."}, status=400)
        attempt_id = _journey_attempt_id(request, journey_token)
        if attempt_id is not None:
            attempt_filter = {"pk": attempt_id}
    elif settings.ALLOW_LEGACY_UNSIGNED_ENTRY_LINKS:
        if bool(posted_pid) == bool(posted_rid):
            return JsonResponse({"detail": "Invalid attempt."}, status=400)
        identifier = posted_pid or posted_rid
        tracking_name = "pid" if posted_pid else "rid"
        if identifier.isalnum():
            attempt_filter = {tracking_name: identifier}
    if attempt_filter is None or len(session_id) > 512:
        return JsonResponse({"detail": "Invalid attempt."}, status=400)
    attempt = SurveyAttempt.objects.select_related(
        "survey", "survey__client", "survey__integration", "client", "client_allocation",
        "platform_user", "platform_user__employee_profile",
    ).filter(**attempt_filter).first()
    if attempt is None or attempt.status != SurveyAttempt.Status.INITIATED:
        return JsonResponse({"detail": "Invalid or finished attempt."}, status=404)
    continuation_url = _attempt_start_url(
        attempt,
        tracking_name,
        journey_token=journey_token,
    )
    policy = effective_verisoul_policy(attempt)
    if not policy.enabled:
        return JsonResponse({"status": "passed", "redirect": continuation_url})

    if not session_id:
        assessment, _ = VerisoulAssessment.objects.get_or_create(
            attempt=attempt,
            defaults={"client_id": policy.client_id, "policy_scope": policy.scope, "policy_scope_id": policy.scope_id},
        )
        reason = "The browser security session could not be collected."
        assessment.status = VerisoulAssessment.Status.ERROR
        assessment.reason = reason
        assessment.assessed_at = timezone.now()
        assessment.save(update_fields=["status", "reason", "assessed_at", "updated_at"])
        _finish_verisoul_attempt(attempt, request, reason=reason)
        return JsonResponse({"status": "blocked", "redirect": _verisoul_status_url(attempt)})

    now = timezone.now()
    with transaction.atomic():
        assessment, created = VerisoulAssessment.objects.select_for_update().get_or_create(
            attempt=attempt,
            defaults={
                "client_id": policy.client_id,
                "policy_scope": policy.scope,
                "policy_scope_id": policy.scope_id,
                "session_id": session_id,
            },
        )
        if assessment.status == VerisoulAssessment.Status.PASSED:
            return JsonResponse({"status": "passed", "redirect": continuation_url})
        if assessment.status in {VerisoulAssessment.Status.FAILED, VerisoulAssessment.Status.ERROR}:
            return JsonResponse({"status": "blocked", "redirect": _verisoul_status_url(attempt)})
        pending_ttl = timedelta(seconds=max(5, int(settings.VERISOUL_PENDING_TTL_SECONDS)))
        if not created and assessment.session_id and assessment.updated_at >= now - pending_ttl:
            return JsonResponse({"status": "pending"}, status=202)
        assessment.session_id = session_id
        assessment.policy_scope = policy.scope
        assessment.policy_scope_id = policy.scope_id
        assessment.save(update_fields=["session_id", "policy_scope", "policy_scope_id", "updated_at"])
    try:
        result = authenticate_verisoul_session(session_id=session_id, attempt=attempt)
    except VerisoulError as exc:
        reason = str(exc)
        assessment.status = VerisoulAssessment.Status.ERROR
        assessment.reason = reason
        assessment.assessed_at = timezone.now()
        assessment.save(update_fields=["status", "reason", "assessed_at", "updated_at"])
        _finish_verisoul_attempt(attempt, request, reason=reason)
        return JsonResponse({"status": "blocked", "redirect": _verisoul_status_url(attempt)})

    assessment.request_id = result.request_id
    assessment.project_id = result.project_id
    assessment.decision = result.decision
    assessment.account_score = result.account_score
    assessment.reason = result.reason
    assessment.response_data = result.response_data
    assessment.assessed_at = timezone.now()
    assessment.status = (
        VerisoulAssessment.Status.PASSED if result.passed else VerisoulAssessment.Status.FAILED
    )
    assessment.save(update_fields=[
        "request_id", "project_id", "decision", "account_score", "reason", "response_data",
        "assessed_at", "status", "updated_at",
    ])
    if result.passed:
        return JsonResponse({"status": "passed", "redirect": continuation_url})
    _finish_verisoul_attempt(
        attempt, request, reason=result.reason, decision=result.decision,
        score=result.account_score, request_id=result.request_id,
    )
    return JsonResponse({"status": "blocked", "redirect": _verisoul_status_url(attempt)})


def _mark_attempt_redirected(attempt, answers, outbound_url):
    """Atomically claim one initiated attempt for its provider redirect.

    Provider adapters can touch the separate prescreener-vault database while
    constructing a URL (for example, Cint assigns and audits an email identity).
    Those operations must finish *before* the main-database write so a slow
    vault operation never holds a ``SurveyAttempt`` row lock.  The conditional
    update is a compare-and-swap: only the first concurrent form submission can
    move the attempt from initiated to redirected.
    """

    now = timezone.now()
    updated = SurveyAttempt.objects.filter(
        pk=attempt.pk,
        status=SurveyAttempt.Status.INITIATED,
    ).update(
        answers=operational_answer_value(answers),
        submitted_at=now,
        redirected_at=now,
        outbound_url=outbound_url,
        status=SurveyAttempt.Status.REDIRECTED,
        updated_at=now,
    )
    return bool(updated)


@require_http_methods(["GET", "POST"])
def survey_start(request):
    """Validate copied links, run the prescreener and redirect one claimed attempt.

    Initial signed GET is read-only; a CSRF-protected POST creates the immutable
    journey. Canonical GET renders questions and the final POST writes the vault,
    applies provider checks and records one outbound redirect.
    """

    if request.method == "GET" and "entry" in request.GET:
        # A short-lived compatibility path is required for Projects pages that
        # were already open when opaque entry tokens were deployed. Their old
        # JavaScript appended a browser-generated PID to the new signed token.
        # The PID is deliberately ignored: the authenticated entry token remains
        # the sole authority for the survey and account. Reject every other
        # extra/duplicated parameter and malformed legacy PID.
        expected_entry_params = {"entry", "pid"} if "pid" in request.GET else {"entry"}
        if (
            not _has_exact_query(request, expected_entry_params)
            or (
                "pid" in request.GET
                and not is_valid_platform_pid(request.GET.get("pid", "").strip())
            )
        ):
            return _invalid_survey_link(request)
        entry_token = request.GET.get("entry", "")
        try:
            decode_entry_token(entry_token)
        except EntryTokenError:
            return _invalid_survey_link(request)
        return render(request, "surveys/entry_gate.html", {
            "entry_token": entry_token,
        })

    signed_entry_post = request.method == "POST" and "entry" in request.POST
    if signed_entry_post or (
        request.method == "GET" and "surveyId" in request.GET
    ):
        signed_entry = signed_entry_post
        platform_pid = ""
        delivery_api_key = None
        delivery_survey_id = None
        survey_id = ""
        supplier_code = ""
        internal_code = ""

        if signed_entry:
            if request.GET or set(request.POST.keys()) - {
                "entry", "csrfmiddlewaretoken"
            } or len(request.POST.getlist("entry")) != 1:
                return _invalid_survey_link(request)
            try:
                entry = decode_entry_token(request.POST.get("entry", ""))
            except EntryTokenError:
                return _invalid_survey_link(request)
            user_id = str(entry["user_id"])
            delivery_survey_id = int(entry["survey_id"])
            if entry.get("api_key_id") is not None:
                try:
                    delivery_api_key = VendorAPIKey.objects.select_related(
                        "vendor", "vendor__employee_profile", "vendor__vendor_commercial_profile"
                    ).get(pk=int(entry["api_key_id"]))
                except VendorAPIKey.DoesNotExist:
                    return _invalid_survey_link(request)
        else:
            # Already-distributed unsigned links remain available only during
            # the explicit grace period. All newly copied links use the opaque
            # branch above and all identifiers are allocated on the server.
            if not settings.ALLOW_LEGACY_UNSIGNED_ENTRY_LINKS:
                return _invalid_survey_link(request)
            has_pid_parameter = "pid" in request.GET
            has_delivery_parameter = "delivery" in request.GET
            required_params = {"surveyId", "supplierCode", "userId", "code"}
            if has_pid_parameter:
                required_params.add("pid")
            if has_delivery_parameter:
                required_params.add("delivery")
            if not _has_exact_query(request, required_params):
                return _invalid_survey_link(request)

            survey_id = request.GET.get("surveyId", "").strip()
            supplier_code = request.GET.get("supplierCode", "").strip()
            internal_code = request.GET.get("code", "").strip()
            user_id = request.GET.get("userId", "").strip()
            platform_pid = request.GET.get("pid", "").strip()
            delivery_token = request.GET.get("delivery", "").strip()
            if (
                not survey_id
                or len(survey_id) > 160
                or not user_id.isdigit()
                or not internal_code.isdigit()
                or len(internal_code) != 14
                or (has_pid_parameter and not is_valid_platform_pid(platform_pid))
            ):
                return _invalid_survey_link(request)
            if has_delivery_parameter:
                try:
                    delivery = decode_delivery_token(delivery_token)
                    delivery_api_key = VendorAPIKey.objects.select_related(
                        "vendor", "vendor__employee_profile", "vendor__vendor_commercial_profile"
                    ).get(pk=int(delivery["api_key_id"]))
                    delivery_survey_id = int(delivery["survey_id"])
                except (KeyError, TypeError, ValueError, signing.BadSignature, VendorAPIKey.DoesNotExist):
                    return _invalid_survey_link(request)

        if delivery_api_key and (
            not delivery_api_key.is_active
            or delivery_api_key.revoked_at
            or (delivery_api_key.expires_at and delivery_api_key.expires_at <= timezone.now())
            or str(delivery_api_key.vendor_id) != user_id
        ):
            return _invalid_survey_link(request)

        platform_user = get_user_model().objects.filter(pk=int(user_id), is_active=True).first()
        if (
            platform_user is None
            or not has_function_access(platform_user, "projects.view")
            or not has_function_access(platform_user, "survey_links.copy")
        ):
            return _invalid_survey_link(request)

        survey_queryset = scope_surveys_for_user(
            Survey.objects.select_related("integration", "client"), platform_user
        )
        if delivery_api_key:
            survey_queryset = scope_surveys_for_api_key(survey_queryset, delivery_api_key)
        survey_lookup = {"pk": delivery_survey_id} if signed_entry else {
            "local_id": internal_code,
            **({"pk": delivery_survey_id} if delivery_survey_id else {}),
        }
        survey = survey_queryset.filter(status=Survey.Status.LIVE, **survey_lookup).first()
        if survey is None:
            return _invalid_survey_link(request)
        expected_survey_id = (
            survey.local_id
            if delivery_api_key and delivery_api_key.survey_id_mode == VendorAPIKey.SurveyIdMode.PROJECT_ID
            else str(survey.source_identifier)
        )
        if not signed_entry and survey_id != expected_survey_id:
            return _invalid_survey_link(request)
        provider_code = (
            survey.integration.provider_code if survey.integration_id else ""
        )
        is_rfg = provider_code == "rfg"
        supports_lazy_entry_link = provider_code in {"rfg", "cint"}
        if not survey.entry_link and not supports_lazy_entry_link:
            return _invalid_survey_link(request)
        expected_supplier_code = settings.PUBLIC_SUPPLIER_CODE
        if not signed_entry and supplier_code != expected_supplier_code:
            return _invalid_survey_link(request)

        if provider_code == "cint":
            try:
                cint_provider = get_provider(survey.integration)
                redirect_ready = cint_provider.redirect_contract_is_current(survey)
            except Exception:
                logger.exception(
                    "Could not validate Cint supplier-link state survey=%s", survey.pk
                )
                redirect_ready = False
            if not redirect_ready:
                try:
                    from .tasks import sync_cint_redirects_task

                    sync_cint_redirects_task.delay(survey.integration_id, batch_size=25)
                except Exception:
                    logger.exception(
                        "Could not queue Cint supplier-link repair survey=%s", survey.pk
                    )
                return _invalid_survey_link(
                    request,
                    "This survey link is still being secured. Please try again shortly.",
                    status_code=503,
                )

        stale = survey.targeting_synced_at is None or (
            survey.source_modified_at and survey.targeting_synced_at < survey.source_modified_at
        )
        if supports_lazy_entry_link:
            stale = stale or not survey.entry_link
        if is_rfg:
            stale = (
                stale
                or not survey.entry_link
                or not survey.targeting_questions.filter(
                    key="RFG_POSTAL_CODE",
                    raw_data__adapter_version__gte=4,
                ).exists()
            )
        if provider_code == "biobrain":
            stale = stale or any(
                not question.text
                or str(question.text).startswith("Qualification ")
                or bool(re.fullmatch(r"Q\d+", str(question.key or ""), re.IGNORECASE))
                or (question.raw_data or {}).get("metadata_hydrated") is not True
                or any(not isinstance(option, dict) for option in (question.options or []))
                for question in survey.targeting_questions.all()
            )
        targeting_warning = ""
        if stale:
            try:
                if survey.integration_id and has_provider(survey.integration.provider_code):
                    get_provider(survey.integration).refresh_details(survey)
                else:
                    replace_survey_targeting(InnovateMRClient(integration=survey.integration), survey)
            except Exception:
                logger.exception(
                    "Provider detail hydration failed for survey=%s integration=%s",
                    survey.pk,
                    survey.integration_id,
                )
                if not survey.entry_link:
                    return _invalid_survey_link(
                        request,
                        "The provider entry link is temporarily unavailable. Please try again shortly.",
                        status_code=503,
                    )
                if not survey.targeting_questions.exists():
                    targeting_warning = "Pre-screening criteria are temporarily unavailable. You can still continue."
        if not survey.entry_link:
            return _invalid_survey_link(
                request,
                "The provider entry link is temporarily unavailable. Please try again shortly.",
                status_code=503,
            )
        entry_ip = get_request_ip(request)
        entry_location = resolve_entry_geolocation(request)
        entry_client_data = {
            **get_request_client_data(request),
            **geolocation_client_data(entry_location),
        }
        try:
            with transaction.atomic():
                allocation_context = resolve_vendor_survey_context(
                    platform_user,
                    survey,
                    require_capacity=True,
                    for_update=True,
                )
                ip_claim, prior_ip_attempt, duplicate_ip = claim_project_entry_ip(survey, entry_ip)
                attempt = create_attempt(
                    survey,
                    platform_user,
                    entry_ip,
                    client_data=entry_client_data,
                    pid=platform_pid or None,
                )
                if not duplicate_ip:
                    attach_project_entry_ip_claim(ip_claim, attempt)
                if allocation_context:
                    reserve_attempt_capacity(
                        attempt,
                        allocation_context.survey_allocation,
                        client_allocation=allocation_context.client_allocation,
                    )
                if delivery_api_key:
                    SurveyAttempt.objects.filter(pk=attempt.pk).update(
                        supplier_api_key_id=delivery_api_key.pk,
                        supplier_delivery_config={
                            "survey_id_mode": delivery_api_key.survey_id_mode,
                            "survey_id": expected_survey_id,
                            "project_id": survey.local_id,
                            "supplier_id": delivery_api_key.vendor_id,
                        },
                    )
                    attempt.supplier_api_key_id = delivery_api_key.pk
                    attempt.supplier_delivery_config = {
                        "survey_id_mode": delivery_api_key.survey_id_mode,
                        "survey_id": expected_survey_id,
                        "project_id": survey.local_id,
                        "supplier_id": delivery_api_key.vendor_id,
                    }
        except AllocationUnavailable as exc:
            return _invalid_survey_link(request, str(exc), status_code=409)
        if duplicate_ip:
            attempt = _finish_duplicate_ip_attempt(attempt, request, prior_ip_attempt)
            return HttpResponseRedirect(_recorded_status_url(attempt, "4"))
        if targeting_warning:
            request.session[f"attempt_warning_{attempt.rid}"] = targeting_warning
        # Newly-issued links continue through a session-bound opaque token. The
        # display PID remains visible on the page, but it is never trusted as a
        # browser-supplied selector for backend work.
        if signed_entry:
            journey_token = _issue_attempt_journey(request, attempt)
            return HttpResponseRedirect(
                _attempt_start_url(attempt, journey_token=journey_token)
            )
        return HttpResponseRedirect(_attempt_start_url(attempt, "rid"))

    journey_token = ""
    attempt_filter = None
    tracking_name = "pid"
    if request.method == "GET" and "journey" in request.GET:
        if not _has_exact_query(request, {"journey"}):
            return _invalid_survey_link(request)
        journey_token = request.GET.get("journey", "").strip()
    elif request.method == "POST" and request.POST.get("journey"):
        journey_token = request.POST.get("journey", "").strip()
        if request.POST.get("pid") or request.POST.get("rid"):
            return _invalid_survey_link(request)

    if journey_token:
        attempt_id = _journey_attempt_id(request, journey_token)
        if attempt_id is not None:
            attempt_filter = {"pk": attempt_id}
    elif settings.ALLOW_LEGACY_UNSIGNED_ENTRY_LINKS:
        if request.method == "GET":
            tracking_name = "pid" if request.GET.get("pid") else "rid"
            if not _has_exact_query(request, {tracking_name}):
                return _invalid_survey_link(request)
            tracking_value = request.GET.get(tracking_name, "").strip()
        else:
            posted_pid = request.POST.get("pid", "").strip()
            posted_rid = request.POST.get("rid", "").strip()
            if bool(posted_pid) == bool(posted_rid):
                return _invalid_survey_link(request)
            tracking_name = "pid" if posted_pid else "rid"
            tracking_value = posted_pid or posted_rid
        if not tracking_value or not tracking_value.isalnum():
            return _invalid_survey_link(request)
        if tracking_name == "rid" and len(tracking_value) != 10:
            return _invalid_survey_link(request)
        if tracking_name == "pid" and not is_valid_platform_pid(tracking_value):
            return _invalid_survey_link(request)
        attempt_filter = {tracking_name: tracking_value}

    if attempt_filter is None:
        return _invalid_survey_link(request)
    attempt = SurveyAttempt.objects.select_related(
        "survey", "survey__client", "survey__integration", "platform_user",
        "platform_user__employee_profile", "client", "client_allocation",
    ).filter(**attempt_filter).first()
    if attempt is None or attempt.platform_user is None or not attempt.platform_user.is_active:
        return _invalid_survey_link(request, status_code=404)
    attempt = backfill_attempt_entry_audit(attempt, request)

    entry_location = {
        "ip": attempt.initiation_ip or "",
        "country_code": (attempt.entry_client_data or {}).get("geo_country_code", ""),
        "country": (attempt.entry_client_data or {}).get("geo_country", ""),
        "postal_code": (attempt.entry_client_data or {}).get("geo_postal_code", ""),
        "source": (attempt.entry_client_data or {}).get("geo_source", ""),
    }
    if not entry_location["country_code"] and request.method == "GET":
        entry_location = resolve_entry_geolocation(request)
        geo_updates = geolocation_client_data(entry_location)
        if geo_updates:
            merged_client_data = {**(attempt.entry_client_data or {}), **geo_updates}
            SurveyAttempt.objects.filter(pk=attempt.pk).update(entry_client_data=merged_client_data)
            attempt.entry_client_data = merged_client_data
    if (
        attempt.status == SurveyAttempt.Status.INITIATED
        and is_wrong_target_country(attempt.survey, entry_location)
    ):
        attempt = _finish_wrong_target_country_attempt(attempt, request, entry_location)
        return HttpResponseRedirect(_recorded_status_url(attempt, "4"))

    verisoul_policy = effective_verisoul_policy(attempt)
    if verisoul_policy.enabled and attempt.status == SurveyAttempt.Status.INITIATED:
        assessment = VerisoulAssessment.objects.filter(attempt=attempt).first()
        if assessment and assessment.status in {
            VerisoulAssessment.Status.FAILED, VerisoulAssessment.Status.ERROR,
        }:
            return HttpResponseRedirect(_verisoul_status_url(attempt))
        if not assessment or assessment.status != VerisoulAssessment.Status.PASSED:
            if request.method == "POST":
                return HttpResponseRedirect(_attempt_start_url(
                    attempt, tracking_name, journey_token=journey_token
                ))
            return render(request, "surveys/security_check.html", {
                "attempt": attempt,
                "journey_token": journey_token,
                "tracking_name": tracking_name,
                "verisoul_project_id": settings.VERISOUL_PROJECT_ID,
                "verisoul_sdk_url": verisoul_sdk_url(),
                "security_check_url": reverse("survey-security-check"),
            })

    if request.method == "POST":
        # A browser retry after a successful submission must not call the
        # provider or allocate another cross-database respondent identity.
        if attempt.status != SurveyAttempt.Status.INITIATED:
            return HttpResponseRedirect(
                _attempt_start_url(
                    attempt, tracking_name, journey_token=journey_token
                )
            )
        answers, errors = _collect_prescreener_answers(request, attempt.survey)
        if not errors:
            try:
                if (
                    attempt.survey.integration_id
                    and attempt.survey.integration.provider_code in {"rfg", "biobrain"}
                ):
                    ensure_attempt_prescreener_uid(attempt)
                provider = (
                    get_provider(attempt.survey.integration)
                    if attempt.survey.integration_id
                    and has_provider(attempt.survey.integration.provider_code)
                    else None
                )
                if provider and attempt.survey.integration.provider_code == "rfg":
                    eligible, reason = provider.validate_prescreener(attempt.survey, answers)
                    if not eligible:
                        if settings.PRESCREENER_VAULT_ENABLED:
                            capture_prescreener_submission(
                                attempt,
                                answers_with_entry_postal_code(attempt, answers),
                                allow_draft_replace=True,
                            )
                        _finish_local_rfg_attempt(
                            attempt, answers, request, result="7", reason=reason
                        )
                        return HttpResponseRedirect(_rfg_result_url(attempt.pid, "7"))

                # Select reuse before writing a new vault row. A reused
                # respondent keeps the original vault RID + UID pair and only
                # that row's Visits counter increases. The SurveyAttempt RID is
                # still unique so callbacks cannot collide between journeys.
                reuse_event = maybe_assign_reusable_profile(attempt, answers)
                if settings.PRESCREENER_VAULT_ENABLED and reuse_event is None:
                    capture_prescreener_submission(
                        attempt,
                        answers_with_entry_postal_code(attempt, answers),
                        allow_draft_replace=(
                            attempt.status == SurveyAttempt.Status.INITIATED
                            and not attempt.redirected_at
                            and not attempt.outbound_url
                        ),
                    )

                if provider and attempt.survey.integration.provider_code == "rfg":
                    try:
                        is_duplicate = provider.duplicate_check(
                            attempt.survey,
                            attempt,
                            get_request_ip(request) or attempt.initiation_ip,
                            request.POST.get("rfg_fingerprint", "0"),
                        )
                    except ProviderSurveyUnavailable as exc:
                        # Retire stale/testing inventory immediately. Existing
                        # copied links finish as a recorded Survey Closed result;
                        # fresh Projects listings stop exposing this code.
                        Survey.objects.filter(pk=attempt.survey_id).update(
                            status=Survey.Status.CLOSED,
                            updated_at=timezone.now(),
                        )
                        invalidate_project_cache()
                        _finish_local_rfg_attempt(
                            attempt,
                            answers,
                            request,
                            result="5",
                            reason=str(exc),
                        )
                        return HttpResponseRedirect(
                            _rfg_result_url(attempt.pid, "5")
                        )
                    if is_duplicate:
                        _finish_local_rfg_attempt(
                            attempt,
                            answers,
                            request,
                            result="8",
                            reason="This respondent has already attempted this survey or survey group.",
                        )
                        return HttpResponseRedirect(_rfg_result_url(attempt.pid, "8"))
                if not errors:
                    # URL construction may use the vault DB. Keep it outside a
                    # main-DB row lock, then claim the redirect with one short,
                    # conditional UPDATE to avoid MySQL 1205/1213 failures.
                    if provider:
                        outbound_url = provider.build_outbound_url(
                            attempt.survey, attempt, answers
                        )
                    elif (
                        attempt.survey.integration_id
                        and attempt.survey.integration.provider_code == "biobrain"
                    ):
                        outbound_url = build_biobrain_outbound_url(
                            attempt.survey.entry_link,
                            attempt.rid,
                            attempt.provider_profile_uid or attempt.prescreener_uid,
                            answers,
                        )
                    else:
                        generic_provider_code = (
                            attempt.survey.integration.provider_code
                            if attempt.survey.integration_id
                            else getattr(attempt.survey.client, "provider_code", "")
                        )
                        outbound_url = build_outbound_url(
                            attempt.survey.entry_link,
                            attempt.rid,
                            answers,
                            allowed_host_suffixes=("innovatemr.net",)
                            if generic_provider_code == "innovatemr"
                            else (),
                        )
                    if not _mark_attempt_redirected(attempt, answers, outbound_url):
                        return HttpResponseRedirect(
                            _attempt_start_url(
                                attempt, tracking_name, journey_token=journey_token
                            )
                        )
                    return HttpResponseRedirect(outbound_url)
            except Exception as exc:
                if isinstance(exc, PrescreenerVaultError):
                    logger.exception("Prescreener vault capture failed for rid=%s", attempt.rid)
                    detail = (
                        "No real respondent email is currently available. Please contact the workspace administrator."
                        if isinstance(exc, CintEmailPoolExhausted)
                        else "Secure prescreener storage is temporarily unavailable. Please submit again shortly."
                    )
                else:
                    logger.exception(
                        "Survey provider continuation failed for rid=%s provider=%s",
                        attempt.rid,
                        attempt.survey.integration.provider_code
                        if attempt.survey.integration_id else "legacy",
                    )
                    detail = str(exc) if isinstance(exc, ProviderError) else "The upstream provider is temporarily unavailable."
                errors.append(f"Survey provider could not continue: {detail}")
    else:
        errors = []

    if attempt.status != SurveyAttempt.Status.INITIATED:
        return render(request, "surveys/status.html", {
            "title": "Survey already initiated",
            "message": "This PID has already been used to enter the survey.",
            "tone": "info",
            "status_label": attempt.get_status_display(),
            "pid": attempt.pid,
            "ip_address": attempt.callback_ip or attempt.initiation_ip,
            "loi_seconds": attempt.loi_seconds,
            "attempt_found": True,
        })

    return render(request, "surveys/prescreener.html", {
        "attempt": attempt,
        "survey": attempt.survey,
        "journey_token": journey_token,
        "tracking_name": tracking_name,
        "questions": _prescreener_questions(attempt.survey, request.POST if request.method == "POST" else None),
        "errors": errors,
        "warning": request.session.pop(f"attempt_warning_{attempt.rid}", ""),
        "is_rfg": bool(
            attempt.survey.integration_id
            and attempt.survey.integration.provider_code == "rfg"
        ),
    })


STATUS_PAGES = {
    "1": {"title": "Thank you for participating!", "message": "Your survey response has been completed successfully.", "tone": "success"},
    "2": {"title": "Survey ended", "message": "The survey provider ended this attempt before it could be completed.", "tone": "neutral"},
    "3": {"title": "Quota already filled", "message": "The required quota was filled before your response could be completed.", "tone": "warning"},
    "4": {"title": "Quality check unsuccessful", "message": "This response did not pass the survey's quality checks.", "tone": "danger"},
}

TERMINAL_ATTEMPT_STATUSES = {
    SurveyAttempt.Status.COMPLETED,
    SurveyAttempt.Status.TERMINATED,
    SurveyAttempt.Status.OVER_QUOTA,
    SurveyAttempt.Status.QUALITY_TERMINATED,
}
PENDING_ATTEMPT_STATUSES = {
    SurveyAttempt.Status.INITIATED,
    SurveyAttempt.Status.REDIRECTED,
}


def _invalid_callback_response(request, *, status_code=409):
    """Return one non-disclosing response for forged or replayed outcomes."""

    return render(request, "surveys/flow_error.html", {
        "title": "Invalid survey callback",
        "message": "This survey result could not be verified and was not recorded.",
    }, status=status_code)


def _render_recorded_status(request, attempt, ip_address):
    """Render only the immutable status stored for a finalized attempt."""

    page = STATUS_PAGES.get(attempt.status)
    if page is None:
        return _invalid_callback_response(request)
    status_label = attempt.get_status_display()
    page, status_label = _status_presentation_for_attempt(attempt, page, status_label)
    return render(request, "surveys/status.html", {
        **page,
        "status_label": status_label,
        "pid": attempt.pid,
        "ip_address": ip_address or attempt.callback_ip or attempt.initiation_ip,
        "loi_seconds": attempt.loi_seconds,
        "attempt_found": True,
    })


def _status_presentation_for_attempt(attempt, page, status_label):
    """Explain locally enforced outcomes instead of showing a generic S4."""

    if not attempt:
        return page, status_label
    audit = attempt.upstream_transaction_data or {}
    if attempt.status_source == "local_country_guard":
        guard = audit.get("local_country_guard") or {}
        expected = str(guard.get("expected_country") or "").upper()
        actual = str(guard.get("detected_country") or "").upper()
        message = "Your location does not match this survey's target country."
        if expected and actual:
            message = f"Your detected country ({actual}) does not match this survey's target country ({expected})."
        return {
            "title": "Location not eligible",
            "message": message,
            "tone": "danger",
        }, "Wrong target country"
    if attempt.status_source == "local_duplicate_ip_guard":
        return {
            "title": "Duplicate entry blocked",
            "message": "This IP address has already entered this project, so another entry from the same IP is not allowed.",
            "tone": "danger",
        }, "Duplicate IP blocked"
    return page, status_label


RFG_CALLBACK_IPS = {
    "15.222.163.99", "3.97.223.177", "3.97.28.227", "3.230.105.121",
    "52.21.20.32", "52.45.41.61",
}


def _rfg_attempt_from_request(request):
    """Resolve RFG TID first, then the UID echoed in RFG's ``rid`` field.

    Provider parameter names never replace platform identity. A successful UID
    lookup returns its SurveyAttempt row, whose immutable 10-character ``rid``
    remains the canonical journey key in callbacks, reports and status logic.
    """

    base = SurveyAttempt.objects.select_related("survey__integration").filter(
        survey__integration__provider_code="rfg"
    )
    matched_attempt = None
    for name in ("tid", "TID", "trackId"):
        value = str(request.GET.get(name) or "").strip()
        if value:
            attempt = base.filter(rid=value).first()
            if attempt:
                if matched_attempt and matched_attempt.pk != attempt.pk:
                    return None
                matched_attempt = attempt
    for name in ("rid", "RID", "pid", "PID", "qsid", "QSID"):
        value = str(request.GET.get(name) or "").strip()
        if value:
            if matched_attempt and value in {
                matched_attempt.rid,
                matched_attempt.pid,
                matched_attempt.prescreener_uid,
                matched_attempt.provider_profile_uid,
            }:
                continue
            attempt = base.filter(
                Q(rid=value) | Q(pid=value) | Q(prescreener_uid=value) | Q(provider_profile_uid=value)
            ).order_by("-initiated_at").first()
            if attempt:
                if matched_attempt and matched_attempt.pk != attempt.pk:
                    return None
                matched_attempt = attempt
    return matched_attempt


SENSITIVE_CALLBACK_PARAMETER_NAMES = {
    "hash", "hashdata", "hmac", "sig", "signature", "sesskey",
    "sessionkey", "session_key", "token", "vq_token",
}


def _redacted_callback_parameters(parameters):
    """Retain useful callback evidence without persisting bearer credentials."""

    return {
        key: "[redacted]" if str(key).casefold() in SENSITIVE_CALLBACK_PARAMETER_NAMES else value
        for key, value in parameters.items()
    }


def _request_has_duplicate_query_parameters(request):
    """Reject ambiguous callbacks instead of accepting QueryDict's last value."""

    return any(len(request.GET.getlist(name)) > 1 for name in request.GET.keys())


@require_http_methods(["GET"])
def rfg_result(request):
    if _request_has_duplicate_query_parameters(request):
        return _invalid_callback_response(request, status_code=400)
    attempt = _rfg_attempt_from_request(request)
    if not attempt:
        return _invalid_survey_link(
            request, "This RFG result link is invalid.", status_code=404
        )

    received_parameters = dict(request.GET.items())
    redacted_parameters = _redacted_callback_parameters(received_parameters)
    now = timezone.now()
    exit_ip = get_request_ip(request)
    client_data = get_request_client_data(request)
    with transaction.atomic():
        locked = SurveyAttempt.objects.select_for_update().get(pk=attempt.pk)
        audit = locked.upstream_transaction_data or {}
        # A browser redirect is not proof of outcome. Capture its redacted
        # evidence once, but never let reloads or crafted URLs replace it.
        if "rfg_browser_return" not in audit:
            locked.last_callback_at = now
            # RFG's authenticated project log intentionally has no respondent
            # IP. The browser return is therefore the only truthful source for
            # the exit IP shown in Traffic Reports; it does not decide the
            # outcome itself.
            locked.callback_ip = exit_ip or locked.callback_ip
            locked.exit_user_agent = client_data.get("user_agent", "")
            locked.exit_browser = client_data.get("browser", "")
            locked.exit_device = client_data.get("device", "")
            locked.exit_os = client_data.get("os", "")
            locked.exit_client_data = client_data
            locked.upstream_transaction_data = {
                **audit,
                "rfg_browser_return": redacted_parameters,
            }
            locked.save(update_fields=[
                "last_callback_at", "callback_ip", "exit_user_agent", "exit_browser", "exit_device", "exit_os",
                "exit_client_data", "upstream_transaction_data", "updated_at",
            ])
        attempt = locked

    # The provider's browser redirect is deliberately not trusted as an
    # outcome.  A just-finished respondent should nevertheless see their real
    # result immediately, so make one bounded authenticated LiveAlert log
    # lookup before rendering the pending page. The periodic worker below is
    # the durable fallback if RFG has not published the log entry yet.
    if attempt.status not in TERMINAL_ATTEMPT_STATUSES and attempt.callback_at is None:
        try:
            provider = get_provider(attempt.survey.integration)
            entries = provider.project_log(
                attempt.survey.source_key,
                start=attempt.initiated_at - timedelta(minutes=5),
                end=timezone.now(),
            )
            reconciliation = reconcile_rfg_project_log_entries(
                [attempt], entries, checked_at=timezone.now()
            )
            for finalized_attempt in reconciliation["callback_attempts"]:
                queue_supplier_result_callback(finalized_attempt)
            if reconciliation["matched"]:
                attempt.refresh_from_db()
        except ProviderError as exc:
            logger.info(
                "Immediate RFG log reconciliation unavailable attempt=%s reason=%s",
                attempt.pk,
                str(exc),
            )
        except Exception:
            # Rendering a temporary pending page is safer than treating an
            # upstream timeout as a completion. Celery will retry shortly.
            logger.exception("Immediate RFG log reconciliation failed attempt=%s", attempt.pk)

    stored = attempt.upstream_transaction_data or {}
    browser_parameters = stored.get("rfg_browser_return") or redacted_parameters
    local_parameters = stored.get("rfg_local_outcome") or {}
    callback_parameters = stored.get("rfg_callback") or {}
    reconciled_parameters = stored.get("rfg_log_reconciliation") or {}
    outcome_parameters = (
        reconciled_parameters
        if attempt.status_source == "rfg_log_reconciliation"
        else callback_parameters if attempt.is_verified else local_parameters or browser_parameters
    )
    outcome = describe_rfg_outcome(outcome_parameters, attempt=attempt)
    return render(request, "surveys/rfg_result.html", {
        "attempt": attempt,
        "outcome": outcome,
        "verified": bool(attempt.is_verified or attempt.status_source == "local_prescreener"),
        "verification_pending": bool(
            attempt.status_source != "local_prescreener" and not attempt.is_verified
        ),
    })


class RFGCallbackAPIView(APIView):
    """Receive RFG's server callback from documented RFG callback addresses."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    @extend_schema(
        tags=["RFG Callbacks"],
        summary="Receive a verified Research For Good result callback",
        description=(
            "Called by RFG after a respondent outcome. It updates the RID attempt, exit IP/time, "
            "LOI and allocation state. This is not a normal admin test endpoint: Swagger calls will "
            "normally receive 403 because only RFG's configured server IPs are trusted. Use the "
            "RFG callback preview endpoint to safely understand result/live codes without writing data."
        ),
        parameters=[
            OpenApiParameter("tid", OpenApiTypes.STR, required=False, description="Platform 10-character attempt RID echoed from RFG TID"),
            OpenApiParameter("rid", OpenApiTypes.STR, required=True, description="Persistent prescreener UID echoed from RFG RID; used to resolve the canonical platform RID"),
            OpenApiParameter("result", OpenApiTypes.STR, required=True, description="RFG result code"),
            OpenApiParameter("ruledOutBy", OpenApiTypes.STR, required=False, description="RFG termination reason"),
            OpenApiParameter("sesskey", OpenApiTypes.STR, required=False, description="RFG session identifier"),
            OpenApiParameter("liveP", OpenApiTypes.STR, required=False, description="RFG respondent journey bit field"),
            OpenApiParameter("liveS", OpenApiTypes.STR, required=False, description="RFG security detail code"),
            OpenApiParameter("liveI", OpenApiTypes.STR, required=False, description="RFG invalid-profile detail code"),
            OpenApiParameter("quotaThrottle", OpenApiTypes.STR, required=False, description="RFG quota throttle flag"),
        ],
        responses={200: RFGCallbackResponseSerializer},
    )
    def get(self, request):
        if _request_has_duplicate_query_parameters(request):
            return Response(
                {"detail": "Ambiguous callback parameters."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        result = request.GET.get("result", "").strip()
        attempt = _rfg_attempt_from_request(request)
        if not attempt or result not in RFG_STATUS_MAP:
            return Response({"detail": "Unknown callback."}, status=status.HTTP_400_BAD_REQUEST)
        requested_status = RFG_STATUS_MAP[result]

        integration = attempt.survey.integration
        config = integration.config or {}
        if config.get("callback_security_mode", "ip") != "ip":
            return Response(
                {"detail": "Unsupported callback security mode."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        callback_ip = get_request_ip(request)
        allowed = set(config.get("callback_ip_allowlist") or RFG_CALLBACK_IPS)
        try:
            verified_ip = bool(callback_ip and str(ipaddress.ip_address(callback_ip)) in allowed)
        except ValueError:
            verified_ip = False
        if not verified_ip:
            return Response({"detail": "Callback source is not trusted."}, status=status.HTTP_403_FORBIDDEN)

        now = timezone.now()
        idempotent = False
        with transaction.atomic():
            locked = SurveyAttempt.objects.select_for_update().get(pk=attempt.pk)
            if locked.status in TERMINAL_ATTEMPT_STATUSES:
                if locked.status != requested_status:
                    return Response(
                        {"detail": "Callback conflicts with the recorded final status."},
                        status=status.HTTP_409_CONFLICT,
                    )
                idempotent = True
            elif locked.status not in PENDING_ATTEMPT_STATUSES or locked.callback_at is not None:
                return Response(
                    {"detail": "Callback conflicts with the recorded attempt state."},
                    status=status.HTTP_409_CONFLICT,
                )
            else:
                locked.status = requested_status
                locked.callback_at = locked.callback_at or now
                locked.last_callback_at = now
                locked.callback_ip = callback_ip
                locked.callback_count += 1
                locked.status_source = "rfg_callback"
                locked.is_verified = True
                locked.loi_seconds = locked.calculate_loi_seconds(now)
                callback_parameters = dict(request.GET.items())
                locked.upstream_transaction_data = {
                    **(locked.upstream_transaction_data or {}),
                    "rfg_callback": _redacted_callback_parameters(callback_parameters),
                    "rfg_outcome": _redacted_callback_parameters(
                        describe_rfg_outcome(callback_parameters)
                    ),
                }
                locked.save(update_fields=[
                    "status", "callback_at", "last_callback_at", "callback_ip", "callback_count",
                    "status_source", "is_verified", "loi_seconds", "upstream_transaction_data", "updated_at",
                ])
                finalize_attempt_capacity(locked)
        queue_supplier_result_callback(locked)
        payload = {
            "ok": True,
            "rid": locked.rid,
            "status": locked.status,
        }
        if idempotent:
            payload["idempotent"] = True
        return Response(payload)


def _external_supplier_result_url(attempt, status_code: str) -> str:
    """Compatibility wrapper for existing callers and callback URL tests."""

    return build_supplier_result_url(attempt, status_code)


CALLBACK_TRACKING_PARAMETER_NAMES = (
    "tid", "TID", "trackId", "rid", "RID", "pid", "PID", "qsid", "QSID",
    "token", "vq_token", "vendor_user_id", "vq_uid",
)


def _status_callback_has_duplicate_security_parameters(request):
    """Reject ambiguous query strings instead of trusting QueryDict's last value."""

    return _request_has_duplicate_query_parameters(request) or len(request.GET.getlist("status")) != 1 or any(
        len(request.GET.getlist(name)) > 1
        for name in CALLBACK_TRACKING_PARAMETER_NAMES
    )


def _resolve_status_attempt(attempts, identifiers):
    """Resolve tracking values without letting one value select another journey.

    RID, platform PID and registration UID are unique anchors. Reusable provider
    profile UIDs may point at many attempts, so they can only refine an anchored
    callback or resolve independently when exactly one journey exists.
    """

    identifiers = [str(value).strip() for value in identifiers if str(value).strip()]
    if not identifiers:
        return None
    unique_matches = list(attempts.filter(
        Q(rid__in=identifiers)
        | Q(pid__in=identifiers)
        | Q(prescreener_uid__in=identifiers)
    ))
    unique_match_ids = {attempt.pk for attempt in unique_matches}
    if len(unique_match_ids) > 1:
        return None
    if unique_matches:
        anchor = unique_matches[0]
        anchor_values = {
            anchor.rid,
            anchor.pid,
            anchor.prescreener_uid or "",
            anchor.provider_profile_uid or "",
        }
        profile_values = set(
            attempts.filter(provider_profile_uid__in=identifiers)
            .exclude(provider_profile_uid="")
            .values_list("provider_profile_uid", flat=True)
        )
        if any(value in profile_values and value not in anchor_values for value in identifiers):
            return None
        return anchor

    profile_matches = list(
        attempts.filter(provider_profile_uid__in=identifiers)
        .exclude(provider_profile_uid="")
        .order_by("-initiated_at")
    )
    profile_match_ids = {attempt.pk for attempt in profile_matches}
    return profile_matches[0] if len(profile_match_ids) == 1 else None


@require_http_methods(["GET"])
def survey_status(request):
    if _status_callback_has_duplicate_security_parameters(request):
        return _invalid_callback_response(request)
    status_code = request.GET.get("status", "").strip()
    callback_identifiers = status_identifiers_from_request(request)
    callback_identifier = callback_identifiers[0] if callback_identifiers else ""
    page = STATUS_PAGES.get(status_code)
    if page is None or not callback_identifier:
        return render(request, "surveys/flow_error.html", {
            "title": "Invalid survey status",
            "message": "A valid status (1-4) and tracking ID are required.",
        }, status=400)

    # Providers name their echoed identifiers differently. Resolve every value
    # against our three non-overlapping identifier shapes, keep the internal
    # RID immutable, and expose only status + platform PID after recording.
    attempts = SurveyAttempt.objects.select_related("survey__integration")
    # Canonical journey IDs always win. Provider profile UIDs may deliberately
    # repeat, so that fallback resolves to the newest matching journey only
    # when no RID/PID/new registration UID was returned.
    attempt = _resolve_status_attempt(attempts, callback_identifiers)
    provider_code = (
        attempt.survey.integration.provider_code
        if attempt and attempt.survey.integration_id
        else "innovatemr" if attempt else ""
    )
    ip_address = get_request_ip(request)
    if attempt:
        canonical_query = _has_exact_query(request, {"status", "pid"}) and (
            request.GET.get("pid", "").strip() == attempt.pid
        )
        # The clean PID URL is a display URL only. It may render the immutable
        # result already stored on the attempt, but it can never create or
        # replace a provider outcome.
        if canonical_query:
            if (
                attempt.status not in TERMINAL_ATTEMPT_STATUSES
                or attempt.status != status_code
            ):
                return _invalid_callback_response(request)
            return _render_recorded_status(request, attempt, ip_address)

        # RFG has a dedicated server-to-server callback with an IP allowlist.
        # Some legacy RFG projects still have their browser return URL set to
        # this generic endpoint.  Preserve the browser evidence and show the
        # safe RFG result page instead of a confusing hard failure, but never
        # accept browser parameters as proof of a terminal result.
        if provider_code == "rfg":
            logger.info(
                "Forwarding legacy RFG browser return rid=%s ip=%s to result page",
                attempt.rid,
                ip_address or "unknown",
            )
            return HttpResponseRedirect(
                f"{reverse('rfg-result')}?{request.GET.urlencode()}"
            )

        # Innovate's transaction API may verify and finalize an outcome just
        # before the browser reaches its signed return URL. A stale/incorrect
        # browser hash must never change data, but it also should not hide the
        # already verified, identical result from the respondent. Send only
        # that exact stored result to the clean PID display URL.
        if (
            provider_code == "innovatemr"
            and attempt.status_source == "innovatemr_transaction"
            and attempt.status in TERMINAL_ATTEMPT_STATUSES
            and attempt.status == status_code
        ):
            return HttpResponseRedirect(_recorded_status_url(attempt, status_code))

        innovate_callback_verified = False
        if (
            provider_code == "innovatemr"
            and settings.INNOVATEMR_CALLBACK_HASH_REQUIRED
        ):
            verification = verify_callback_request(request)
            if not verification.valid:
                logger.warning(
                    "Rejected InnovateMR callback rid=%s reason=%s ip=%s",
                    attempt.rid,
                    verification.error,
                    ip_address or "unknown",
                )
                return render(request, "surveys/flow_error.html", {
                    "title": "Invalid survey callback",
                    "message": "This survey result could not be verified and was not recorded.",
                }, status=403)
            innovate_callback_verified = True
        transitioned = False
        with transaction.atomic():
            attempt = SurveyAttempt.objects.select_related(
                "survey__integration"
            ).select_for_update().get(pk=attempt.pk)
            if attempt.status in TERMINAL_ATTEMPT_STATUSES:
                # Raw browser/provider callbacks are single-use even when the
                # replay claims the same result. Only the exact clean PID URL
                # handled above may display a finalized outcome.
                return _invalid_callback_response(request)
            elif (
                attempt.status not in PENDING_ATTEMPT_STATUSES
                or attempt.callback_at is not None
            ):
                return _invalid_callback_response(request)
            else:
                transitioned = True

            now = timezone.now()
            if transitioned:
                exit_client_data = get_request_client_data(request)
                if innovate_callback_verified:
                    exit_client_data["innovatemr_callback"] = {
                        "status": status_code,
                        "termReason": str(
                            request.GET.get("termReason")
                            or request.GET.get("term_reason")
                            or request.GET.get("reason")
                            or ""
                        ).strip()[:1000],
                        "closeQuotaId": str(request.GET.get("closeQuotaId") or "").strip()[:160],
                        "surveyId": str(request.GET.get("surveyId") or "").strip()[:160],
                        "verifiedAt": now.isoformat(),
                    }
                attempt.callback_at = now
                attempt.callback_ip = ip_address
                attempt.loi_seconds = attempt.calculate_loi_seconds(now)
                attempt.status = status_code
                attempt.exit_user_agent = exit_client_data.get("user_agent", "")
                attempt.exit_browser = exit_client_data.get("browser", "")
                attempt.exit_device = exit_client_data.get("device", "")
                attempt.exit_os = exit_client_data.get("os", "")
                attempt.exit_client_data = exit_client_data
                attempt.status_source = (
                    "innovatemr_signed_redirect"
                    if innovate_callback_verified
                    else "browser_callback"
                )
                attempt.last_callback_at = now
                attempt.callback_count += 1
                update_fields = [
                    "callback_at", "callback_ip", "loi_seconds", "status", "exit_user_agent",
                    "exit_browser", "exit_device", "exit_os", "exit_client_data", "status_source",
                    "last_callback_at", "callback_count",
                ]
                callback_data = _redacted_callback_parameters(
                    dict(request.GET.items())
                )
                audit_key = (
                    "rfg_browser_return" if provider_code == "rfg"
                    else "cint_browser_return" if provider_code == "cint"
                    else "innovatemr_browser_return" if innovate_callback_verified
                    else "browser_return"
                )
                audit = {
                    **(attempt.upstream_transaction_data or {}),
                    audit_key: callback_data,
                }
                if provider_code == "rfg":
                    audit["rfg_outcome"] = describe_rfg_outcome(
                        callback_data, attempt=attempt
                    )
                attempt.upstream_transaction_data = audit
                update_fields.append("upstream_transaction_data")
                if innovate_callback_verified:
                    attempt.is_verified = True
                    update_fields.append("is_verified")
                attempt.save(update_fields=list(dict.fromkeys(update_fields + ["updated_at"])))
                finalize_attempt_capacity(attempt)

        # From this point on, redirects and downstream callbacks always use
        # the locked database outcome, never the request's untrusted status.
        status_code = attempt.status
        page = STATUS_PAGES[status_code]
        if transitioned:
            supplier_callback_url = _external_supplier_result_url(attempt, attempt.status)
            if supplier_callback_url:
                return HttpResponseRedirect(supplier_callback_url)
        clean_query = urlencode({"status": attempt.status, "pid": attempt.pid})
        return HttpResponseRedirect(f"{reverse('survey-status')}?{clean_query}")
    else:
        status_label = "Unknown attempt"

    page, status_label = _status_presentation_for_attempt(attempt, page, status_label)

    return render(request, "surveys/status.html", {
        **page,
        "status_label": status_label,
        "pid": attempt.pid if attempt else callback_identifier,
        "ip_address": ip_address,
        "loi_seconds": attempt.loi_seconds if attempt else None,
        "attempt_found": bool(attempt),
    }, status=200 if attempt else 404)


@extend_schema_view(
    list=extend_schema(
        tags=["Canonical qualification mappings"],
        summary="List stable internal qualification keys",
        description=(
            "Provider-neutral question and answer keys. External supplier integrations should use these "
            "keys instead of hard-coding InnovateMR, RFG or Cint IDs."
        ),
    ),
    retrieve=extend_schema(
        tags=["Canonical qualification mappings"],
        summary="Get one stable qualification definition",
    ),
)
class CanonicalQuestionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = CanonicalQuestion.objects.filter(is_active=True).prefetch_related("options")
    serializer_class = CanonicalQuestionSerializer
    lookup_field = "code"
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["code", "label", "description"]
    ordering_fields = ["code", "label", "updated_at"]
    ordering = ["code"]


@extend_schema_view(
    list=extend_schema(
        tags=["Canonical qualification mappings"],
        summary="List provider-to-platform question mappings",
        description=(
            "Shows how each provider's country/language-specific question IDs and answer precodes map "
            "to stable platform keys. Filter with provider_code, country_code, language_code or canonical_key."
        ),
        parameters=[
            OpenApiParameter("provider_code", OpenApiTypes.STR),
            OpenApiParameter("country_code", OpenApiTypes.STR),
            OpenApiParameter("language_code", OpenApiTypes.STR),
            OpenApiParameter("country_language_id", OpenApiTypes.STR),
            OpenApiParameter("canonical_key", OpenApiTypes.STR),
        ],
    ),
)
class ProviderQuestionMappingViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ProviderQuestionMapping.objects.filter(is_active=True).select_related(
        "canonical_question"
    ).prefetch_related("option_mappings__canonical_option")
    serializer_class = ProviderQuestionMappingSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        "external_question_id", "external_question_key", "canonical_question__code",
        "canonical_question__label",
    ]
    ordering_fields = ["provider_code", "country_code", "language_code", "external_question_id"]
    ordering = ["provider_code", "country_code", "language_code", "external_question_id"]

    def get_queryset(self):
        queryset = super().get_queryset()
        exact_filters = {
            "provider_code": "provider_code__iexact",
            "country_code": "country_code__iexact",
            "language_code": "language_code__iexact",
            "country_language_id": "country_language_id",
            "canonical_key": "canonical_question__code",
        }
        for parameter, lookup in exact_filters.items():
            value = self.request.query_params.get(parameter)
            if value not in (None, ""):
                queryset = queryset.filter(**{lookup: value})
        return queryset


class SurveySearchFilter(filters.SearchFilter):
    """Search only project attributes the requesting account may receive."""

    def get_search_fields(self, view, request):
        codes = effective_permission_codes(request.user)
        search_fields = []
        if "projects.column.project_id" in codes or "projects.column.actions" in codes:
            search_fields.append("local_id")
        if "projects.column.survey" in codes:
            search_fields.extend([
                "=source_key", "=source_id", "name", "buyer_id", "survey_type",
            ])
        if "projects.column.client_name" in codes:
            search_fields.append("company_name")
        if "projects.column.market" in codes:
            search_fields.extend(["country", "country_code"])
        if "projects.column.loi_ir" in codes:
            search_fields.extend(["group_type", "job_category"])
        return search_fields

    def filter_queryset(self, request, queryset, view):
        terms = self.get_search_terms(request)
        if len(terms) == 1:
            term = str(terms[0]).strip()
            codes = effective_permission_codes(request.user)
            exact_query = Q(pk__isnull=True)
            if "projects.column.project_id" in codes or "projects.column.actions" in codes:
                exact_query |= Q(local_id=term)
            if "projects.column.survey" in codes:
                exact_query |= Q(source_key=term) | Q(buyer_id=term)
                if term.isdigit() and len(term) <= 18:
                    exact_query |= Q(source_id=int(term))
            exact_queryset = queryset.filter(exact_query)
            if exact_queryset.exists():
                return exact_queryset
        return super().filter_queryset(request, queryset, view)

PROJECT_ORDERING_PERMISSIONS = {
    "source_modified_at": "projects.column.modified",
    "source_created_at": "projects.column.modified",
    "created_at": "projects.column.modified",
    "cpi": "projects.filter.cpi",
    "sample_size": "projects.column.completes",
    "completes": "projects.column.completes",
}


def _enforce_project_ordering_permission(request):
    ordering = str(
        request.query_params.get("ordering") or ""
    ).strip()

    if not ordering:
        return

    fields = {
        item.strip().lstrip("-")
        for item in ordering.split(",")
        if item.strip()
    }

    codes = effective_permission_codes(request.user)

    for field in fields:
        permission = PROJECT_ORDERING_PERMISSIONS.get(field)

        if (
            permission
            and not request.user.is_superuser
            and permission not in codes
        ):
            raise PermissionDenied(
                f"Your account cannot order projects by {field}."
            )
@extend_schema_view(
    list=extend_schema(
        tags=["Surveys"],
        summary="List synchronized surveys",
        description="Returns locally stored surveys using the requesting user's access scope.",
    ),
    retrieve=extend_schema(
        tags=["Surveys"],
        summary="Get one survey",
        description="Returns one project with normalized quota and targeting details.",
    ),
)
class SurveyViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Survey.objects.select_related("client", "integration").all()
    project_count_cache_enabled = True
    lookup_field = "local_id"
    filterset_class = SurveyFilter
    filter_backends = [SparseDjangoFilterBackend, SurveySearchFilter, filters.OrderingFilter]
    search_fields = ["local_id", "=source_key", "=source_id", "name", "company_name", "buyer_id", "survey_type", "country", "country_code", "job_category"]
    ordering_fields = ["source_modified_at", "source_created_at", "cpi", "sample_size", "completes", "created_at"]
    ordering = ["-source_modified_at", "-created_at"]
    permission_classes = [HasFunctionPermission]

    def get_queryset(self):
        queryset = scope_surveys_for_user(super().get_queryset(), self.request.user)
        queryset = scope_surveys_for_api_key(queryset, self.request.auth)

        # Viewer-visible CPI must be calculated in SQL only when a request
        # filters or sorts by it. Ordinary list rows are priced by the existing
        # serializer using prefetched allocation data, avoiding two correlated
        # pricing subqueries in both the page query and its pagination count.
        cpi_ordering = self.request.query_params.get("ordering", "").lstrip("-") == "cpi"
        cpi_filtering = any(
            self.request.query_params.get(name) not in {None, ""}
            for name in ("min_cpi", "max_cpi")
        )
        if self.action in {"retrieve", "export"} or cpi_ordering or cpi_filtering:
            queryset = annotate_survey_pricing_for_user(queryset, self.request.user)

        # A list page needs completes for only the rows it returns. Detail and
        # export paths keep the correlated annotation because they consume the
        # queryset outside the paginated list handler.
        if self.action in {"retrieve", "export"}:
            completed_attempts = (
                SurveyAttempt.objects.filter(
                    survey_id=OuterRef("pk"),
                    status=SurveyAttempt.Status.COMPLETED,
                )
                .values("survey_id")
                .annotate(total=Count("pk"))
                .values("total")[:1]
            )
            queryset = queryset.annotate(
                platform_completes=Coalesce(
                    Subquery(completed_attempts, output_field=IntegerField()),
                    Value(0),
                )
            )
        if self.action == "retrieve":
            queryset = queryset.prefetch_related("quotas", "targeting_questions")
        return queryset

    @staticmethod
    def _attach_page_platform_completes(surveys):
        """Attach completes using one grouped query for the current page."""

        survey_ids = [survey.pk for survey in surveys]
        if not survey_ids:
            return
        totals = dict(
            SurveyAttempt.objects.filter(
                survey_id__in=survey_ids,
                status=SurveyAttempt.Status.COMPLETED,
            )
            .values("survey_id")
            .annotate(total=Count("pk"))
            .values_list("survey_id", "total")
        )
        for survey in surveys:
            survey.platform_completes = totals.get(survey.pk, 0)

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            self._attach_page_platform_completes(page)
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        rows = list(queryset)
        self._attach_page_platform_completes(rows)
        serializer = self.get_serializer(rows, many=True)
        return Response(serializer.data)

    def get_required_function_permission(self):
        if self.action == "export":
            return "projects.export"
        return "survey_details.view" if self.action in {"retrieve", "quotas", "targeting", "details"} else "projects.view"

    @extend_schema(
        tags=["Surveys"],
        summary="Issue a fresh secure respondent entry link",
        description=(
            "Exchanges an existing authenticated entry token for a newly encrypted "
            "link after rechecking the account, project scope and copy permission."
        ),
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT},
    )
    @action(detail=False, methods=["post"], url_path="entry-link")
    def entry_link(self, request):
        if not has_function_access(request.user, "survey_links.copy"):
            raise PermissionDenied("Your account cannot copy survey links.")

        token = str(request.data.get("entry") or "").strip()
        try:
            entry = decode_entry_token(token)
        except EntryTokenError:
            return Response({"detail": "Invalid survey entry token."}, status=status.HTTP_400_BAD_REQUEST)

        request_api_key = request.auth if isinstance(request.auth, VendorAPIKey) else None
        expected_api_key_id = request_api_key.pk if request_api_key else None
        if (
            int(entry["user_id"]) != request.user.pk
            or entry.get("api_key_id") != expected_api_key_id
        ):
            raise PermissionDenied("This survey entry token belongs to a different account.")

        survey = self.get_queryset().filter(
            pk=int(entry["survey_id"]),
            status=Survey.Status.LIVE,
        ).first()
        if survey is None:
            return Response(
                {"detail": "This survey is not available to your account."},
                status=status.HTTP_404_NOT_FOUND,
            )

        start_link = SurveyListSerializer(context={"request": request}).get_start_link(survey)
        if not start_link:
            return Response(
                {"detail": "This survey entry link is not ready."},
                status=status.HTTP_409_CONFLICT,
            )
        response = Response({"start_link": start_link})
        response["Cache-Control"] = "no-store"
        return response

    def filter_queryset(self, queryset):
        _enforce_query_permissions(self.request, {
            "projects.filter.search": ("search",),
            "projects.filter.country": ("country",),
            "projects.filter.status": ("status",),
            "projects.filter.client": ("company", "client_name"),
            "projects.filter.buyer": ("buyer_id",),
            "projects.filter.survey_type": ("survey_type",),
            "projects.filter.date": ("created_from", "created_to", "modified_from", "modified_to"),
        })
        _enforce_project_ordering_permission(self.request)
        cpi_ordering = self.request.query_params.get("ordering", "").lstrip("-") == "cpi"
        cpi_filtering = any(self.request.query_params.get(name) not in {None, ""} for name in ("min_cpi", "max_cpi"))
        if (cpi_ordering or cpi_filtering) and not has_function_access(self.request.user, "projects.filter.cpi"):
            raise PermissionDenied("Your account cannot filter or sort projects by CPI.")
        queryset = super().filter_queryset(queryset)
        if cpi_ordering:
            direction = "-" if self.request.query_params.get("ordering", "").startswith("-") else ""
            queryset = queryset.order_by(
                f"{direction}visible_cpi",
                "-source_modified_at",
                "-created_at",
            )
        return queryset

    def get_serializer_class(self):
        return SurveyDetailSerializer if self.action == "retrieve" else SurveyListSerializer

    @extend_schema(
        tags=["Surveys"],
        summary="Export all filtered projects",
        description=(
            "Downloads an Excel workbook containing every survey matching the current Projects filters and "
            "ordering. Pagination is ignored and columns follow the requesting user's project permissions."
        ),
        parameters=[
            OpenApiParameter("search", OpenApiTypes.STR, description="Search project ID, survey ID, name, country or category."),
            OpenApiParameter("country", OpenApiTypes.STR, description="Comma-separated country codes."),
            OpenApiParameter("status", OpenApiTypes.STR, description="Comma-separated survey statuses."),
            OpenApiParameter("company", OpenApiTypes.STR, description="Comma-separated client/company names."),
            OpenApiParameter("buyer_id", OpenApiTypes.STR, description="Comma-separated buyer/sub-client IDs."),
            OpenApiParameter("survey_type", OpenApiTypes.STR, description="Comma-separated normalized audience types, for example B2B,B2C."),
            OpenApiParameter("created_from", OpenApiTypes.DATETIME, description="Source-created timestamp lower bound."),
            OpenApiParameter("created_to", OpenApiTypes.DATETIME, description="Source-created timestamp upper bound."),
            OpenApiParameter("modified_from", OpenApiTypes.DATETIME, description="Source-modified timestamp lower bound."),
            OpenApiParameter("modified_to", OpenApiTypes.DATETIME, description="Source-modified timestamp upper bound."),
            OpenApiParameter("min_cpi", OpenApiTypes.NUMBER, description="Minimum viewer-visible CPI after configured cuts, inclusive."),
            OpenApiParameter("max_cpi", OpenApiTypes.NUMBER, description="Maximum viewer-visible CPI after configured cuts, inclusive."),
            OpenApiParameter("ordering", OpenApiTypes.STR, description="Current Projects ordering, including viewer-visible cpi or -cpi."),
        ],
        responses={(200, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"): OpenApiTypes.BINARY},
    )
    @action(detail=False, methods=["get"], url_path="export")
    def export(self, request, *args, **kwargs):
        if not has_function_access(request.user, "projects.view"):
            raise PermissionDenied("Project visibility is required before projects can be exported.")
        queryset = self.filter_queryset(self.get_queryset())
        columns = [column for column in _project_columns_for_user(request.user) if column != "actions"]
        local_now = timezone.localtime()
        headers, rows, widths = _survey_excel_rows(queryset, request, columns)
        return build_excel_response(
            f"projects-{local_now:%Y%m%d-%H%M%S}-IST.xlsx",
            [ExcelSheet("Projects", headers, rows, widths)],
        )

    @staticmethod
    def _refresh_if_stale(survey, detail_type):
        synced_at = survey.quota_synced_at if detail_type == "quotas" else survey.targeting_synced_at
        stale = synced_at is None or (
            survey.source_modified_at is not None and synced_at < survey.source_modified_at
        )
        if (
            survey.integration_id
            and survey.integration.provider_code == "cint"
            and synced_at is not None
            and synced_at < timezone.now() - timedelta(seconds=60)
        ):
            stale = True
        if survey.integration_id and survey.integration.provider_code == "biobrain":
            if detail_type == "targeting":
                stale = stale or any(
                    not question.text
                    or str(question.text).startswith("Qualification ")
                    or bool(re.fullmatch(r"Q\d+", str(question.key or ""), re.IGNORECASE))
                    or (question.raw_data or {}).get("metadata_hydrated") is not True
                    or any(not isinstance(option, dict) for option in (question.options or []))
                    for question in survey.targeting_questions.all()
                )
            else:
                stale = stale or any(
                    not isinstance((quota.raw_data or {}).get("targeting_details"), list)
                    or (quota.raw_data or {}).get("metadata_hydrated") is not True
                    for quota in survey.quotas.all()
                )
        if stale:
            if survey.integration_id and has_provider(survey.integration.provider_code):
                get_provider(survey.integration).refresh_details(survey)
            else:
                refresh = replace_survey_quotas if detail_type == "quotas" else replace_survey_targeting
                refresh(InnovateMRClient(integration=survey.integration), survey)

    @extend_schema(
        tags=["Survey details"],
        summary="List a survey's quotas",
        description="Returns the most recently synchronized, provider-normalized quota data for this survey.",
        responses={200: SurveyQuotaSerializer(many=True)},
    )
    @action(detail=True, methods=["get"])
    def quotas(self, request, local_id=None):
        survey = self.get_object()
        try:
            self._refresh_if_stale(survey, "quotas")
        except (InnovateMRAPIError, ProviderError) as exc:
            if survey.quota_synced_at is None and not survey.quotas.exists():
                raise UpstreamUnavailable(str(exc)) from exc
        questions = list(survey.targeting_questions.all())
        return Response(SurveyQuotaSerializer(
            survey.quotas.all(), many=True,
            context={"targeting_questions": questions},
        ).data)

    @extend_schema(
        tags=["Survey details"],
        summary="List pre-screening questions and accepted answers",
        description="Returns provider-normalized pre-screening questions. Answer codes preserve the upstream provider mapping.",
        responses={200: TargetingQuestionSerializer(many=True)},
    )
    @action(detail=True, methods=["get"], url_path="targeting")
    def targeting(self, request, local_id=None):
        survey = self.get_object()
        try:
            self._refresh_if_stale(survey, "targeting")
        except (InnovateMRAPIError, ProviderError) as exc:
            if survey.targeting_synced_at is None and not survey.targeting_questions.exists():
                raise UpstreamUnavailable(str(exc)) from exc
        return Response(TargetingQuestionSerializer(survey.targeting_questions.all(), many=True).data)

    @extend_schema(
        tags=["Survey details"],
        summary="Get a survey's targeting and quotas together",
        description="Returns the same normalized detail payloads used by the two drawer tabs in one request.",
        responses={200: OpenApiTypes.OBJECT},
    )
    @action(detail=True, methods=["get"], url_path="details")
    def details(self, request, local_id=None):
        survey = self.get_object()
        detail_errors = {}
        for detail_type, relation_name, synced_field in (
            ("targeting", "targeting_questions", "targeting_synced_at"),
            ("quotas", "quotas", "quota_synced_at"),
        ):
            try:
                self._refresh_if_stale(survey, detail_type)
            except (InnovateMRAPIError, ProviderError) as exc:
                relation = getattr(survey, relation_name)
                if getattr(survey, synced_field) is None and not relation.exists():
                    # The former two-request drawer allowed one tab to remain
                    # usable when only the other upstream payload failed. Keep
                    # that partial-success behavior in the combined response.
                    detail_errors[detail_type] = str(exc)
            survey.refresh_from_db(fields=["quota_synced_at", "targeting_synced_at"])

        questions = list(survey.targeting_questions.all())
        quotas = list(survey.quotas.all())
        return Response({
            "targeting": TargetingQuestionSerializer(questions, many=True).data,
            "quotas": SurveyQuotaSerializer(
                quotas, many=True, context={"targeting_questions": questions}
            ).data,
            "errors": detail_errors,
        })


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
    filter_backends = [SparseDjangoFilterBackend, filters.OrderingFilter]
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

class SurveyAttemptSearchFilter(filters.SearchFilter):
    """Use indexed exact lookups for tracking identifiers before broad search.

    The existing substring search remains the fallback for names, emails,
    browsers and partial identifiers. Exact RID/PID/UID/IP/source-ID searches
    avoid a multi-column ``LIKE %term%`` scan when an authoritative match is
    already present inside the viewer's filtered queryset.
    """

    def filter_queryset(self, request, queryset, view):
        terms = self.get_search_terms(request)
        if len(terms) == 1:
            term = str(terms[0]).strip()
            exact_query = (
                Q(rid=term)
                | Q(pid=term)
                | Q(prescreener_uid=term)
                | Q(provider_profile_uid=term)
            )
            try:
                ipaddress.ip_address(term)
            except ValueError:
                pass
            else:
                exact_query |= Q(initiation_ip=term) | Q(callback_ip=term)
            if term.isdigit():
                exact_query |= Q(survey__source_key=term)
                # ``source_key`` is the canonical identifier. Keep the legacy
                # numeric column in the fast path where it is safely bounded.
                if len(term) <= 18:
                    exact_query |= Q(survey__source_id=int(term))

            exact_queryset = queryset.filter(exact_query)
            if exact_queryset.exists():
                return exact_queryset
        return super().filter_queryset(request, queryset, view)


@extend_schema_view(
    list=extend_schema(
        tags=["Survey attempts"],
        summary="List respondent survey attempts",
        description=(
            "Staff-only audit data for initiated pre-screeners, redirects, callbacks, IPs, measured LOI, "
            "survey country and the CPI snapshot frozen when the respondent entered."
        ),
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
    filter_backends = [SparseDjangoFilterBackend, SurveyAttemptSearchFilter, filters.OrderingFilter]
    filterset_class = SurveyAttemptFilter
    search_fields = [
        "rid", "pid", "prescreener_uid", "user_id", "survey__local_id", "=survey__source_key", "=survey__source_id", "survey__name", "survey__company_name",
        "platform_user__username", "platform_user__first_name", "platform_user__last_name", "platform_user__email",
        "initiation_ip", "callback_ip", "entry_browser", "entry_device", "entry_os",
    ]
    ordering_fields = ["initiated_at", "callback_at", "loi_seconds", "status"]
    ordering = ["-initiated_at"]

    def _filtered_summary(self, queryset):
        completed_filter = Q(status=SurveyAttempt.Status.COMPLETED)
        survey_termination_filter = Q(status=SurveyAttempt.Status.TERMINATED) & ~Q(
            status_source="local_prescreener"
        )
        summary = queryset.aggregate(
            total=Count("id"),
            initiated=Count("id", filter=Q(status__in=[SurveyAttempt.Status.INITIATED, SurveyAttempt.Status.REDIRECTED])),
            completed=Count("id", filter=completed_filter),
            terminated=Count("id", filter=Q(status=SurveyAttempt.Status.TERMINATED)),
            survey_terminated=Count("id", filter=survey_termination_filter),
            over_quota=Count("id", filter=Q(status=SurveyAttempt.Status.OVER_QUOTA)),
            security_terminated=Count("id", filter=Q(status=SurveyAttempt.Status.QUALITY_TERMINATED)),
            desktop=Count("id", filter=completed_filter & Q(entry_device__icontains="desktop")),
            mobile=Count("id", filter=completed_filter & (Q(entry_device__icontains="mobile") | Q(entry_device__icontains="phone"))),
            tablet=Count("id", filter=completed_filter & (Q(entry_device__icontains="tablet") | Q(entry_device__iexact="tab"))),
            total_revenue=Sum("source_cpi_snapshot", filter=completed_filter, default=Decimal("0.00")),
            supplier_revenue=Sum(
                Coalesce("payable_cpi_snapshot", "source_cpi_snapshot"),
                filter=completed_filter,
                default=Decimal("0.00"),
            ),
            revenue_currency=Max("cpi_currency_snapshot", filter=completed_filter),
        )
        completed = summary["completed"]
        ir_denominator = completed + summary["survey_terminated"]
        classified = summary["desktop"] + summary["mobile"] + summary["tablet"]
        if not can_view_report_commercials(self.request.user):
            revenue = (
                summary["supplier_revenue"]
                if is_external_vendor_scope(self.request.user)
                else summary["total_revenue"]
            )
            summary["total_revenue"] = apply_percentage(
                revenue,
                role_visibility_percent(self.request.user),
            )
        card_access = _component_access(
            effective_permission_codes(self.request.user), STUDY_CARD_PERMISSIONS
        )
        visible = lambda card, value: value if card_access[card] else None
        response_summary = {
            "total": visible("total", summary["total"]),
            "initiated": visible("initiated", summary["initiated"]),
            "completed": visible("completed", completed),
            "terminated": visible("terminated", summary["terminated"]),
            "over_quota": visible("quota", summary["over_quota"]),
            "security_terminated": visible("security", summary["security_terminated"]),
            "conversion_rate": visible(
                "conversion",
                round((completed / summary["total"] * 100), 2) if summary["total"] else 0.0,
            ),
            "incidence_rate": visible(
                "ir", round((completed / ir_denominator * 100), 2) if ir_denominator else 0.0,
            ),
            "total_revenue": visible("revenue", summary["total_revenue"]),
            "revenue_currency": visible(
                "revenue", summary["revenue_currency"] or "USD"
            ),
            "completed_devices": {
                "desktop": visible("desktop", summary["desktop"]),
                "mobile": visible("mobile", summary["mobile"]),
                "tablet": visible("tablet", summary["tablet"]),
                "unclassified": max(0, completed - classified),
            },
        }
        return response_summary, int(summary["total"])

    @extend_schema(tags=["Survey attempts"], summary="List visible survey attempts with filter-aware totals", responses={200: SurveyAttemptListResponseSerializer})
    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        include_summary = str(request.query_params.get("include_summary", "true")).lower() not in {
            "0", "false", "no",
        }
        if not include_summary:
            # Retain the row-only response for existing API consumers. The
            # Traffic page uses the default combined response so its aggregate
            # count can also seed pagination below.
            page = self.paginate_queryset(queryset)
            if page is not None:
                return self.get_paginated_response(
                    self.get_serializer(page, many=True).data
                )
            return Response({
                "count": queryset.count(),
                "next": None,
                "previous": None,
                "results": self.get_serializer(queryset, many=True).data,
            })

        summary, total_count = cached_report_payload(
            "traffic-summary",
            request,
            lambda: self._filtered_summary(queryset),
        )
        # Pagination membership stays authoritative even while KPI aggregates
        # use a short stale-while-revalidate cache. Reusing the cached summary
        # count could hide a newly created next page or expose a phantom one.
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            response = self.get_paginated_response(serializer.data)
            response.data["summary"] = summary
            return response
        return Response({"count": total_count, "next": None, "previous": None, "results": self.get_serializer(queryset, many=True).data, "summary": summary})

    @extend_schema(
        tags=["Survey attempts"],
        summary="Get filter-aware Traffic Report KPI totals",
        description="Returns the same permission-aware summary used by the Traffic Report cards without serializing page rows.",
    )
    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        summary, total_count = cached_report_payload(
            "traffic-summary",
            request,
            lambda: self._filtered_summary(queryset),
        )
        return Response({"count": total_count, "summary": summary})

    def get_required_function_permission(self):
        return "attempts.export" if self.action == "export" else "attempts.view"

    def get_serializer_class(self):
        if self.action == "list":
            return SurveyAttemptListSerializer
        return SurveyAttemptSerializer

    def get_queryset(self):
        if getattr(self, "action", None) == "list":
            permission_codes = effective_permission_codes(self.request.user)
            selected_fields = [
                "id", "survey_id", "platform_user_id", "client_id",
                "rid", "pid", "prescreener_uid", "provider_profile_uid", "user_id",
                "source_cpi_snapshot", "cpi_snapshot_source", "cpi_cut_percent_snapshot",
                "payable_cpi_snapshot", "cpi_currency_snapshot",
                "status", "initiated_at", "callback_at", "loi_seconds",
                "initiation_ip", "callback_ip", "entry_device",
                "survey__id", "survey__client_id", "survey__integration_id",
                "survey__local_id", "survey__source_id", "survey__source_key",
                "survey__company_name", "survey__country", "survey__country_code",
                "survey__buyer_id", "survey__cpi",
                "survey__client__id", "survey__client__name",
                "survey__integration__id", "survey__integration__provider_code",
                "survey__integration__config", "survey__integration__field_mapping",
                "platform_user__id", "platform_user__username", "platform_user__first_name",
                "platform_user__last_name", "platform_user__email",
                "client__id", "client__name",
            ]
            if STUDY_STATUS_SOURCE_PERMISSION in permission_codes:
                selected_fields.append("status_source")
            if STUDY_PROVIDER_STATUS_PERMISSION in permission_codes:
                selected_fields.extend([
                    "upstream_transaction_data", "is_verified", "exit_client_data",
                ])
            queryset = SurveyAttempt.objects.select_related(
                "survey", "survey__client", "survey__integration", "platform_user", "client",
            ).only(*selected_fields)
        else:
            queryset = SurveyAttempt.objects.select_related(
                "survey", "survey__client", "survey__integration", "platform_user", "platform_user__employee_profile", "platform_user__employee_profile__role",
                "platform_user__employee_profile__organization_unit", "platform_user__employee_profile__organization_unit__parent",
                "platform_user__employee_profile__organization_unit__parent__parent",
                "vendor", "vendor__employee_profile", "client", "client_allocation", "survey_allocation",
            ).all()
        if self.request.user.is_superuser:
            return queryset
        return _scope_attempt_queryset_to_user(queryset, self.request.user)

    def filter_queryset(self, queryset):
        _enforce_query_permissions(self.request, {
            "studies.filter.search": ("search",),
            "studies.filter.branch": ("branch",),
            "studies.filter.sub_branch": ("sub_branch",),
            "studies.filter.shift": ("shift",),
            "studies.filter.user": ("user",),
            "studies.filter.supplier": ("supplier",),
            "studies.filter.status": ("status",),
            "studies.filter.country": ("country",),
            "studies.filter.client": ("client",),
            "studies.filter.buyer": ("buyer_id",),
            "studies.filter.project": ("internal_id",),
            "studies.filter.date": ("initiated_from", "initiated_to", "callback_from", "callback_to"),
        })
        return super().filter_queryset(queryset)

    @extend_schema(
        tags=["Survey attempts"],
        summary="Export all filtered survey attempt data",
        description=(
            "Downloads the agreed Traffic Reports Excel columns for every filtered attempt, including immutable "
            "hit-time CPI, supplier CPI, respondent device/network audit and lifecycle timestamps."
        ),
        parameters=[
            OpenApiParameter("search", OpenApiTypes.STR, description="Search RID, user, survey, IP or client metadata."),
            OpenApiParameter("user", OpenApiTypes.STR, description="Comma-separated platform user IDs."),
            OpenApiParameter("supplier", OpenApiTypes.STR, description="Comma-separated external supplier user IDs."),
            OpenApiParameter("branch", OpenApiTypes.STR, description="Comma-separated organization Branch IDs or legacy labels."),
            OpenApiParameter("sub_branch", OpenApiTypes.STR, description="Comma-separated organization Sub-branch IDs or legacy labels."),
            OpenApiParameter("shift", OpenApiTypes.STR, description="Comma-separated organization Shift IDs or legacy labels."),
            OpenApiParameter("status", OpenApiTypes.STR, description="Comma-separated attempt status codes."),
            OpenApiParameter("country", OpenApiTypes.STR, description="Comma-separated survey country codes."),
            OpenApiParameter("company", OpenApiTypes.STR, description="Comma-separated survey company names."),
            OpenApiParameter("client", OpenApiTypes.STR, description="Comma-separated internal client IDs."),
            OpenApiParameter("buyer_id", OpenApiTypes.STR, description="Comma-separated buyer/sub-client IDs."),
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
        responses={(200, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"): OpenApiTypes.BINARY},
    )
    @action(detail=False, methods=["get"], url_path="export")
    def export(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        local_now = timezone.localtime()
        headers, rows, widths = _attempt_excel_rows(queryset, request.user)
        if not headers:
            raise PermissionDenied("No Traffic Report columns are assigned to your account.")
        return build_excel_response(
            f"traffic-reports-{local_now:%Y%m%d-%H%M%S}-IST.xlsx",
            [ExcelSheet("Traffic Reports", headers, rows, widths)],
        )


class DashboardAPIView(APIView):
    permission_classes = [HasFunctionPermission]
    required_function_permission = "dashboard.view"

    @extend_schema(
        tags=["Dashboard"],
        summary="Get permission-scoped dashboard analytics",
        description=(
            "Returns permission-scoped KPI totals, incidence rate, immutable hit-time CPI revenue, "
            "client completion share, performance, outcome/device breakdowns and top users."
        ),
        parameters=[
            OpenApiParameter(
                "range", OpenApiTypes.STR,
                description="Global analytics window: 24h, 48h, 72h, current month, 3m, 6m or financial year. Defaults to 24h.",
                enum=["24h", "48h", "72h", "month", "3m", "6m", "fy"],
            ),
            OpenApiParameter("financial_year", OpenApiTypes.INT, description="Starting year when range=fy, for example 2026 for 2026-27."),
            OpenApiParameter(
                "traffic_range", OpenApiTypes.STR,
                description="Independent Traffic graph window; does not change dashboard cards.",
                enum=["24h", "48h", "72h", "month", "3m", "6m", "fy"],
            ),
            OpenApiParameter("traffic_financial_year", OpenApiTypes.INT, description="Starting year when traffic_range=fy."),
            OpenApiParameter(
                "traffic_client", OpenApiTypes.INT,
                description="Visible internal client ID for the Traffic graph only.",
            ),
            OpenApiParameter(
                "finance_range", OpenApiTypes.STR,
                description="Independent Revenue/RPC graph window; does not change dashboard cards.",
                enum=["24h", "48h", "72h", "month", "3m", "6m", "fy"],
            ),
            OpenApiParameter("finance_financial_year", OpenApiTypes.INT, description="Starting year when finance_range=fy."),
            OpenApiParameter(
                "finance_client", OpenApiTypes.INT,
                description="Visible internal client ID for the Revenue/RPC graph only.",
            ),
        ],
        responses={200: DashboardResponseSerializer},
    )
    def get(self, request):
        codes = effective_permission_codes(request.user)
        if any(request.query_params.get(key) not in {None, ""} for key in (
            "traffic_range", "traffic_financial_year", "traffic_client"
        )) and DASHBOARD_GRAPH_FILTER_PERMISSIONS["traffic"] not in codes:
            raise PermissionDenied("Your account cannot filter the Traffic dashboard graph.")
        if any(request.query_params.get(key) not in {None, ""} for key in (
            "finance_range", "finance_financial_year", "finance_client"
        )) and DASHBOARD_GRAPH_FILTER_PERMISSIONS["finance"] not in codes:
            raise PermissionDenied("Your account cannot filter the Finance dashboard graph.")
        def load_dashboard():
            # Anchor all three windows to one instant. Equal range selections
            # then have exactly equal buckets and can safely reuse one series.
            dashboard_now = timezone.now()
            visible_queryset = dashboard_attempts(request.user, {})
            financial_years = dashboard_financial_year_options(
                visible_queryset, now=dashboard_now
            )
            available_financial_years = {
                item["start_year"] for item in financial_years
            }

            def selected_window(range_parameter, year_parameter, fallback=None):
                fallback_key = fallback.get("key") if isinstance(fallback, dict) else fallback
                range_key = request.query_params.get(range_parameter) or fallback_key or "24h"
                selected_year = request.query_params.get(year_parameter)
                if range_key == "fy":
                    if selected_year in {None, ""}:
                        selected_year = (
                            fallback.get("financial_year")
                            if isinstance(fallback, dict) and fallback.get("key") == "fy"
                            else financial_years[0]["start_year"]
                        )
                    try:
                        selected_year = int(selected_year)
                    except (TypeError, ValueError) as exc:
                        raise ValueError("Financial year must be a numeric starting year.") from exc
                    if selected_year not in available_financial_years:
                        raise ValueError("The selected financial year has no visible dashboard data.")
                return dashboard_range_window(
                    range_key,
                    now=dashboard_now,
                    financial_year=selected_year,
                )

            range_window = selected_window("range", "financial_year")
            traffic_window = selected_window(
                "traffic_range", "traffic_financial_year", fallback=range_window
            )
            finance_window = selected_window(
                "finance_range", "finance_financial_year", fallback=range_window
            )
            client_options = dashboard_client_options(visible_queryset)
            visible_client_ids = {item["id"] for item in client_options}

            def selected_client(parameter):
                raw_value = str(request.query_params.get(parameter) or "").strip()
                if not raw_value:
                    return None
                try:
                    client_id = int(raw_value)
                except ValueError as exc:
                    raise ValueError("Graph client must be a numeric client ID.") from exc
                if client_id not in visible_client_ids:
                    raise ValueError("The selected graph client is not visible to this account.")
                return client_id

            traffic_client_id = selected_client("traffic_client")
            finance_client_id = selected_client("finance_client")

            def graph_queryset(window, client_id=None):
                scoped = visible_queryset.filter(
                    initiated_at__gte=window["start"], initiated_at__lt=window["end"]
                )
                return scoped.filter(survey__client_id=client_id) if client_id else scoped

            queryset = graph_queryset(range_window)
            comparison_duration = range_window["end"] - range_window["start"]
            comparison_queryset = visible_queryset.filter(
                initiated_at__gte=range_window["start"] - comparison_duration,
                initiated_at__lt=range_window["start"],
            )
            traffic_queryset = graph_queryset(traffic_window, traffic_client_id)
            finance_queryset = graph_queryset(finance_window, finance_client_id)
            return build_dashboard_payload(
                queryset,
                request.user,
                _component_access(codes, DASHBOARD_CARD_PERMISSIONS),
                _component_access(codes, DASHBOARD_CHART_PERMISSIONS),
                range_window,
                traffic_queryset=traffic_queryset,
                traffic_range_window=traffic_window,
                traffic_client_id=traffic_client_id,
                finance_queryset=finance_queryset,
                finance_range_window=finance_window,
                finance_client_id=finance_client_id,
                client_options=client_options,
                comparison_queryset=comparison_queryset,
                financial_years=financial_years,
            )

        try:
            payload = cached_report_payload("dashboard-v2", request, load_dashboard)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload)


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
            OpenApiParameter("search", OpenApiTypes.STR, description="Search user, email, branch, sub-branch or shift."),
            OpenApiParameter("user", OpenApiTypes.STR, description="Comma-separated platform user IDs."),
            OpenApiParameter("branch", OpenApiTypes.STR, description="Comma-separated branch/company labels."),
            OpenApiParameter("sub_branch", OpenApiTypes.STR, description="Comma-separated sub-branch/department labels."),
            OpenApiParameter("shift", OpenApiTypes.STR, description="Comma-separated organization shift labels."),
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
        _enforce_query_permissions(request, {
            "user_hits.filter.search": ("search",),
            "user_hits.filter.user": ("user",),
            "user_hits.filter.branch": ("branch",),
            "user_hits.filter.sub_branch": ("sub_branch",),
            "user_hits.filter.shift": ("shift",),
            "user_hits.filter.supplier": ("supplier",),
            "user_hits.filter.date": ("from_date", "from_time", "to_date", "to_time"),
        })
        codes = effective_permission_codes(request.user)

        def load_user_hits():
            payload = aggregate_user_hit_payload(request.user, request.query_params)
            summary = payload["summary"]
            if USER_HIT_CARD_PERMISSIONS["total_hits"] not in codes:
                summary["hits"]["total"] = None
            if USER_HIT_CARD_PERMISSIONS["completes"] not in codes:
                summary["completes"]["total"] = None
            if USER_HIT_CARD_PERMISSIONS["conversion"] not in codes:
                summary["conversion_rate"] = None
            if USER_HIT_CARD_PERMISSIONS["active_users"] not in codes:
                summary["active_users"] = None
            if USER_HIT_CARD_PERMISSIONS["devices"] not in codes:
                for device in ("desktop", "mobile", "tablet", "unclassified"):
                    summary["completes"][device] = None
            if USER_HIT_CARD_PERMISSIONS["ir"] not in codes:
                summary["incidence_rate"] = None
            return payload

        try:
            payload = cached_report_payload(
                "user-hits-v5",
                request,
                load_user_hits,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        # Column permissions are a data boundary, not only a presentation hint.
        # Project the cached aggregate after loading it so hidden identities,
        # locations, dates and metrics never reach the browser response.
        permitted_columns = set(_permitted_columns(codes, USER_HIT_COLUMN_PERMISSIONS))
        fields_by_column = {
            "branch": ("branch",),
            "sub_branch": ("sub_branch",),
            "shift": ("shift",),
            "user": ("user_id", "user_name", "username", "user_email"),
            "date": ("date",),
            "hits": ("hits",),
            "completes": ("completes",),
        }
        visible_fields = {
            field
            for column in permitted_columns
            for field in fields_by_column.get(column, ())
        }
        paginator = SurveyPagination()
        compact_page = paginator.paginate_queryset(payload["rows"], request, view=self)
        page_rows = expand_user_hit_rows(compact_page, payload["metadata"])
        projected_rows = [
            {field: value for field, value in row.items() if field in visible_fields}
            for row in page_rows
        ]

        response = paginator.get_paginated_response(projected_rows)
        response.data["summary"] = payload["summary"]
        return response


class UserDashboardAPIView(APIView):
    permission_classes = [HasFunctionPermission]
    required_function_permission = "user_dashboard.view"

    @extend_schema(
        tags=["User dashboard"],
        summary="Monthly employee completion and final-ID performance",
        description=(
            "Returns one row per visible active employee for the selected IST month. "
            "Accepted and rejected counts come from the latest client Final ID decision; "
            "completed journeys without a decision remain pending."
        ),
        parameters=[
            OpenApiParameter("search", OpenApiTypes.STR, description="Search employee identity or hierarchy."),
            OpenApiParameter("user", OpenApiTypes.STR, description="Comma-separated platform user IDs."),
            OpenApiParameter("branch", OpenApiTypes.STR, description="Comma-separated branch IDs or labels."),
            OpenApiParameter("sub_branch", OpenApiTypes.STR, description="Comma-separated sub-branch IDs or labels."),
            OpenApiParameter("shift", OpenApiTypes.STR, description="Comma-separated shift IDs or labels."),
            OpenApiParameter("month", OpenApiTypes.INT, description="IST calendar month, 1-12."),
            OpenApiParameter("year", OpenApiTypes.INT, description="IST calendar year."),
            OpenApiParameter("page", OpenApiTypes.INT, description="1-based employee page."),
            OpenApiParameter("page_size", OpenApiTypes.INT, description="Rows per page, 1-100."),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        latest_upload_id = FinalIDUpload.objects.order_by("-id").values_list("id", flat=True).first()

        def load_dashboard():
            return build_user_dashboard_payload(request.user, request.query_params)

        try:
            payload = cached_report_payload(
                "user-dashboard-v1",
                request,
                load_dashboard,
                extra_scope={"latest_final_id_upload": latest_upload_id},
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        paginator = SurveyPagination()
        page_rows = paginator.paginate_queryset(payload["rows"], request, view=self)
        response = paginator.get_paginated_response(page_rows)
        response.data["summary"] = payload["summary"]
        response.data["period"] = payload["period"]
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


def _excel_datetime(value):
    if not value:
        return ""
    local_value = timezone.localtime(value) if timezone.is_aware(value) else value
    return local_value.strftime("%d %b %Y %I:%M:%S %p IST")


def _survey_excel_rows(queryset, request, columns):
    can_view_client_name = has_function_access(request.user, "projects.column.client_name")
    survey_headers = ["Survey ID", "Survey name"]
    survey_widths = [16, 32]
    if can_view_client_name:
        survey_headers.append("Client")
        survey_widths.append(21)
    survey_headers.append("Buyer ID")
    survey_widths.append(15)
    headers_by_column = {
        "project_id": ["Project ID"],
        "survey": survey_headers,
        "market": ["Country code", "Country", "Language code", "Language"],
        "completes": ["Sample size", "Completes", "Remaining", "Progress (%)"],
        "cpi": ["CPI"],
        "loi_ir": ["LOI (minutes)", "Incidence rate (%)", "Survey type"],
        "entry_link": ["Entry link"],
        "modified": ["Status", "Source created at", "Source modified at", "Record created at", "Record updated at"],
    }
    widths_by_column = {
        "project_id": [19], "survey": survey_widths, "market": [13, 20, 14, 18],
        "completes": [13, 12, 12, 14], "cpi": [11], "loi_ir": [15, 18, 14],
        "entry_link": [48], "modified": [14, 22, 22, 22, 22],
    }
    export_columns = [column for column in columns if column in headers_by_column]
    headers = [header for column in export_columns for header in headers_by_column[column]]
    widths = [width for column in export_columns for width in widths_by_column[column]]

    def rows():
        serializer_context = {"request": request}
        # Reuse one bound serializer for the whole stream. Constructing and
        # permission-pruning a DRF serializer for every project made large
        # Cint exports spend most of their time rebuilding identical fields;
        # representation, pricing, security and entry-link logic stay exactly
        # the same for every row.
        serializer = SurveyListSerializer(context=serializer_context)
        for survey in queryset.iterator(chunk_size=500):
            data = serializer.to_representation(survey)
            values_by_column = {
                "project_id": [data.get("local_id")],
                "survey": (
                    [data.get("source_id"), data.get("name")]
                    + ([data.get("client_name") or data.get("display_company_name") or data.get("company_name")] if can_view_client_name else [])
                    + [data.get("buyer_id")]
                ),
                "market": [data.get("country_code"), data.get("country"), data.get("language_code"), data.get("language")],
                "completes": [data.get("sample_size"), data.get("completes"), data.get("remaining"), data.get("progress_percent")],
                "cpi": [data.get("cpi")],
                "loi_ir": [data.get("loi"), data.get("incidence_rate"), data.get("survey_type") or data.get("group_type")],
                "entry_link": [data.get("start_link")],
                "modified": [
                    data.get("status"), data.get("source_created_at"), data.get("source_modified_at"),
                    data.get("created_at"), data.get("updated_at"),
                ],
            }
            yield [value for column in export_columns for value in values_by_column[column]]

    return headers, rows(), widths


def _attempt_excel_rows(queryset, requesting_user=None):
    """Build Traffic Report rows without leaking upstream commercial data.

    Platform admins receive the source CPI, computed supplier CPI and supplier
    identity. Scoped/cut users receive only their adjusted CPI in the two client
    CPI columns; supplier commercial columns do not exist in their workbook.
    """

    commercial_admin = can_view_report_commercials(requesting_user)
    can_view_client_name = has_function_access(
        requesting_user, STUDY_CLIENT_NAME_PERMISSION
    )
    can_view_provider_status = has_function_access(
        requesting_user, STUDY_PROVIDER_STATUS_PERMISSION
    )
    can_view_status_source = has_function_access(
        requesting_user, STUDY_STATUS_SOURCE_PERMISSION
    )
    permitted = set(_permitted_columns(
        effective_permission_codes(requesting_user), STUDY_COLUMN_PERMISSIONS
    ))
    specs = {
        "project_id": (
            ["Project id"] + (["Client name"] if can_view_client_name else []),
            [19] + ([21] if can_view_client_name else []),
        ),
        "survey_id": (["Cleint survey id"], [18]),
        "pid": (["PID"], [12]),
        "respondent_id": (["RID", "UID"], [14, 21]),
        "status": (
            ["Status"]
            + (["Provider status", "Term reason", "Term category"] if can_view_provider_status else [])
            + (["Status source"] if can_view_status_source else []),
            [19]
            + ([27, 44, 22] if can_view_provider_status else [])
            + ([18] if can_view_status_source else []),
        ),
        "country": (["Country"], [18]),
        "cpi": (
            ["Current Client CPI", "Client entry link CPI"]
            + (["Vendor CPI", "Vendor name"] if commercial_admin else []),
            [18, 20] + ([14, 20] if commercial_admin else []),
        ),
        "user": (["User name"], [22]),
        "device": (["Device", "OS", "Browser", "User agent"], [13, 16, 18, 42]),
        "ip": (["Entry IP", "Exit IP"], [16, 16]),
        "loi": (["Actual LOI (minutes)"], [19]),
        "start": (
            ["Inisitate at", "Presecreent at", "Redirect at", "entry date time"],
            [22, 22, 22, 22],
        ),
        "end": (["Exit date time"], [22]),
    }
    ordered_columns = [column for column in STUDY_COLUMN_PERMISSIONS if column in permitted]
    headers = [header for column in ordered_columns for header in specs[column][0]]
    widths = [width for column in ordered_columns for width in specs[column][1]]

    selected_fields = {"id"}
    selected_relations = set()
    if set(ordered_columns) & {"project_id", "survey_id", "country", "cpi"}:
        selected_relations.add("survey")
        selected_fields.update({"survey_id", "survey__id"})
    if "project_id" in ordered_columns:
        selected_fields.add("survey__local_id")
        if can_view_client_name:
            selected_relations.update({"client", "survey__client"})
            selected_fields.update({
                "client_id", "client__id", "client__name",
                "survey__client_id", "survey__client__id", "survey__client__name",
                "survey__company_name",
            })
    if "survey_id" in ordered_columns:
        selected_fields.update({"survey__source_id", "survey__source_key"})
    if "pid" in ordered_columns:
        selected_fields.add("pid")
    if "respondent_id" in ordered_columns:
        selected_fields.update({"rid", "prescreener_uid"})
    if "status" in ordered_columns:
        selected_fields.add("status")
        if can_view_provider_status:
            selected_relations.update({"survey", "survey__integration"})
            selected_fields.update({
                "rid", "survey_id", "survey__id", "survey__integration_id",
                "survey__integration__id", "survey__integration__provider_code",
                "survey__integration__config", "survey__integration__field_mapping",
                "upstream_transaction_data", "exit_client_data", "is_verified",
            })
        if can_view_status_source:
            selected_fields.add("status_source")
    if "country" in ordered_columns:
        selected_fields.update({"survey__country", "survey__country_code"})
    if "cpi" in ordered_columns:
        selected_fields.update({
            "source_cpi_snapshot", "payable_cpi_snapshot",
            "cpi_cut_percent_snapshot", "survey__cpi",
        })
        if commercial_admin:
            selected_relations.update({
                "platform_user", "platform_user__employee_profile",
                "platform_user__employee_profile__role",
                "platform_user__employee_profile__organization_unit",
                "platform_user__employee_profile__organization_unit__parent",
                "platform_user__employee_profile__organization_unit__parent__parent",
                "vendor", "vendor__employee_profile",
            })
            selected_fields.update({
                "platform_user_id", "platform_user__id", "platform_user__username",
                "platform_user__first_name", "platform_user__last_name",
                "platform_user__employee_profile__id",
                "platform_user__employee_profile__user_id",
                "platform_user__employee_profile__account_type",
                "platform_user__employee_profile__role_id",
                "platform_user__employee_profile__role__id",
                "platform_user__employee_profile__role__cpi_visibility_percent",
                "platform_user__employee_profile__organization_unit_id",
                "platform_user__employee_profile__organization_unit__id",
                "platform_user__employee_profile__organization_unit__name",
                "platform_user__employee_profile__organization_unit__unit_type",
                "platform_user__employee_profile__organization_unit__parent_id",
                "platform_user__employee_profile__organization_unit__parent__id",
                "platform_user__employee_profile__organization_unit__parent__name",
                "platform_user__employee_profile__organization_unit__parent__unit_type",
                "platform_user__employee_profile__organization_unit__parent__parent_id",
                "platform_user__employee_profile__organization_unit__parent__parent__id",
                "platform_user__employee_profile__organization_unit__parent__parent__name",
                "platform_user__employee_profile__organization_unit__parent__parent__unit_type",
                "vendor_id", "vendor__id", "vendor__username",
                "vendor__first_name", "vendor__last_name",
                "vendor__employee_profile__id", "vendor__employee_profile__user_id",
                "vendor__employee_profile__account_type",
            })
    if "user" in ordered_columns:
        selected_relations.add("platform_user")
        selected_fields.update({
            "platform_user_id", "platform_user__id", "platform_user__username",
            "platform_user__first_name", "platform_user__last_name",
        })
    if "device" in ordered_columns:
        selected_fields.update({
            "entry_device", "entry_os", "entry_browser", "entry_user_agent",
        })
    if "ip" in ordered_columns:
        selected_fields.update({"initiation_ip", "callback_ip"})
    if "loi" in ordered_columns:
        selected_fields.add("loi_seconds")
    if "start" in ordered_columns:
        selected_fields.update({
            "initiated_at", "submitted_at", "redirected_at", "created_at",
        })
    if "end" in ordered_columns:
        selected_fields.update({"callback_at", "last_callback_at"})

    queryset = queryset.select_related(None)
    if selected_relations:
        queryset = queryset.select_related(*sorted(selected_relations))
    queryset = queryset.only(*sorted(selected_fields))

    def rows():
        for attempt in queryset.iterator(chunk_size=1000):
            values_by_column = {}
            if "project_id" in ordered_columns:
                values = [attempt.survey.local_id]
                if can_view_client_name:
                    client = attempt.client or attempt.survey.client
                    values.append(
                        client.name if client else attempt.survey.company_name
                    )
                values_by_column["project_id"] = values
            if "survey_id" in ordered_columns:
                values_by_column["survey_id"] = [attempt.survey.source_identifier]
            if "pid" in ordered_columns:
                values_by_column["pid"] = [attempt.pid]
            if "respondent_id" in ordered_columns:
                values_by_column["respondent_id"] = [
                    attempt.rid, attempt.prescreener_uid or "",
                ]
            if "status" in ordered_columns:
                status_label = (
                    "Initiated"
                    if attempt.status in {
                        SurveyAttempt.Status.INITIATED, SurveyAttempt.Status.REDIRECTED,
                    }
                    else attempt.get_status_display()
                )
                outcome = provider_outcome(attempt) if can_view_provider_status else {}
                values_by_column["status"] = (
                    [status_label]
                    + ([
                        outcome.get("status") or "",
                        outcome.get("reason") or "",
                        outcome.get("category") or "",
                    ] if can_view_provider_status else [])
                    + ([attempt.status_source] if can_view_status_source else [])
                )
            if "country" in ordered_columns:
                values_by_column["country"] = [
                    attempt.survey.country or attempt.survey.country_code
                ]
            if "cpi" in ordered_columns:
                values_by_column["cpi"] = [
                    viewer_attempt_cpi(attempt, requesting_user, current=True),
                    viewer_attempt_cpi(attempt, requesting_user),
                    *(
                        [supplier_cpi_for_admin(attempt), supplier_label_for_admin(attempt)]
                        if commercial_admin else []
                    ),
                ]
            if "user" in ordered_columns:
                user = attempt.platform_user
                values_by_column["user"] = [
                    (user.get_full_name() or user.username) if user else "Deleted user"
                ]
            if "device" in ordered_columns:
                values_by_column["device"] = [
                    attempt.entry_device, attempt.entry_os, attempt.entry_browser,
                    attempt.entry_user_agent,
                ]
            if "ip" in ordered_columns:
                values_by_column["ip"] = [attempt.initiation_ip, attempt.callback_ip]
            if "loi" in ordered_columns:
                values_by_column["loi"] = [round((attempt.loi_seconds or 0) / 60, 2)]
            if "start" in ordered_columns:
                values_by_column["start"] = [
                    _excel_datetime(attempt.initiated_at),
                    _excel_datetime(attempt.submitted_at),
                    _excel_datetime(attempt.redirected_at),
                    _excel_datetime(attempt.created_at),
                ]
            if "end" in ordered_columns:
                values_by_column["end"] = [
                    _excel_datetime(attempt.callback_at or attempt.last_callback_at)
                ]
            yield [value for column in ordered_columns for value in values_by_column[column]]

    return headers, rows(), widths


def _survey_csv_rows(queryset, request, columns):
    headers_by_column = {
        "project_id": ["Project ID"],
        "survey": ["Survey ID", "Survey name", "Client", "Buyer ID"],
        "market": ["Country code", "Country", "Language code", "Language"],
        "completes": ["Sample size", "Completes", "Remaining", "Progress (%)"],
        "cpi": ["CPI"],
        "loi_ir": ["LOI (minutes)", "Incidence rate (%)", "Survey type"],
        "entry_link": ["Entry link"],
        "modified": ["Status", "Source created at", "Source modified at", "Record created at", "Record updated at"],
    }
    export_columns = [column for column in columns if column in headers_by_column]
    headers = [header for column in export_columns for header in headers_by_column[column]]
    writer = csv.writer(_CsvEcho())
    yield "\ufeff" + writer.writerow(headers)
    serializer_context = {"request": request}
    for survey in queryset.iterator(chunk_size=500):
        data = SurveyListSerializer(survey, context=serializer_context).data
        values_by_column = {
            "project_id": [data.get("local_id")],
            "survey": [
                data.get("source_id"), data.get("name"),
                data.get("client_name") or data.get("display_company_name") or data.get("company_name"),
                data.get("buyer_id"),
            ],
            "market": [data.get("country_code"), data.get("country"), data.get("language_code"), data.get("language")],
            "completes": [data.get("sample_size"), data.get("completes"), data.get("remaining"), data.get("progress_percent")],
            "cpi": [data.get("cpi")],
            "loi_ir": [data.get("loi"), data.get("incidence_rate"), data.get("survey_type") or data.get("group_type")],
            "entry_link": [data.get("start_link")],
            "modified": [
                data.get("status"), data.get("source_created_at"), data.get("source_modified_at"),
                data.get("created_at"), data.get("updated_at"),
            ],
        }
        values = [value for column in export_columns for value in values_by_column[column]]
        yield writer.writerow([_csv_safe(value) for value in values])


def _attempt_csv_rows(queryset, requesting_user=None):
    headers = [
        "Respondent ID (RID)", "Status code", "Status", "Termination reason", "Termination category", "Status source", "Platform user ID", "Username", "Employee name",
        "Email", "Employee ID", "Account type", "Role", "Supplier ID", "Supplier name", "Supplier account type",
        "Client ID", "Client name", "Client allocation ID", "Survey allocation ID",
        "Internal project ID", "Survey ID", "Survey name", "Company", "Buyer ID", "Survey type", "Country", "Language", "Supplier code",
        "Current survey CPI", "Source CPI snapshot", "CPI snapshot source", "CPI cut snapshot (%)", "Payable CPI snapshot",
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
    hide_source_cpi = is_external_vendor_scope(requesting_user)
    requesting_profile = getattr(requesting_user, "employee_profile", None) if requesting_user else None
    requesting_role = getattr(requesting_profile, "role", None) if requesting_profile else None
    visible_percent = (
        requesting_role.cpi_visibility_percent
        if requesting_profile and requesting_profile.account_type == "employee" and requesting_role and not requesting_user.is_superuser
        else Decimal("100.00")
    )

    def visible_cpi(value):
        if hide_source_cpi or value is None:
            return ""
        return (Decimal(value) * visible_percent / Decimal("100.00")).quantize(Decimal("0.01"))

    for attempt in queryset.iterator(chunk_size=1000):
        outcome = provider_outcome(attempt) if attempt.status in {
            SurveyAttempt.Status.TERMINATED,
            SurveyAttempt.Status.OVER_QUOTA,
            SurveyAttempt.Status.QUALITY_TERMINATED,
        } else {"reason": "", "category": ""}
        user = attempt.platform_user
        profile = getattr(user, "employee_profile", None) if user else None
        role = getattr(profile, "role", None) if profile else None
        vendor = attempt.vendor
        vendor_profile = getattr(vendor, "employee_profile", None) if vendor else None
        survey = attempt.survey
        values = [
            attempt.rid, attempt.status,
            "Initiated" if attempt.status in {SurveyAttempt.Status.INITIATED, SurveyAttempt.Status.REDIRECTED} else attempt.get_status_display(),
            outcome["reason"], outcome["category"], attempt.status_source, user.pk if user else attempt.user_id,
            user.username if user else "", (user.get_full_name() or user.username) if user else "Deleted user",
            user.email if user else "", getattr(profile, "employee_id", ""),
            profile.get_account_type_display() if profile else "", role.name if role else "",
            vendor.pk if vendor else "", (vendor.get_full_name() or vendor.username) if vendor else "",
            vendor_profile.get_account_type_display() if vendor_profile else "",
            attempt.client_id, attempt.client.name if attempt.client else "", attempt.client_allocation_id,
            attempt.survey_allocation_id,
            survey.local_id, survey.source_identifier, survey.name, survey.company_name, survey.buyer_id, survey.survey_type or survey.group_type, survey.country_code,
            survey.language_code, attempt.supplier_code,
            visible_cpi(survey.cpi),
            visible_cpi(attempt.source_cpi_snapshot),
            attempt.cpi_snapshot_source, attempt.cpi_cut_percent_snapshot, attempt.payable_cpi_snapshot, attempt.cpi_currency_snapshot,
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
