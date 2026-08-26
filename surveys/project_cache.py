"""Non-authoritative Projects caches isolated from respondent/profile data."""

from django.conf import settings
from django.db.models import Max, Min

from config.cache_utils import (
    safe_cache_add,
    safe_cache_get,
    safe_cache_get_or_set,
    safe_cache_increment,
    stable_cache_key,
)


CACHE_ALIAS = "projects"
_FILTER_VERSION_KEY = "projects:filters-version"
_COUNT_VERSION_KEY = "projects:count-version"
_INVALIDATION_THROTTLE_KEY = "projects:invalidate-throttle"


def _version(key: str) -> int:
    return int(safe_cache_get(key, 1, alias=CACHE_ALIAS) or 1)


def invalidate_project_cache(
    *,
    throttle_seconds: int = 0,
    filters: bool = True,
    counts: bool = True,
) -> bool:
    """Invalidate metadata/counts without scanning or flushing Redis DB 3.

    High-frequency feeds can request a small throttle window. The first
    process increments the shared version while later callbacks in that window
    reuse it, preventing every five-second Cint delivery from invalidating all
    user-scoped project metadata. Filter metadata and filtered counts use
    independent versions so a busy inventory feed can keep pagination totals
    fresh without forcing expensive filter-option rebuilds at the same rate.
    Cache outages fail open and still invalidate.
    """

    if not filters and not counts:
        return False
    throttle_seconds = max(0, int(throttle_seconds or 0))
    if throttle_seconds:
        throttle_scope = f"{int(filters)}:{int(counts)}"
        acquired = safe_cache_add(
            f"{_INVALIDATION_THROTTLE_KEY}:{throttle_scope}",
            1,
            timeout=throttle_seconds,
            alias=CACHE_ALIAS,
        )
        if acquired is False:
            return False

    if filters:
        safe_cache_increment(_FILTER_VERSION_KEY, alias=CACHE_ALIAS)
    if counts:
        safe_cache_increment(_COUNT_VERSION_KEY, alias=CACHE_ALIAS)
    return True


def project_filter_metadata(
    queryset,
    *,
    user_id: int,
    client_scoped: bool,
    include_cpi: bool,
    cpi_field: str = "cpi",
) -> dict:
    key = stable_cache_key(
        f"projects:v{_version(_FILTER_VERSION_KEY)}:filters",
        {
            "user_id": user_id,
            "client_scoped": client_scoped,
            "include_cpi": include_cpi,
            "cpi_field": cpi_field,
        },
    )

    def load():
        countries = list(
            queryset.exclude(country_code="")
            .values_list("country_code", "country")
            .distinct()
            .order_by("country_code")
        )
        company_field = "client__name" if client_scoped else "company_name"
        companies = list(
            queryset.exclude(**{company_field: ""})
            .values_list(company_field, flat=True)
            .distinct()
            .order_by(company_field)
        )
        buyer_rows = list(
            queryset.exclude(buyer_id="")
            .values("buyer_id", "client__name", "company_name")
            .distinct()
            .order_by("buyer_id")
        )
        survey_types = list(
            queryset.exclude(survey_type="")
            .values_list("survey_type", flat=True)
            .distinct()
            .order_by("survey_type")
        )
        cpi_bounds = (
            queryset.aggregate(
                minimum=Min(cpi_field),
                maximum=Max(cpi_field),
            )
            if include_cpi
            else {"minimum": None, "maximum": None}
        )
        return {
            "countries": countries,
            "companies": companies,
            "buyer_options": [
                {
                    "value": row["buyer_id"],
                    "client_value": (
                        row["client__name"] if client_scoped else row["company_name"]
                    ) or "",
                }
                for row in buyer_rows
            ],
            "survey_types": survey_types,
            "cpi_min": cpi_bounds["minimum"],
            "cpi_max": cpi_bounds["maximum"],
        }

    return safe_cache_get_or_set(
        key,
        load,
        timeout=settings.PROJECT_CACHE_FILTERS_TTL_SECONDS,
        jitter_seconds=settings.PROJECT_CACHE_TTL_JITTER_SECONDS,
        alias=CACHE_ALIAS,
    )


def project_filtered_count(request, queryset) -> int:
    count_neutral_parameters = {"page", "page_size", "ordering", "format"}
    # DRF's public ``request.auth`` property performs lazy authentication when
    # accessed. Pagination runs after authentication, so its private snapshot
    # is already populated; reading it directly also keeps this cache helper
    # side-effect free for RequestFactory callers and tests.
    request_auth = getattr(request, "_auth", None)
    key = stable_cache_key(
        f"projects:v{_version(_COUNT_VERSION_KEY)}:count",
        {
            "user_id": request.user.pk,
            "api_key_id": getattr(request_auth, "pk", None),
            "query": sorted(
                (key, tuple(values))
                for key, values in request.query_params.lists()
                if key not in count_neutral_parameters
            ),
        },
    )

    def load_count():
        """Count only unique project IDs, not every annotated list column.

        The Projects list queryset can carry viewer-pricing and completes
        annotations plus joins used by organization/supplier visibility. A
        direct ``queryset.count()`` makes MySQL materialize those wide rows in
        a DISTINCT subquery. Pagination only needs unique survey primary keys,
        so clear ordering and narrow the selected columns before counting.
        Filters and permission predicates remain attached to the queryset.
        """

        count_queryset = queryset.order_by()
        # Scope querysets that already require DISTINCT must keep it for
        # correctness, but narrow the subquery back to the primary key so list
        # annotations and wide model columns are not materialized. Ordinary
        # scalar/Exists querysets take the cheaper direct COUNT(*) path.
        if count_queryset.query.distinct:
            count_queryset = count_queryset.values("pk")
        return count_queryset.count()

    return int(safe_cache_get_or_set(
        key,
        load_count,
        timeout=settings.PROJECT_CACHE_COUNT_TTL_SECONDS,
        jitter_seconds=settings.PROJECT_CACHE_TTL_JITTER_SECONDS,
        alias=CACHE_ALIAS,
    ))
