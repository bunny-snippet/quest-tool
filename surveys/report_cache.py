"""Permission-scoped caches for expensive read-only workspace reports.

MySQL remains authoritative.  Short result TTLs keep active traffic close to
real time while preventing repeated filters, page navigation and dashboard
refreshes from recalculating the same aggregates.  Filter metadata has a longer
TTL because country/client/hierarchy choices change far less often.
"""

from collections.abc import Callable, Iterable
import time
from typing import Any

from django.conf import settings

from accounts.access import activity_visible_user_ids, effective_permission_codes
from config.cache_utils import (
    safe_cache_add,
    safe_cache_delete,
    safe_cache_get,
    safe_cache_increment,
    safe_cache_set,
    stable_cache_key,
)


CACHE_ALIAS = "reports"
_MISSING = object()
REPORT_METADATA_GENERATION_KEY = "reports:metadata:generation"


def report_metadata_generation() -> int:
    """Return the global version for filter/hierarchy selector payloads."""

    return int(safe_cache_get(
        REPORT_METADATA_GENERATION_KEY, 1, alias=CACHE_ALIAS
    ) or 1)


def invalidate_report_metadata_cache() -> int:
    """Expire selector payloads after their underlying dimensions change."""

    return safe_cache_increment(
        REPORT_METADATA_GENERATION_KEY, default=1, alias=CACHE_ALIAS
    )


def _cached_with_stale_revalidation(key: str, factory: Callable[[], Any], *, timeout: int) -> Any:
    """Prevent a popular report key from stampeding MySQL when it expires.

    Fresh results are returned normally.  One request refreshes an expired
    value while concurrent requests receive the last successful payload.  On a
    cold miss, peers briefly wait for the lock owner instead of launching the
    same full-table aggregate hundreds of times.
    """

    now = time.time()
    record = safe_cache_get(key, _MISSING, alias=CACHE_ALIAS)
    if isinstance(record, dict) and record.get("_report_cache_record") is True:
        if float(record.get("fresh_until") or 0) > now:
            return record.get("payload")
        stale_payload = record.get("payload")
    else:
        stale_payload = _MISSING

    lock_key = f"{key}:refresh-lock"
    owns_lock = safe_cache_add(lock_key, "1", timeout=30, alias=CACHE_ALIAS)
    if owns_lock:
        try:
            value = factory()
            safe_cache_set(
                key,
                {
                    "_report_cache_record": True,
                    "fresh_until": time.time() + timeout,
                    "payload": value,
                },
                # Keep stale data long enough for fail-open background-style
                # refreshes, without making reporting permanently stale.
                timeout=timeout + max(60, timeout * 4),
                jitter_seconds=settings.REPORT_CACHE_TTL_JITTER_SECONDS,
                alias=CACHE_ALIAS,
            )
            return value
        finally:
            safe_cache_delete(lock_key, alias=CACHE_ALIAS)

    if stale_payload is not _MISSING:
        return stale_payload
    if owns_lock is None:
        return factory()

    # A cold cache has no stale result. Wait for the single owner for at most
    # 400 ms; this is much cheaper than multiplying a large aggregate query.
    for _ in range(8):
        time.sleep(0.05)
        peer_record = safe_cache_get(key, _MISSING, alias=CACHE_ALIAS)
        if isinstance(peer_record, dict) and peer_record.get("_report_cache_record") is True:
            return peer_record.get("payload")
    return factory()


def _parameter_lists(request, neutral_parameters: Iterable[str]) -> list:
    source = getattr(request, "query_params", None) or request.GET
    neutral = set(neutral_parameters)
    return sorted(
        (key, tuple(values))
        for key, values in source.lists()
        if key not in neutral
    )


def report_viewer_scope(user) -> dict:
    """Build a bounded, permission-safe viewer fingerprint for cache keys."""

    return {
        "user_id": user.pk,
        "superuser": bool(user.is_superuser),
        "permissions": sorted(effective_permission_codes(user)),
        "visible_users": sorted(activity_visible_user_ids(user)),
    }


