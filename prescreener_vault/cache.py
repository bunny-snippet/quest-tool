"""Redis-backed, non-authoritative caches for the isolated profile vault."""

from django.conf import settings
from django.db.models import Count, Q

from config.cache_utils import (
    safe_cache_add,
    safe_cache_delete,
    safe_cache_get,
    safe_cache_get_or_set,
    safe_cache_increment,
    stable_cache_key,
)

from .constants import DATABASE_ALIAS
from .models import PrescreenerSubmission


_VERSION_KEY = "prescreener-vault:version"
_SUMMARY_VERSION_KEY = "prescreener-vault:summary-version"
_OPTIONS_VERSION_KEY = "prescreener-vault:options-version"
_OPTIONS_INVALIDATION_THROTTLE_KEY = "prescreener-vault:options-invalidation"


def _namespace_version() -> int:
    """Return the current logical cache generation."""

    return int(safe_cache_get(_VERSION_KEY, 1) or 1)


def _summary_namespace_version() -> int:
    return int(safe_cache_get(_SUMMARY_VERSION_KEY, 1) or 1)


def _options_namespace_version() -> int:
    return int(safe_cache_get(_OPTIONS_VERSION_KEY, 1) or 1)


def _profile_cache_key(uid: str) -> str:
    return stable_cache_key(
        f"prescreener-vault:v{_namespace_version()}:profile",
        str(uid or "").strip(),
    )


def invalidate_vault_cache(
    uid: str | None = None,
    *,
    summary: bool = True,
    options: bool = True,
) -> None:
    """Invalidate exact profile/summary reads and bound option refresh churn.

    Profile and summary data remain immediately fresh after every write. The
    four DISTINCT selector queries are different: their values change slowly,
    while profile captures and reuses can arrive several times per second. At
    most one write per short window rotates the options generation, and the
    options TTL is capped to that same window so a newly seen dimension still
    appears promptly without forcing every page request back to MySQL.
    """

    normalized_uid = str(uid or "").strip()
    if normalized_uid:
        # Normal traffic always knows the affected UID. Delete only its bounded
        # snapshot instead of making every unrelated profile miss Redis.
        safe_cache_delete(_profile_cache_key(normalized_uid))
    else:
        # Bulk repair/purge commands intentionally invalidate the full profile
        # namespace because they can touch an unbounded set of UIDs.
        safe_cache_increment(_VERSION_KEY)
    if summary:
        safe_cache_increment(_SUMMARY_VERSION_KEY)
    if not options:
        return
    throttle_seconds = max(
        1,
        int(getattr(settings, "VAULT_CACHE_OPTIONS_INVALIDATION_SECONDS", 120)),
    )
    owns_throttle = safe_cache_add(
        _OPTIONS_INVALIDATION_THROTTLE_KEY,
        "1",
        timeout=throttle_seconds,
    )
    if owns_throttle is not False:
        # ``None`` means Redis is unavailable. Increment fail-open so the
        # existing correctness behavior is retained when cache service returns.
        safe_cache_increment(_OPTIONS_VERSION_KEY)


def apply_submission_filters(queryset, selected: dict[str, str]):
    """Apply the Panelist Data UI filters to a vault queryset."""

    if selected.get("search"):
        value = selected["search"]
        # Every stored UID is exactly 19 characters. A full UID search can use
        # the primary-key index; shorter exploratory searches retain the
        # existing contains behavior.
        if len(value) == PrescreenerSubmission._meta.get_field("uid").max_length:
            queryset = queryset.filter(uid__iexact=value)
        else:
            queryset = queryset.filter(uid__icontains=value)
    if selected.get("country"):
        queryset = queryset.filter(country_code__iexact=selected["country"])
    if selected.get("language"):
        queryset = queryset.filter(language_code__iexact=selected["language"])
    if selected.get("age_group"):
        queryset = queryset.filter(respondent_age_group__iexact=selected["age_group"])
    if selected.get("gender"):
        queryset = queryset.filter(respondent_gender__iexact=selected["gender"])
    return queryset


def vault_filter_options() -> dict:
    """Return cached distinct country/language/age/gender selector values."""

    version = _options_namespace_version()
    key = f"prescreener-vault:v{version}:filter-options"

    def load():
        base = PrescreenerSubmission.objects.using(DATABASE_ALIAS).all()
        return {
            "countries": list(
                base.exclude(country_code="")
                .values("country_code", "country")
                .distinct()
                .order_by("country_code")
            ),
            "languages": list(
                base.exclude(language_code="")
                .values("language_code", "language")
                .distinct()
                .order_by("language_code")
            ),
            "age_groups": list(
                base.exclude(respondent_age_group="")
                .values_list("respondent_age_group", flat=True)
                .distinct()
                .order_by("respondent_age_group")
            ),
            "genders": list(
                base.exclude(respondent_gender="")
                .values_list("respondent_gender", flat=True)
                .distinct()
                .order_by("respondent_gender")
            ),
        }

    configured_timeout = getattr(settings, "VAULT_CACHE_OPTIONS_TTL_SECONDS", 600)
    invalidation_window = getattr(
        settings, "VAULT_CACHE_OPTIONS_INVALIDATION_SECONDS", 120
    )
    return safe_cache_get_or_set(
        key,
        load,
        timeout=max(1, min(int(configured_timeout), int(invalidation_window))),
    )


def vault_filtered_summary(selected: dict[str, str]) -> dict:
    """Return cached filter-aware vault totals."""

    version = _summary_namespace_version()
    normalized = {key: str(value or "").strip().lower() for key, value in selected.items()}
    key = stable_cache_key(f"prescreener-vault:v{version}:summary", normalized)

    def load():
        queryset = apply_submission_filters(
            PrescreenerSubmission.objects.using(DATABASE_ALIAS).all(), selected
        )
        return queryset.aggregate(
            total=Count("uid"),
            countries=Count("country_code", distinct=True),
            age_groups=Count("respondent_age_group", distinct=True),
            genders=Count("respondent_gender", distinct=True),
        )

    return safe_cache_get_or_set(
        key,
        load,
        timeout=getattr(settings, "VAULT_CACHE_SUMMARY_TTL_SECONDS", 180),
    )


def cached_profile(uid: str) -> dict | None:
    """Return a bounded normalized profile snapshot without raw question payloads."""

    normalized_uid = str(uid or "").strip()
    if not normalized_uid:
        return None
    key = _profile_cache_key(normalized_uid)

    def load():
        row = (
            PrescreenerSubmission.objects.using(DATABASE_ALIAS)
            .filter(uid=normalized_uid)
            .values(
                "uid",
                "rid",
                "source_client_code",
                "country_code",
                "language_code",
                "respondent_age",
                "respondent_age_group",
                "respondent_gender",
                "respondent_ethnicity",
                "respondent_postal_code",
                "profile_dimensions",
                "usage_count",
                "last_reused_at",
                "submitted_at",
            )
            .first()
        )
        return row

    return safe_cache_get_or_set(
        key,
        load,
        timeout=getattr(settings, "VAULT_CACHE_PROFILE_TTL_SECONDS", 900),
    )
