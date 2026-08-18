"""Secure ingestion and normalization for Cint Feed Opportunities callbacks."""

import base64
import hashlib
import json
import time
from decimal import Decimal, InvalidOperation

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from vendors.models import ClientIntegration

from .models import CintWebhookDelivery, Survey, SurveyQuota, TargetingQuestion
from .project_cache import invalidate_project_cache


# Accept the spellings present in the supplied PHP subscription and the
# underscore/legacy spellings used by current Cint API examples.
LOCALE_RULES = {
    "eng_ca": {"country_language_id": 6, "country": "Canada", "country_code": "CA", "language": "English", "language_code": "ENG", "minimum": Decimal("1"), "maximum": Decimal("4")},
    "eng_in": {"country_language_id": 7, "country": "India", "country_code": "IN", "language": "English", "language_code": "ENG", "minimum": Decimal("1"), "maximum": Decimal("4")},
    "eng_gb": {"country_language_id": 8, "country": "United Kingdom", "country_code": "GB", "language": "English", "language_code": "ENG", "minimum": Decimal("1"), "maximum": Decimal("2")},
    "eng_us": {"country_language_id": 9, "country": "United States", "country_code": "US", "language": "English", "language_code": "ENG", "minimum": Decimal("1"), "maximum": Decimal("4")},
    "fra_fr": {"country_language_id": 10, "country": "France", "country_code": "FR", "language": "French", "language_code": "FRE", "minimum": Decimal("1"), "maximum": Decimal("4")},
    "fre_fr": {"country_language_id": 10, "country": "France", "country_code": "FR", "language": "French", "language_code": "FRE", "minimum": Decimal("1"), "maximum": Decimal("4")},
    "hin_in": {"country_language_id": 76, "country": "India", "country_code": "IN", "language": "Hindi", "language_code": "HIN", "minimum": Decimal("1"), "maximum": Decimal("4")},
}

LOCAL_STATE_KEYS = (
    "_cint_supplier_link",
    "_cint_redirect_contract",
    "_cint_redirect_synced_at",
    "_cint_redirect_supplier_code",
    "_cint_redirect_method",
    "_cint_redirect_verified_at",
)


class CintWebhookError(Exception):
    """A callback cannot be authenticated or safely normalized."""


def normalize_locale(value):
    """Normalize callback locale aliases without changing their meaning."""

    return str(value or "").strip().lower().replace("-", "_")


def decimal_value(value):
    """Read Cint monetary values from either a scalar or Monetary Amount."""

    if isinstance(value, dict):
        value = value.get("value", value.get("Value"))
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def extract_opportunities(payload):
    """Support Cint's single-object and batched callback body shapes."""

    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("opportunities"), list):
        rows = payload["opportunities"]
    elif isinstance(payload, dict) and isinstance(payload.get("surveys"), list):
        rows = payload["surveys"]
    elif isinstance(payload, dict) and payload.get("survey_id") is not None:
        rows = [payload]
    else:
        raise CintWebhookError("The callback body contains no Cint opportunities.")
    rows = [row for row in rows if isinstance(row, dict)]
    if not rows:
        raise CintWebhookError("The callback contains no valid survey objects.")
    return rows


def _urlsafe_decode(value):
    value = str(value or "").strip().encode("ascii")
    return base64.urlsafe_b64decode(value + b"=" * (-len(value) % 4))


def parse_signature_header(header):
    """Return timestamp plus every version/key/signature candidate."""

    parts = [part.strip() for part in str(header or "").split(",") if part.strip()]
    if not parts or not parts[0].startswith("t:"):
        raise CintWebhookError("Missing or invalid X-Lucid-Signature timestamp.")
    try:
        timestamp = int(parts[0].split(":", 1)[1])
    except (TypeError, ValueError) as exc:
        raise CintWebhookError("Invalid X-Lucid-Signature timestamp.") from exc
    signatures = []
    for part in parts[1:]:
        fields = part.split(":", 2)
        if len(fields) == 3:
            signatures.append(tuple(fields))
    if not signatures:
        raise CintWebhookError("X-Lucid-Signature contains no signatures.")
    return timestamp, signatures


