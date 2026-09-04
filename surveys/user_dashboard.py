"""Hierarchy-scoped monthly user reconciliation performance."""

from __future__ import annotations

from datetime import datetime, timedelta

from django.db.models import Count, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from accounts.models import EmployeeProfile

from .models import FinalIDUpload, SurveyAttempt
from .user_hits import (
    _legacy_identifier_user_map,
    _user_metadata,
    _visible_user_ids,
)


def _csv_values(value: str) -> set[str]:
    return {item.strip() for item in (value or "").split(",") if item.strip()}


def _employee_metadata(user) -> dict[int, dict]:
    visible_ids = _visible_user_ids(user)
    employee_ids = set(
        EmployeeProfile.objects.filter(
            user_id__in=visible_ids,
            account_type=EmployeeProfile.AccountType.EMPLOYEE,
        ).values_list("user_id", flat=True)
    )
    metadata = _user_metadata(user, visible_ids)
    return {
        user_id: item
        for user_id, item in metadata.items()
        if user_id in employee_ids
    }


def user_dashboard_filter_options(user) -> dict:
    """Return live report dimensions available to employee performance."""

    users = [dict(item) for item in _employee_metadata(user).values()]
    users.sort(key=lambda item: (item["user_name"].casefold(), item["user_id"]))

    def option_value(item, level):
        return str(item.get(f"{level}_id") or item.get(level) or "")

    def unique_options(level, parent_levels=()):
        options = {}
        for item in users:
            name = item.get(level) or ""
            value = option_value(item, level)
            if not name or not value:
                continue
            option = {"value": value, "name": name}
            for parent_level in parent_levels:
                option[f"{parent_level}_value"] = option_value(item, parent_level)
            key = (
                value,
                *(option.get(f"{parent}_value", "") for parent in parent_levels),
            )
            options[key] = option
        return sorted(
            options.values(),
            key=lambda option: (option["name"].casefold(), option["value"]),
        )

    for item in users:
        item["branch_value"] = option_value(item, "branch")
        item["sub_branch_value"] = option_value(item, "sub_branch")

    employee_ids = [item["user_id"] for item in users]
    attempts = SurveyAttempt.objects.filter(platform_user_id__in=employee_ids)
    suppliers = []
    for row in (
        attempts.filter(vendor__isnull=False)
        .values(
            "vendor_id", "vendor__first_name", "vendor__last_name",
            "vendor__username", "vendor__email",
        )
        .distinct()
        .order_by("vendor__first_name", "vendor__last_name", "vendor__username")
    ):
        full_name = " ".join(
            part for part in (row["vendor__first_name"], row["vendor__last_name"])
            if part
        ).strip()
        suppliers.append({
            "value": str(row["vendor_id"]),
            "name": full_name or row["vendor__username"] or row["vendor__email"] or f"Supplier {row['vendor_id']}",
            "email": row["vendor__email"] or "",
        })

    return {
        "users": users,
        "branches": unique_options("branch"),
        "sub_branches": unique_options("sub_branch", ("branch",)),
        "suppliers": suppliers,
        "countries": list(
            attempts.exclude(survey__country_code="")
            .values("survey__country_code", "survey__country")
            .distinct()
            .order_by("survey__country_code")
        ),
        "clients": list(
            attempts.filter(survey__client__isnull=False)
            .values("survey__client_id", "survey__client__name")
            .distinct()
            .order_by("survey__client__name", "survey__client_id")
        ),
        "buyers": list(
            attempts.exclude(survey__buyer_id="")
            .values("survey__buyer_id", "survey__client_id")
            .distinct()
            .order_by("survey__buyer_id")
        ),
    }


