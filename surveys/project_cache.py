"""Non-authoritative Projects caches isolated from respondent/profile data."""

from django.conf import settings
from django.db.models import Max, Min

from config.cache_utils import (
    safe_cache_get,
    safe_cache_get_or_set,
    safe_cache_increment,
    stable_cache_key,
)


CACHE_ALIAS = "projects"
_VERSION_KEY = "projects:version"


def _version() -> int:
    return int(safe_cache_get(_VERSION_KEY, 1, alias=CACHE_ALIAS) or 1)


def invalidate_project_cache() -> None:
    """Invalidate metadata/counts without scanning or flushing Redis DB 3."""

    safe_cache_increment(_VERSION_KEY, alias=CACHE_ALIAS)


def project_filter_metadata(
    queryset,
    *,
    user_id: int,
    client_scoped: bool,
    include_cpi: bool,
    cpi_field: str = "cpi",
) -> dict:
    key = stable_cache_key(
        f"projects:v{_version()}:filters",
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
    key = stable_cache_key(
        f"projects:v{_version()}:count",
        {
            "user_id": request.user.pk,
            "query": sorted(
                (key, tuple(values))
                for key, values in request.query_params.lists()
                if key not in count_neutral_parameters
            ),
        },
    )
    return int(safe_cache_get_or_set(
        key,
        queryset.count,
        timeout=settings.PROJECT_CACHE_COUNT_TTL_SECONDS,
        jitter_seconds=settings.PROJECT_CACHE_TTL_JITTER_SECONDS,
        alias=CACHE_ALIAS,
    ))