def validate_public_key(encoded_key):
    """Decode the Cint subscription public key and require an ECDSA key."""

    try:
        der = base64.b64decode("".join(str(encoded_key or "").split()), validate=True)
        key = serialization.load_der_public_key(der)
    except (TypeError, ValueError, UnsupportedAlgorithm) as exc:
        raise CintWebhookError("The configured Cint webhook public key is invalid.") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise CintWebhookError("The configured Cint webhook public key is not ECDSA.")
    return key


def webhook_credentials(integration):
    """Resolve non-secret webhook verification metadata from env or integration config."""

    config = integration.config or {}
    public_key = str(
        config.get("opportunities_public_key")
        or getattr(settings, "CINT_OPPORTUNITIES_PUBLIC_KEY", "")
        or config.get("public_key")
        or ""
    ).strip()
    key_id = str(
        config.get("opportunities_key_id")
        or getattr(settings, "CINT_OPPORTUNITIES_KEY_ID", "")
        or config.get("key_id")
        or ""
    ).strip()
    return public_key, key_id


def integration_webhook_enabled(integration):
    """Return whether one Cint connection accepts Opportunities callbacks."""

    config = integration.config or {}
    if config.get("opportunities_webhook_enabled") is True:
        return True
    return bool(getattr(settings, "CINT_OPPORTUNITIES_WEBHOOK_ENABLED", False))


def candidate_integrations():
    """Limit callback verification to the configured active Cint supplier."""

    queryset = ClientIntegration.objects.filter(
        provider_code="cint",
        is_active=True,
        client__is_active=True,
    ).select_related("client")
    supplier_code = str(
        getattr(settings, "CINT_OPPORTUNITIES_SUPPLIER_CODE", "") or ""
    ).strip()
    if supplier_code:
        queryset = queryset.filter(supplier_code=supplier_code)
    return [integration for integration in queryset if integration_webhook_enabled(integration)]


def webhook_receiver_enabled():
    """Avoid exposing a callback surface until at least one Cint feed is enabled."""

    return bool(candidate_integrations())


def verify_signature(raw_body, signature_header, integration, *, now=None):
    """Verify Cint ECDSA-SHA256 signature and reject replay-aged callbacks."""

    public_key, configured_key_id = webhook_credentials(integration)
    if not public_key or not configured_key_id:
        raise CintWebhookError("Cint webhook public_key/key_id are not configured.")
    timestamp, signatures = parse_signature_header(signature_header)
    current = int(time.time() if now is None else now)
    tolerance = max(
        30,
        int(getattr(settings, "CINT_OPPORTUNITIES_SIGNATURE_TOLERANCE_SECONDS", 300)),
    )
    if abs(current - timestamp) > tolerance:
        raise CintWebhookError("Cint callback timestamp is outside the replay window.")
    message = str(timestamp).encode("ascii") + b"." + raw_body
    key = validate_public_key(public_key)
    for version, signature_key_id, encoded_signature in signatures:
        if version != "v1" or not configured_key_id.startswith(signature_key_id):
            continue
        try:
            key.verify(
                _urlsafe_decode(encoded_signature),
                message,
                ec.ECDSA(hashes.SHA256()),
            )
            return timestamp, signature_key_id
        except (InvalidSignature, ValueError):
            continue
    raise CintWebhookError("Cint callback signature verification failed.")


def resolve_signed_integration(raw_body, signature_header):
    """Find the integration whose returned Cint public key validates this callback."""

    errors = []
    for integration in candidate_integrations():
        try:
            timestamp, key_id = verify_signature(raw_body, signature_header, integration)
            return integration, timestamp, key_id
        except CintWebhookError as exc:
            errors.append(str(exc))
    if not errors:
        raise CintWebhookError("No active Cint webhook integration is configured.")
    raise CintWebhookError("No configured Cint integration accepted this signature.")


def resolve_local_test_integration():
    """Select one explicit Cint integration for DEBUG-only unsigned replay."""

    integrations = candidate_integrations()
    if len(integrations) != 1:
        raise CintWebhookError(
            "Local webhook replay requires exactly one matching active Cint integration."
        )
    return integrations[0]