def _report_datetime(value: str, label: str):
    parsed = parse_datetime(str(value or "").strip())
    if parsed is None:
        raise ValueError(f"{label} must be a valid date and time.")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _selected_period(params) -> tuple[int, int, datetime, datetime, str, str]:
    local_today = timezone.localdate()
    try:
        year = int(params.get("year") or local_today.year)
        month = int(params.get("month") or local_today.month)
    except (TypeError, ValueError) as exc:
        raise ValueError("Month and year must be numeric.") from exc
    if year < 2020 or year > local_today.year + 1:
        raise ValueError("Year is outside the supported reporting range.")
    if month < 1 or month > 12:
        raise ValueError("Month must be between 1 and 12.")

    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    current_timezone = timezone.get_current_timezone()
    lower = timezone.make_aware(datetime(year, month, 1), current_timezone)
    upper = timezone.make_aware(datetime(next_year, next_month, 1), current_timezone)
    date_field = str(params.get("date_field") or "initiated").strip().lower()
    if date_field not in {"initiated", "callback"}:
        raise ValueError("Date column must be entry date or exit date.")
    if params.get("from_datetime"):
        lower = _report_datetime(params.get("from_datetime"), "From date and time")
    if params.get("to_datetime"):
        upper = _report_datetime(params.get("to_datetime"), "To date and time")
    else:
        # The legacy month/year API treated the next month boundary as
        # exclusive. Preserve that contract while exact date-time controls
        # remain inclusive.
        upper -= timedelta(microseconds=1)
    if lower > upper:
        raise ValueError("From date and time cannot be after To date and time.")
    local_lower = timezone.localtime(lower)
    local_upper = timezone.localtime(upper)
    label = (
        f"{local_lower.strftime('%d %b %Y, %I:%M %p')} – "
        f"{local_upper.strftime('%d %b %Y, %I:%M %p')}"
    )
    return year, month, lower, upper, date_field, label


def _filter_attempt_dimensions(queryset, params):
    """Apply Traffic Report-style dimensions without altering lifecycle data."""

    exact_filters = {
        "supplier": "vendor_id",
        "country": "survey__country_code",
        "buyer_id": "survey__buyer_id",
    }
    for parameter, field_name in exact_filters.items():
        selected = _csv_values(params.get(parameter, ""))
        if selected:
            queryset = queryset.filter(**{f"{field_name}__in": selected})

    selected_clients = _csv_values(params.get("client", ""))
    if selected_clients:
        if any(not value.isdigit() for value in selected_clients):
            raise ValueError("Client filters must contain numeric IDs.")
        client_ids = {int(value) for value in selected_clients}
        queryset = queryset.filter(
            Q(survey__client_id__in=client_ids) | Q(client_id__in=client_ids)
        )

    selected_final_statuses = _csv_values(params.get("final_status", ""))
    allowed_final_statuses = {
        FinalIDUpload.Decision.ACCEPTED,
        FinalIDUpload.Decision.REJECTED,
        "pending",
    }
    if not selected_final_statuses <= allowed_final_statuses:
        raise ValueError("Final status contains an unsupported value.")
    if selected_final_statuses:
        status_query = Q(pk__in=[])
        decided = selected_final_statuses - {"pending"}
        if decided:
            status_query |= Q(final_id_status__status__in=decided)
        if "pending" in selected_final_statuses:
            status_query |= Q(final_id_status__isnull=True)
        queryset = queryset.filter(status_query)
    return queryset


