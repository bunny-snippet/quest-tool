"""Encrypted real-email pool for stable Cint respondent identities."""

import base64
import hashlib
import re

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, connections, transaction
from django.db.models import Case, Count, DateTimeField, F, Value, When
from django.utils import timezone

from config.cache_utils import (
    safe_cache_delete,
    safe_cache_get,
    safe_cache_set,
    stable_cache_key,
)

from .constants import DATABASE_ALIAS
from .models import CintRespondentEmail, CintRespondentEmailUse
from .services import PrescreenerVaultError


UID_PATTERN = re.compile(r"^[A-Za-z0-9]{4}(?:-[A-Za-z0-9]{4}){3}$")
RID_PATTERN = re.compile(r"^[A-Za-z0-9]{10}$")
CACHE_NAMESPACE = "cint-respondent-email:v1"


class CintEmailPoolExhausted(PrescreenerVaultError):
    pass


class CintEmailPoolConfigurationError(PrescreenerVaultError):
    pass


def _fernet() -> Fernet:
    raw = str(settings.RESPONDENT_EMAIL_ENCRYPTION_KEY).strip()
    if not raw:
        raise CintEmailPoolConfigurationError(
            "RESPONDENT_EMAIL_ENCRYPTION_KEY is not configured."
        )
    key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())
    return Fernet(key)


def clean_cint_email(email: str) -> str:
    """Apply Cint's documented normalization before SHA-256 hashing."""

    cleaned = str(email or "").strip().lower()
    try:
        validate_email(cleaned)
    except ValidationError as exc:
        raise ValueError("Enter a valid real respondent email address.") from exc
    local, domain = cleaned.rsplit("@", 1)
    if domain in {"gmail.com", "googlemail.com"}:
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    normalized = f"{local}@{domain}"
    try:
        validate_email(normalized)
    except ValidationError as exc:
        raise ValueError("The normalized respondent email is invalid.") from exc
    return normalized


def cint_email_hash(email: str) -> str:
    return hashlib.sha256(clean_cint_email(email).encode("utf-8")).hexdigest()


def add_real_email(email: str) -> tuple[CintRespondentEmail, bool]:
    """Encrypt and add one real email; normalized duplicates are idempotent."""

    normalized = clean_cint_email(email)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    existing = (
        CintRespondentEmail.objects.using(DATABASE_ALIAS)
        .filter(email_hash=digest)
        .first()
    )
    if existing:
        return existing, False
    encrypted = _fernet().encrypt(normalized.encode("utf-8")).decode("ascii")
    try:
        with transaction.atomic(using=DATABASE_ALIAS):
            row = CintRespondentEmail.objects.using(DATABASE_ALIAS).create(
                encrypted_email=encrypted,
                email_hash=digest,
            )
        return row, True
    except IntegrityError:
        return (
            CintRespondentEmail.objects.using(DATABASE_ALIAS).get(email_hash=digest),
            False,
        )


