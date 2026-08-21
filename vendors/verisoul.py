"""Verisoul policy resolution and backend-only session authentication."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math

import requests
from django.conf import settings

from .models import SecurityPolicyMode
from .services import organization_client_policies_for_user


class VerisoulError(RuntimeError):
    """A safe operational error that never includes credentials or raw payloads."""


@dataclass(frozen=True)
class EffectiveVerisoulPolicy:
    enabled: bool
    client_id: int | None
    scope: str
    scope_id: int | None


@dataclass(frozen=True)
class VerisoulDecision:
    passed: bool
    decision: str
    account_score: Decimal
    request_id: str
    project_id: str
    reason: str
    response_data: dict


def verisoul_sdk_url() -> str:
    environment = str(settings.VERISOUL_ENV or "sandbox").lower()
    if environment not in {"sandbox", "prod"}:
        raise VerisoulError("Verisoul environment must be sandbox or prod.")
    return f"https://js.verisoul.ai/{environment}/bundle.js"


def verisoul_api_url() -> str:
    environment = str(settings.VERISOUL_ENV or "sandbox").lower()
    if environment not in {"sandbox", "prod"}:
        raise VerisoulError("Verisoul environment must be sandbox or prod.")
    return f"https://api.{environment}.verisoul.ai/session/authenticate"


def validate_verisoul_configuration() -> None:
    verisoul_sdk_url()
    if not settings.VERISOUL_PROJECT_ID or not settings.VERISOUL_API_KEY:
        raise VerisoulError("Verisoul credentials are not configured.")
    threshold = float(settings.VERISOUL_ACCOUNT_SCORE_THRESHOLD)
    if not math.isfinite(threshold) or threshold < 0 or threshold > 1:
        raise VerisoulError("Verisoul account-score threshold must be between 0 and 1.")


def effective_verisoul_policy(attempt) -> EffectiveVerisoulPolicy:
    """Resolve client default, supplier override, then closest organization override."""

    client = getattr(attempt, "client", None) or getattr(attempt.survey, "client", None)
    if client is None:
        return EffectiveVerisoulPolicy(False, None, "none", None)

    enabled = bool(client.verisoul_enabled)
    scope = "client"
    scope_id = client.pk

    allocation = getattr(attempt, "client_allocation", None)
    if allocation is not None:
        if allocation.verisoul_mode == SecurityPolicyMode.ENABLED:
            enabled = True
            scope = "supplier"
            scope_id = allocation.pk
        elif allocation.verisoul_mode == SecurityPolicyMode.DISABLED:
            enabled = False
            scope = "supplier"
            scope_id = allocation.pk

    platform_user = getattr(attempt, "platform_user", None)
    if platform_user is not None:
        policies = organization_client_policies_for_user(platform_user)
        organization_policy = policies.get(client.pk) if policies is not None else None
        if organization_policy is not None:
            enabled = organization_policy.verisoul_enabled
            if organization_policy.verisoul_source_unit_id is not None:
                scope = "organization"
                scope_id = organization_policy.verisoul_source_unit_id

    return EffectiveVerisoulPolicy(enabled, client.pk, scope, scope_id)


def _decimal_score(value) -> Decimal:
    try:
        score = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise VerisoulError("Verisoul returned an invalid account score.") from exc
    if not score.is_finite() or score < 0 or score > 1:
        raise VerisoulError("Verisoul returned an invalid account score.")
    return score


def _safe_response_data(payload: dict) -> dict:
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    risk_signals = session.get("risk_signals") if isinstance(session.get("risk_signals"), dict) else {}
    location = session.get("location") if isinstance(session.get("location"), dict) else {}
    return {
        "decision": str(payload.get("decision") or "")[:40],
        "account_score": payload.get("account_score"),
        "request_id": str(payload.get("request_id") or "")[:160],
        "project_id": str(payload.get("project_id") or "")[:160],
        "true_country_code": str(session.get("true_country_code") or location.get("country_code") or "")[:8],
        "risk_signals": {
            str(key)[:80]: bool(value)
            for key, value in risk_signals.items()
            if isinstance(value, bool)
        },
    }


def authenticate_verisoul_session(*, session_id: str, attempt) -> VerisoulDecision:
    """Authenticate one browser session without exposing the API key to the browser."""

    validate_verisoul_configuration()
    session_id = str(session_id or "").strip()
    if not session_id or len(session_id) > 160:
        raise VerisoulError("Verisoul session ID is invalid.")

    account = {
        "id": str(attempt.pid or attempt.rid),
        "metadata": {
            "source": "survey_entry",
            "rid": attempt.rid,
            "project_id": attempt.survey.local_id,
            "provider_survey_id": str(attempt.survey.source_identifier),
            "client_id": attempt.survey.client_id,
        },
    }

    try:
        response = requests.post(
            verisoul_api_url(),
            json={"session_id": session_id, "account": account},
            headers={"x-api-key": settings.VERISOUL_API_KEY, "Content-Type": "application/json"},
            timeout=settings.VERISOUL_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise VerisoulError("Verisoul verification is temporarily unavailable.") from exc
    if response.status_code != 200:
        raise VerisoulError(f"Verisoul verification failed (HTTP {response.status_code}).")
    try:
        payload = response.json()
    except ValueError as exc:
        raise VerisoulError("Verisoul returned an invalid response.") from exc
    if not isinstance(payload, dict):
        raise VerisoulError("Verisoul returned an invalid response.")

    decision = str(payload.get("decision") or "").strip()
    score = _decimal_score(payload.get("account_score"))
    threshold = Decimal(str(settings.VERISOUL_ACCOUNT_SCORE_THRESHOLD))
    passed = decision.lower() == "real" and score < threshold
    if decision.lower() != "real":
        reason = f"Verisoul classified the session as {decision or 'unknown'}."
    elif score >= threshold:
        reason = f"Verisoul account score {score} met or exceeded the allowed threshold {threshold}."
    else:
        reason = "Verisoul classified the session as real within the allowed risk threshold."

    return VerisoulDecision(
        passed=passed,
        decision=decision,
        account_score=score,
        request_id=str(payload.get("request_id") or "")[:160],
        project_id=str(payload.get("project_id") or "")[:160],
        reason=reason[:240],
        response_data=_safe_response_data(payload),
    )