def build_user_dashboard_payload(user, params) -> dict:
    """Aggregate one selected completion month into one row per employee."""

    metadata = _employee_metadata(user)
    visible_ids = set(metadata)

    selected_users = _csv_values(params.get("user", ""))
    if any(not value.isdigit() for value in selected_users):
        raise ValueError("User filters must contain numeric IDs.")
    if selected_users:
        visible_ids &= {int(value) for value in selected_users}

    def hierarchy_match(item, level, selected):
        return bool(
            str(item.get(f"{level}_id") or "") in selected
            or str(item.get(level) or "") in selected
        )

    for parameter, level in (
        ("branch", "branch"),
        ("sub_branch", "sub_branch"),
    ):
        selected = _csv_values(params.get(parameter, ""))
        if selected:
            visible_ids = {
                user_id
                for user_id in visible_ids
                if hierarchy_match(metadata.get(user_id, {}), level, selected)
            }

    search = str(params.get("search") or "").strip().casefold()
    if search:
        visible_ids = {
            user_id
            for user_id in visible_ids
            if any(
                search in str(metadata.get(user_id, {}).get(field, "")).casefold()
                for field in (
                    "user_name", "username", "user_email", "employee_id",
                    "branch", "sub_branch",
                )
            )
        }

    year, month, lower, upper, date_field, period_label = _selected_period(params)
    visible_metadata = {
        user_id: metadata[user_id]
        for user_id in visible_ids
        if user_id in metadata
    }
    legacy_user_map = _legacy_identifier_user_map(visible_metadata)

    aggregates = {
        user_id: {"completes": 0, "accepted": 0, "rejected": 0}
        for user_id in visible_ids
    }

    def merge_rows(queryset, identity_field, resolver):
        rows = queryset.values(identity_field).annotate(
            completes=Count("id"),
            accepted=Count(
                "id",
                filter=Q(final_id_status__status=FinalIDUpload.Decision.ACCEPTED),
            ),
            rejected=Count(
                "id",
                filter=Q(final_id_status__status=FinalIDUpload.Decision.REJECTED),
            ),
        )
        for aggregate in rows.iterator(chunk_size=1000):
            report_user_id = resolver(aggregate[identity_field])
            if report_user_id not in aggregates:
                continue
            bucket = aggregates[report_user_id]
            bucket["completes"] += aggregate["completes"]
            bucket["accepted"] += aggregate["accepted"]
            bucket["rejected"] += aggregate["rejected"]

    timestamp_filter = {
        f"{'initiated_at' if date_field == 'initiated' else 'callback_at'}__gte": lower,
        f"{'initiated_at' if date_field == 'initiated' else 'callback_at'}__lte": upper,
    }
    current_attempts = SurveyAttempt.objects.filter(
        platform_user_id__in=visible_ids,
        status=SurveyAttempt.Status.COMPLETED,
        **timestamp_filter,
    )
    current_attempts = _filter_attempt_dimensions(current_attempts, params)
    merge_rows(current_attempts, "platform_user_id", lambda value: value)

    if legacy_user_map:
        legacy_attempts = SurveyAttempt.objects.filter(
            platform_user_id__isnull=True,
            user_id__in=tuple(legacy_user_map),
            status=SurveyAttempt.Status.COMPLETED,
            **timestamp_filter,
        )
        legacy_attempts = _filter_attempt_dimensions(legacy_attempts, params)
        merge_rows(
            legacy_attempts,
            "user_id",
            lambda value: legacy_user_map.get(str(value or "").strip()),
        )

    rows = []
    activity_filter_applied = any(
        _csv_values(params.get(parameter, ""))
        for parameter in ("supplier", "country", "client", "buyer_id", "final_status")
    )
    for user_id in visible_ids:
        counts = aggregates[user_id]
        if activity_filter_applied and not counts["completes"]:
            continue
        reviewed = counts["accepted"] + counts["rejected"]
        pending = max(0, counts["completes"] - reviewed)
        rows.append({
            **visible_metadata[user_id],
            **counts,
            "pending": pending,
            "acceptance_rate": (
                round(counts["accepted"] / counts["completes"] * 100, 1)
                if counts["completes"] else 0.0
            ),
            "rejection_rate": (
                round(counts["rejected"] / counts["completes"] * 100, 1)
                if counts["completes"] else 0.0
            ),
            "pending_rate": (
                round(pending / counts["completes"] * 100, 1)
                if counts["completes"] else 0.0
            ),
            "reviewed_rate": (
                round(reviewed / counts["completes"] * 100, 1)
                if counts["completes"] else 0.0
            ),
        })
    rows.sort(
        key=lambda row: (
            -row["accepted"],
            -row["completes"],
            row["user_name"].casefold(),
            row["user_id"],
        )
    )

    summary = {
        "users": len(rows),
        "active_users": sum(1 for row in rows if row["completes"]),
        "completes": sum(row["completes"] for row in rows),
        "accepted": sum(row["accepted"] for row in rows),
        "rejected": sum(row["rejected"] for row in rows),
        "pending": sum(row["pending"] for row in rows),
    }
    reviewed_total = summary["accepted"] + summary["rejected"]
    summary["acceptance_rate"] = (
        round(summary["accepted"] / summary["completes"] * 100, 1)
        if summary["completes"] else 0.0
    )
    summary["rejection_rate"] = (
        round(summary["rejected"] / summary["completes"] * 100, 1)
        if summary["completes"] else 0.0
    )
    summary["pending_rate"] = (
        round(summary["pending"] / summary["completes"] * 100, 1)
        if summary["completes"] else 0.0
    )
    summary["reviewed_rate"] = (
        round(reviewed_total / summary["completes"] * 100, 1)
        if summary["completes"] else 0.0
    )
    return {
        "rows": rows,
        "summary": summary,
        "period": {
            "year": year,
            "month": month,
            "date_field": date_field,
            "label": period_label,
        },
    }
