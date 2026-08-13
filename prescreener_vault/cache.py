"""Redis-backed, non-authoritative caches for the isolated profile vault."""

from django.conf import settings
from django.db.models import Count, Q

from config.cache_utils import (
    safe_cache_get,
    safe_cache_get_or_set,
    safe_cache_increment,
    stable_cache_key,
)

from .constants import DATABASE_ALIAS
from .models import PrescreenerSubmission


_VERSION_KEY = "prescreener-vault:version"


def _namespace_version() -> int:
    """Return the current logical cache generation."""

    return int(safe_cache_get(_VERSION_KEY, 1) or 1)


def invalidate_vault_cache() -> None:
    """Logically invalidate all cached vault reads in one constant-time write."""

    safe_cache_increment(_VERSION_KEY)


def apply_submission_filters(queryset, selected: dict[str, str]):
    """Apply the Prescreened Data UI filters to a vault queryset."""

    if selected.get("search"):
        value = selected["search"]
        queryset = queryset.filter(Q(uid__icontains=value) | Q(rid__icontains=value))
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

    version = _namespace_version()
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

    return safe_cache_get_or_set(
        key,
        load,
        timeout=getattr(settings, "VAULT_CACHE_OPTIONS_TTL_SECONDS", 600),
    )


def vault_filtered_summary(selected: dict[str, str]) -> dict:
    """Return cached filter-aware vault totals."""

    version = _namespace_version()
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
    version = _namespace_version()
    key = stable_cache_key(f"prescreener-vault:v{version}:profile", normalized_uid)

    def load():
        row = (
            PrescreenerSubmission.objects.using(DATABASE_ALIAS)
            .filter(uid=normalized_uid)
            .values(
                "uid",
                "rid",
                "country_code",
                "language_code",
                "respondent_age",
                "respondent_age_group",
                "respondent_gender",
                "respondent_ethnicity",
                "respondent_postal_code",
                "profile_dimensions",
                "usage_count",
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