def reveal_email(identity: CintRespondentEmail) -> str:
    """Operational helper; ordinary respondent flows never decrypt the email."""

    try:
        return _fernet().decrypt(identity.encrypted_email.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise CintEmailPoolConfigurationError(
            "Stored respondent email cannot be decrypted with the configured key."
        ) from exc


def _cache_key(uid: str) -> str:
    return stable_cache_key(CACHE_NAMESPACE, uid)


def _identity_payload(identity: CintRespondentEmail) -> dict:
    return {
        "id": identity.pk,
        "uid": identity.assigned_uid,
        "email_hash": identity.email_hash,
    }


def _load_or_assign(uid: str) -> dict:
    existing = (
        CintRespondentEmail.objects.using(DATABASE_ALIAS)
        .filter(assigned_uid=uid, status=CintRespondentEmail.Status.ASSIGNED)
        .first()
    )
    if existing:
        return _identity_payload(existing)

    try:
        with transaction.atomic(using=DATABASE_ALIAS):
            existing = (
                CintRespondentEmail.objects.using(DATABASE_ALIAS)
                .select_for_update()
                .filter(assigned_uid=uid)
                .first()
            )
            if existing:
                if existing.status != CintRespondentEmail.Status.ASSIGNED:
                    raise CintEmailPoolConfigurationError(
                        "This respondent UID has a disabled email identity."
                    )
                return _identity_payload(existing)

            available = CintRespondentEmail.objects.using(DATABASE_ALIAS).filter(
                status=CintRespondentEmail.Status.AVAILABLE,
                assigned_uid__isnull=True,
            ).order_by("pk")
            connection = connections[DATABASE_ALIAS]
            if connection.features.has_select_for_update_skip_locked:
                available = available.select_for_update(skip_locked=True)
            else:
                available = available.select_for_update()
            identity = available.first()
            if identity is None:
                raise CintEmailPoolExhausted(
                    "No unassigned real respondent email is available for Cint."
                )
            identity.assigned_uid = uid
            identity.status = CintRespondentEmail.Status.ASSIGNED
            identity.assigned_at = timezone.now()
            identity.save(update_fields=[
                "assigned_uid", "status", "assigned_at", "updated_at"
            ])
            return _identity_payload(identity)
    except IntegrityError:
        # Concurrent requests for the same UID converge on the unique assignment.
        identity = CintRespondentEmail.objects.using(DATABASE_ALIAS).get(
            assigned_uid=uid,
            status=CintRespondentEmail.Status.ASSIGNED,
        )
        return _identity_payload(identity)


def _record_distinct_session(identity_id: int, uid: str, rid: str) -> None:
    with transaction.atomic(using=DATABASE_ALIAS):
        identity = (
            CintRespondentEmail.objects.using(DATABASE_ALIAS)
            .select_for_update()
            .filter(
                pk=identity_id,
                assigned_uid=uid,
                status=CintRespondentEmail.Status.ASSIGNED,
            )
            .first()
        )
        if identity is None:
            raise CintEmailPoolConfigurationError(
                "The assigned Cint respondent email is no longer active."
            )
        _, created = CintRespondentEmailUse.objects.using(DATABASE_ALIAS).get_or_create(
            identity_id=identity_id,
            rid=rid,
        )
        if not created:
            return
        now = timezone.now()
        CintRespondentEmail.objects.using(DATABASE_ALIAS).filter(pk=identity_id).update(
            use_count=F("use_count") + 1,
            first_used_at=Case(
                When(first_used_at__isnull=True, then=Value(now)),
                default=F("first_used_at"),
                output_field=DateTimeField(),
            ),
            last_used_at=now,
        )


def assigned_email_hash(uid: str, rid: str) -> str:
    """Return one stable real-email hash and audit each distinct Cint session."""

    uid = str(uid or "").strip()
    rid = str(rid or "").strip()
    if not UID_PATTERN.fullmatch(uid):
        raise CintEmailPoolConfigurationError("Cint respondent UID is invalid.")
    if not RID_PATTERN.fullmatch(rid):
        raise CintEmailPoolConfigurationError("Cint respondent RID is invalid.")

    key = _cache_key(uid)
    payload = safe_cache_get(key)
    if not isinstance(payload, dict) or not payload.get("id") or not payload.get("email_hash"):
        payload = _load_or_assign(uid)
        safe_cache_set(
            key,
            payload,
            timeout=settings.CINT_EMAIL_IDENTITY_CACHE_TTL_SECONDS,
        )
    try:
        _record_distinct_session(int(payload["id"]), uid, rid)
    except CintEmailPoolConfigurationError:
        # Redis is non-authoritative. Recover from a stale row ID after a DB
        # restore while still refusing disabled identities in the vault itself.
        safe_cache_delete(key)
        payload = _load_or_assign(uid)
        safe_cache_set(
            key,
            payload,
            timeout=settings.CINT_EMAIL_IDENTITY_CACHE_TTL_SECONDS,
        )
        _record_distinct_session(int(payload["id"]), uid, rid)
    return str(payload["email_hash"])


def email_pool_status() -> dict:
    rows = (
        CintRespondentEmail.objects.using(DATABASE_ALIAS)
        .values("status")
        .annotate(total=Count("pk"))
    )
    counts = {row["status"]: row["total"] for row in rows}
    return {
        "total": sum(counts.values()),
        "available": counts.get(CintRespondentEmail.Status.AVAILABLE, 0),
        "assigned": counts.get(CintRespondentEmail.Status.ASSIGNED, 0),
        "disabled": counts.get(CintRespondentEmail.Status.DISABLED, 0),
        "session_uses": CintRespondentEmailUse.objects.using(DATABASE_ALIAS).count(),
    }