def delivery_event_key(integration_id, signature_header, raw_body):
    """Build an idempotency key that remains stable across Cint retries."""

    timestamp_part = str(signature_header or "local-test").split(",", 1)[0]
    material = (
        str(integration_id).encode("ascii")
        + b"\0"
        + timestamp_part.encode("utf-8")
        + b"\0"
        + raw_body
    )
    return hashlib.sha256(material).hexdigest()


def subscription_payload(callback):
    """Build the current official Opportunities subscription request body."""

    return {
        "callback": str(callback).strip(),
        "include_quotas": True,
        "payload_max_size_mb": 8,
        "payload_max_survey_count": 1000,
        "send_interval_seconds": 5,
        "opportunities": [
            {
                "country_language": {
                    "in": ["eng_in", "eng_us", "eng_ca", "fra_fr", "hin-in"]
                },
                "revenue_per_interview": {"gte": 1, "lte": 4},
                "study_type": {"eq": "adhoc"},
            },
            {
                "country_language": {"in": ["eng_gb"]},
                "revenue_per_interview": {"gte": 1, "lte": 2},
                "study_type": {"eq": "adhoc"},
            },
        ],
    }


def opportunity_is_eligible(row):
    """Reapply subscription criteria before allowing a row into inventory."""

    locale = normalize_locale(row.get("country_language"))
    rule = LOCALE_RULES.get(locale)
    rpi = decimal_value(row.get("revenue_per_interview"))
    study_type = str(row.get("study_type") or "").strip().lower()
    return bool(
        rule
        and rpi is not None
        and rule["minimum"] <= rpi <= rule["maximum"]
        and study_type == "adhoc"
    )


