"""Opaque, integrity-protected tokens for public respondent entry links.

The browser only transports this token. Survey, user and supplier-delivery
identifiers are decrypted and validated on the server, so editing query-string
values cannot select another user, project or external API allocation.
"""

import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class EntryTokenError(ValueError):
    """Raised when a public entry token cannot be authenticated or decoded."""


def _fernet() -> Fernet:
    material = str(
        getattr(settings, "SURVEY_ENTRY_TOKEN_KEY", "") or settings.SECRET_KEY
    ).encode("utf-8")
    digest = hashlib.sha256(b"exchange-hub:survey-entry:v1:" + material).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _journey_fernet() -> Fernet:
    material = str(
        getattr(settings, "SURVEY_ENTRY_TOKEN_KEY", "") or settings.SECRET_KEY
    ).encode("utf-8")
    digest = hashlib.sha256(b"exchange-hub:survey-journey:v1:" + material).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def issue_entry_token(*, survey_id: int, user_id: int, api_key_id: int | None = None) -> str:
    """Return an opaque reusable link token bound to one survey and account."""

    payload = {
        "v": 1,
        "survey_id": int(survey_id),
        "user_id": int(user_id),
        "api_key_id": int(api_key_id) if api_key_id is not None else None,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _fernet().encrypt(raw).decode("ascii")


def decode_entry_token(token: str) -> dict:
    """Authenticate an entry token and return its strictly validated payload."""

    token = str(token or "").strip()
    if not token or len(token) > 2048:
        raise EntryTokenError("Invalid entry token.")
    try:
        payload = json.loads(_fernet().decrypt(token.encode("ascii")).decode("utf-8"))
    except (InvalidToken, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise EntryTokenError("Invalid entry token.") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise EntryTokenError("Unsupported entry token.")
    try:
        survey_id = int(payload["survey_id"])
        user_id = int(payload["user_id"])
        api_key_id = payload.get("api_key_id")
        api_key_id = int(api_key_id) if api_key_id is not None else None
    except (KeyError, TypeError, ValueError) as exc:
        raise EntryTokenError("Invalid entry token payload.") from exc
    if survey_id <= 0 or user_id <= 0 or (api_key_id is not None and api_key_id <= 0):
        raise EntryTokenError("Invalid entry token payload.")
    return {"survey_id": survey_id, "user_id": user_id, "api_key_id": api_key_id}


def issue_journey_token(*, attempt_id: int, nonce: str) -> str:
    """Return an opaque short-lived continuation bound to a browser session."""

    nonce = str(nonce or "")
    if attempt_id <= 0 or not 24 <= len(nonce) <= 128:
        raise EntryTokenError("Invalid journey token payload.")
    payload = {"v": 1, "attempt_id": int(attempt_id), "nonce": nonce}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _journey_fernet().encrypt(raw).decode("ascii")


def decode_journey_token(token: str) -> dict:
    """Authenticate a short-lived journey continuation token."""

    token = str(token or "").strip()
    if not token or len(token) > 2048:
        raise EntryTokenError("Invalid journey token.")
    ttl = max(
        60,
        int(getattr(settings, "SURVEY_JOURNEY_TOKEN_MAX_AGE_SECONDS", 7200)),
    )
    try:
        raw = _journey_fernet().decrypt(token.encode("ascii"), ttl=ttl)
        payload = json.loads(raw.decode("utf-8"))
    except (InvalidToken, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise EntryTokenError("Invalid journey token.") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise EntryTokenError("Unsupported journey token.")
    try:
        attempt_id = int(payload["attempt_id"])
        nonce = str(payload["nonce"])
    except (KeyError, TypeError, ValueError) as exc:
        raise EntryTokenError("Invalid journey token payload.") from exc
    if attempt_id <= 0 or not 24 <= len(nonce) <= 128:
        raise EntryTokenError("Invalid journey token payload.")
    return {"attempt_id": attempt_id, "nonce": nonce}