def cached_report_payload(
    namespace: str,
    request,
    factory: Callable[[], Any],
    *,
    timeout: int | None = None,
    neutral_parameters: Iterable[str] = ("page", "page_size", "format", "ordering"),
    extra_scope: Any = None,
) -> Any:
    """Cache a filtered aggregate/payload without exposing query values in keys."""

    key = stable_cache_key(
        f"reports:{namespace}",
        {
            "viewer": report_viewer_scope(request.user),
            "query": _parameter_lists(request, neutral_parameters),
            "extra": extra_scope,
        },
    )
    return _cached_with_stale_revalidation(
        key,
        factory,
        timeout=timeout or settings.REPORT_CACHE_RESULT_TTL_SECONDS,
    )


def cached_user_metadata(
    namespace: str,
    user,
    factory: Callable[[], Any],
    *,
    extra_scope: Any = None,
) -> Any:
    """Cache hierarchy/filter selector metadata for one effective viewer scope."""

    key = stable_cache_key(
        f"reports:{namespace}:metadata",
        {
            "viewer": report_viewer_scope(user),
            "extra": extra_scope,
            "metadata_generation": report_metadata_generation(),
        },
    )
    return _cached_with_stale_revalidation(
        key,
        factory,
        timeout=settings.REPORT_CACHE_METADATA_TTL_SECONDS,
    )


def cached_integration_metadata(
    namespace: str,
    integration,
    factory: Callable[[], Any],
    *,
    timeout: int | None = None,
) -> Any:
    """Cache expensive provider-card counters shared by authorized viewers."""

    key = stable_cache_key(
        f"reports:integration:{namespace}",
        {
            "integration_id": integration.pk,
            "integration_updated_at": integration.updated_at,
        },
    )
    return _cached_with_stale_revalidation(
        key,
        factory,
        timeout=timeout or settings.REPORT_CACHE_RESULT_TTL_SECONDS,
    )


def traffic_filter_metadata(user, visible_attempts, visible_surveys) -> dict:
    """Return cached Traffic filter choices without caching attempt rows."""

    def load():
        from .user_hits import user_hit_filter_options

        hierarchy = user_hit_filter_options(user)
        supplier_rows = list(
            visible_attempts.filter(vendor__isnull=False)
            .values(
                "vendor_id",
                "vendor__first_name",
                "vendor__last_name",
                "vendor__username",
                "vendor__email",
            )
            .distinct()
            .order_by("vendor__first_name", "vendor__last_name", "vendor__username")
        )
        suppliers = []
        for row in supplier_rows:
            full_name = " ".join(
                part for part in (row["vendor__first_name"], row["vendor__last_name"]) if part
            ).strip()
            suppliers.append({
                "value": str(row["vendor_id"]),
                "name": full_name or row["vendor__username"] or row["vendor__email"] or f"Supplier {row['vendor_id']}",
                "email": row["vendor__email"] or "",
            })
        return {
            **hierarchy,
            "suppliers": suppliers,
            "countries": list(
                visible_surveys.exclude(country_code="")
                .values("country_code", "country")
                .distinct()
                .order_by("country_code")
            ),
            "clients": list(
                visible_attempts.filter(survey__client__isnull=False)
                .values("survey__client_id", "survey__client__name")
                .distinct()
                .order_by("survey__client__name")
            ),
            "buyers": list(
                visible_attempts.exclude(survey__buyer_id="")
                .values("survey__buyer_id", "survey__client_id")
                .distinct()
                .order_by("survey__buyer_id")
            ),
        }

    # v2 invalidates metadata created before supplier-aware filters and also
    # refreshes organization labels after this deployment.
    return cached_user_metadata("traffic-filters-v2", user, load)


def term_filter_metadata(user, base_queryset) -> dict:
    """Return cached Term Report selector values and hierarchy choices."""

    def load():
        from .user_hits import user_hit_filter_options

        hierarchy = user_hit_filter_options(user)
        return {
            **hierarchy,
            "countries": list(
                base_queryset.exclude(survey__country_code="")
                .values("survey__country_code", "survey__country")
                .distinct()
                .order_by("survey__country_code")
            ),
            "clients": list(
                base_queryset.filter(survey__client__isnull=False)
                .values("survey__client_id", "survey__client__name")
                .distinct()
                .order_by("survey__client__name")
            ),
            "buyers": list(
                base_queryset.exclude(survey__buyer_id="")
                .values("survey__client_id", "survey__buyer_id")
                .distinct()
                .order_by("survey__buyer_id")
            ),
        }

    # Keep this namespace versioned whenever the visibility/scoping rules change.
    # That prevents a short-lived permission leak from a payload cached by older
    # application code during a rolling deploy.
    return cached_user_metadata("term-filters-v2", user, load)