def _integer(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


CINT_STANDARD_QUESTION_FALLBACKS = {
    # Feed Opportunities intentionally carries only Question IDs and precodes.
    # These three standard profile IDs are documented across the configured
    # Cint markets, so keep their controls readable while the localized
    # Question Library response is being hydrated.
    42: {
        "key": "AGE",
        "text": "What is your age?",
        "question_type": "Numeric - Open-end",
        "category": "Demographic",
    },
    43: {
        "key": "GENDER",
        "text": "Are you...?",
        "question_type": "Single Punch",
        "category": "Demographic",
        "options": {"1": "Male", "2": "Female"},
    },
    45: {
        "key": "ZIP",
        "text": "What is your ZIP/postal code?",
        "question_type": "Numeric - Open-end",
        "category": "Demographic",
    },
}


def _targeting_rows(survey, payload):
    merged = {}
    for qualification in payload.get("survey_qualifications") or []:
        if not isinstance(qualification, dict):
            continue
        question_id = _integer(qualification.get("question_id"), -1)
        if question_id < 0:
            continue
        precodes = [str(value) for value in qualification.get("precodes") or []]
        current = merged.setdefault(question_id, {
            "precodes": [],
            "logical_operator": str(qualification.get("logical_operator") or "OR").upper(),
            "conditions": [],
        })
        current["precodes"] = list(dict.fromkeys(current["precodes"] + precodes))
        current["conditions"].append(qualification)
    rows = []
    for question_id, qualification in merged.items():
        precodes = qualification["precodes"]
        fallback = CINT_STANDARD_QUESTION_FALLBACKS.get(question_id, {})
        option_labels = fallback.get("options", {})
        rows.append(TargetingQuestion(
            survey=survey,
            question_id=question_id,
            key=fallback.get("key", f"CINT_Q_{question_id}"),
            text=fallback.get("text", f"Cint qualification {question_id}"),
            question_type=fallback.get("question_type", "Qualification"),
            category=fallback.get("category", "Cint qualification"),
            options=[
                {"OptionId": value, "OptionText": option_labels.get(value, value)}
                for value in precodes
            ],
            raw_data={
                "provider": "cint",
                "source": "opportunities_webhook",
                "logical_operator": qualification["logical_operator"],
                "targeting_choices": precodes,
                "qualification": qualification["conditions"],
            },
        ))
    return rows


def _quota_rows(survey, payload):
    rows = []
    for index, quota in enumerate(payload.get("survey_quotas") or []):
        if not isinstance(quota, dict):
            continue
        quota_id = _integer(quota.get("survey_quota_id"), 0)
        quota_type = str(quota.get("survey_quota_type") or "Client")
        remaining = max(0, _integer(quota.get("number_of_respondents"), 0))
        rows.append(SurveyQuota(
            survey=survey,
            source_key=str(quota_id or f"cint-webhook-{index}"),
            quota_id=quota_id or None,
            title=f"{quota_type} quota",
            name=f"{quota_type} quota",
            remaining=remaining,
            status="Full" if remaining == 0 else "Open",
            targeting={"questions": quota.get("questions") or []},
            raw_data={
                **quota,
                "provider": "cint",
                "source": "opportunities_webhook",
                "quotaLimitBy": "completes",
            },
        ))
    return rows


def upsert_opportunity(integration, payload, seen_at):
    """Apply one new/update/reactivate/deactivate message idempotently."""

    source_key = str(payload.get("survey_id") or "").strip()
    if not source_key.isdigit():
        raise CintWebhookError("Cint opportunity is missing a numeric survey_id.")
    existing = Survey.objects.filter(
        integration=integration,
        source_key=source_key,
    ).first()
    if existing:
        previous_received_at = parse_datetime(str(
            (existing.raw_data or {}).get("_cint_webhook_received_at") or ""
        ))
        if previous_received_at and previous_received_at > seen_at:
            # Celery retries can make an older delivery finish after a newer
            # one. Never let that stale message roll inventory backwards.
            return "skipped", existing
    reason = str(payload.get("message_reason") or "updated").strip().lower()
    explicitly_closed = payload.get("is_live") is False or reason == "deactivated"
    eligible = opportunity_is_eligible(payload)
    if explicitly_closed or not eligible:
        if existing and existing.status != Survey.Status.CLOSED:
            existing.status = Survey.Status.CLOSED
            existing.last_seen_at = seen_at
            existing.source_modified_at = seen_at
            existing.raw_data = {**(existing.raw_data or {}), **payload}
            existing.save(update_fields=[
                "status", "last_seen_at", "source_modified_at", "raw_data", "updated_at"
            ])
            return "closed", existing
        return "skipped", existing

    locale = normalize_locale(payload.get("country_language"))
    locale_rule = LOCALE_RULES[locale]
    rpi = decimal_value(payload.get("revenue_per_interview"))
    completes = max(0, _integer(payload.get("overall_completes"), 0))
    remaining = max(0, _integer(payload.get("total_remaining"), 0))
    raw_data = {
        **payload,
        # The webhook locale string and the REST Question Library use two
        # representations of the same market. Persist both so the first Eye or
        # prescreener request can hydrate exact country-language labels.
        "CountryLanguageID": locale_rule["country_language_id"],
        "_cint_inventory_source": "opportunities_webhook",
        "_cint_locale": locale,
        "_cint_country_language_request_id": locale_rule["country_language_id"],
        "_cint_webhook_received_at": seen_at.isoformat(),
    }
    entry_link = test_entry_link = ""
    if existing:
        for key in LOCAL_STATE_KEYS:
            if key in (existing.raw_data or {}):
                raw_data[key] = existing.raw_data[key]
        entry_link = existing.entry_link
        test_entry_link = existing.test_entry_link
    values = {
        "client": integration.client,
        "integration": integration,
        "source_id": int(source_key),
        "source_key": source_key,
        "company_name": integration.client.name,
        "name": str(payload.get("survey_name") or f"Cint survey {source_key}")[:500],
        "status": Survey.Status.LIVE,
        "sample_size": completes + remaining,
        "completes": completes,
        "remaining": remaining,
        "cpi": rpi,
        "loi": max(0, _integer(payload.get("bid_length_of_interview"), 0)),
        "incidence_rate": decimal_value(payload.get("bid_incidence")),
        "country": locale_rule["country"],
        "country_code": locale_rule["country_code"],
        "language": locale_rule["language"],
        "language_code": locale_rule["language_code"],
        "group_type": str(payload.get("study_type") or "")[:80],
        "buyer_id": str(payload.get("account_name") or payload.get("buyer_id") or "")[:160],
        "entry_link": entry_link,
        "test_entry_link": test_entry_link,
        "job_category": str(payload.get("industry") or "")[:180],
        "has_quota": bool(payload.get("survey_quotas")),
        "is_pii_required": bool(payload.get("collects_pii")),
        "is_recontact": bool(payload.get("respondent_pids") or payload.get("recontact_count")),
        # Feed Opportunities has no provider-created/provider-modified fields.
        # The signed delivery receipt is therefore the authoritative source
        # timestamp: creation remains stable, while modified tracks the newest
        # accepted Cint event for this survey.
        "source_created_at": (
            existing.source_created_at if existing and existing.source_created_at else seen_at
        ),
        "source_modified_at": seen_at,
        "last_seen_at": seen_at,
        "raw_data": raw_data,
    }
    with transaction.atomic():
        previous_qualifications = (
            (existing.raw_data or {}).get("survey_qualifications")
            if existing is not None else None
        )
        targeting_changed = (
            existing is None
            or previous_qualifications != payload.get("survey_qualifications")
            or not existing.targeting_questions.exists()
        )
        previous_targeting_synced_at = (
            existing.targeting_synced_at if existing is not None else None
        )
        if existing is None:
            survey = Survey.objects.create(**values)
            action = "created"
        else:
            survey = existing
            changed = any(getattr(survey, key) != value for key, value in values.items())
            for key, value in values.items():
                setattr(survey, key, value)
            survey.save()
            action = "updated" if changed else "skipped"
        # Do not replace already-hydrated Question Library labels on every
        # five-second webhook delivery. Replace them only when the actual
        # qualification contract changes; the null sync timestamp then causes
        # an immediate official REST hydration on the first consumer request.
        if targeting_changed:
            survey.targeting_questions.all().delete()
            TargetingQuestion.objects.bulk_create(_targeting_rows(survey, payload))
        survey.quotas.all().delete()
        SurveyQuota.objects.bulk_create(_quota_rows(survey, payload))
        survey.targeting_synced_at = (
            None if targeting_changed else previous_targeting_synced_at
        )
        survey.quota_synced_at = seen_at
        survey.detail_synced_at = (
            None if targeting_changed else survey.detail_synced_at
        )
        survey.save(update_fields=[
            "targeting_synced_at", "quota_synced_at", "detail_synced_at", "updated_at"
        ])
    return action, survey


def process_delivery(delivery_id):
    """Process one stored callback and return durable counters."""

    delivery = CintWebhookDelivery.objects.select_related("integration__client").get(
        pk=delivery_id
    )
    if delivery.status == CintWebhookDelivery.Status.PROCESSED:
        return delivery
    CintWebhookDelivery.objects.filter(pk=delivery.pk).update(
        status=CintWebhookDelivery.Status.PROCESSING,
        error="",
    )
    counters = {"created": 0, "updated": 0, "closed": 0, "skipped": 0, "errors": 0}
    errors = []
    # Delivery receipt time, not worker start time, establishes update order.
    seen_at = delivery.received_at
    for payload in extract_opportunities(delivery.payload):
        try:
            action, _survey = upsert_opportunity(delivery.integration, payload, seen_at)
            counters[action] += 1
        except Exception as exc:  # preserve the rest of a Cint batch
            counters["errors"] += 1
            errors.append(f"{payload.get('survey_id', 'unknown')}: {exc}")
    delivery.created_count = counters["created"]
    delivery.updated_count = counters["updated"]
    delivery.closed_count = counters["closed"]
    delivery.skipped_count = counters["skipped"]
    delivery.error_count = counters["errors"]
    delivery.error = "\n".join(errors)[:10000]
    delivery.status = (
        CintWebhookDelivery.Status.PARTIAL
        if counters["errors"]
        else CintWebhookDelivery.Status.PROCESSED
    )
    delivery.processed_at = timezone.now()
    delivery.save(update_fields=[
        "created_count", "updated_count", "closed_count", "skipped_count",
        "error_count", "error", "status", "processed_at",
    ])
    invalidate_project_cache()
    if getattr(settings, "CINT_OPPORTUNITIES_QUEUE_REDIRECTS", False) and (
        counters["created"] or counters["updated"]
    ):
        from .tasks import sync_cint_redirects_task

        sync_cint_redirects_task.delay(delivery.integration_id, batch_size=10)
    return delivery
