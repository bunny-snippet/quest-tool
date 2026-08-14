"""Provider-neutral respondent identifiers, audit capture and redirect helpers."""

import secrets
import string
import re
from ipaddress import ip_address
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.db import IntegrityError, transaction

from .identifiers import generate_platform_pid
from .models import Survey, SurveyAttempt


RID_ALPHABET = string.ascii_letters + string.digits
PRESCREENER_UID_ALPHABET = string.ascii_letters + string.digits


def generate_rid() -> str:
    """Generate a 10-character RID containing upper, lower and numeric characters."""
    characters = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        *(secrets.choice(RID_ALPHABET) for _ in range(7)),
    ]
    secrets.SystemRandom().shuffle(characters)
    return "".join(characters)


def generate_prescreener_uid() -> str:
    """Generate 16 mixed alphanumeric characters rendered in four groups."""
    characters = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        *(secrets.choice(PRESCREENER_UID_ALPHABET) for _ in range(13)),
    ]
    secrets.SystemRandom().shuffle(characters)
    compact = "".join(characters)
    return "-".join(compact[index:index + 4] for index in range(0, 16, 4))


def ensure_attempt_prescreener_uid(attempt: SurveyAttempt) -> str:
    """Allocate one stable vault UID for an attempt, including legacy attempts."""
    if attempt.prescreener_uid:
        return attempt.prescreener_uid
    for _ in range(10):
        candidate = generate_prescreener_uid()
        try:
            updated = SurveyAttempt.objects.filter(
                pk=attempt.pk, prescreener_uid__isnull=True
            ).update(prescreener_uid=candidate)
        except IntegrityError:
            continue
        if updated:
            attempt.prescreener_uid = candidate
            return candidate
        attempt.refresh_from_db(fields=["prescreener_uid"])
        if attempt.prescreener_uid:
            return attempt.prescreener_uid
    raise RuntimeError("Could not allocate a unique prescreener UID")


def normalize_client_ip(value) -> str | None:
    """Return a syntactically valid non-loopback/non-unspecified IP or ``None``."""

    if not value:
        return None
    try:
        parsed = ip_address(str(value).strip())
    except ValueError:
        return None
    if parsed.is_loopback or parsed.is_unspecified:
        return None
    return str(parsed)


def get_request_ip(request) -> str | None:
    """Return the original client IP, trusting proxy headers only when configured."""
    if settings.TRUST_X_FORWARDED_FOR:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        candidates = [
            request.META.get("HTTP_CF_CONNECTING_IP"),
            *(part.strip() for part in forwarded.split(",") if part.strip()),
            request.META.get("HTTP_X_REAL_IP"),
        ]
        for candidate in candidates:
            normalized = normalize_client_ip(candidate)
            if normalized:
                return normalized
    return normalize_client_ip(request.META.get("REMOTE_ADDR"))


def supplier_code_from_entry_link(entry_link: str) -> str:
    """Snapshot the real provider supplier code embedded in an allocated link."""

    query = dict(parse_qsl(urlsplit(entry_link).query, keep_blank_values=True))
    return str(query.get("supCode") or query.get("supplierCode") or "")


def _versioned_match(user_agent: str, patterns: list[tuple[str, str]]) -> str:
    """Return the first named user-agent pattern and its normalized version."""

    for name, pattern in patterns:
        match = re.search(pattern, user_agent, re.IGNORECASE)
        if match:
            version = match.group(1).replace("_", ".") if match.lastindex else ""
            return f"{name} {version}".strip()
    return "Unknown"


def get_request_client_data(request) -> dict:
    """Return a deliberately limited, non-cookie client audit snapshot."""
    user_agent = request.META.get("HTTP_USER_AGENT", "")[:4000]
    browser = _versioned_match(user_agent, [
        ("Edge", r"(?:Edg|EdgiOS|EdgA)/([\d.]+)"),
        ("Opera", r"(?:OPR|Opera)/([\d.]+)"),
        ("Chrome", r"(?:Chrome|CriOS)/([\d.]+)"),
        ("Firefox", r"(?:Firefox|FxiOS)/([\d.]+)"),
        ("Safari", r"Version/([\d.]+).*Safari"),
        ("Internet Explorer", r"(?:MSIE\s|rv:)([\d.]+)"),
    ])
    os_name = _versioned_match(user_agent, [
        ("Windows", r"Windows NT\s([\d.]+)"),
        ("Android", r"Android\s([\d.]+)"),
        ("iOS", r"(?:iPhone OS|CPU OS)\s([\d_]+)"),
        ("macOS", r"Mac OS X\s([\d_]+)"),
        ("Chrome OS", r"CrOS\s[^\s]+\s([\d.]+)"),
    ])
    lowered = user_agent.lower()
    if any(token in lowered for token in ("bot", "crawler", "spider", "slurp")):
        device = "Bot"
    elif "ipad" in lowered or "tablet" in lowered:
        device = "Tablet"
    elif any(token in lowered for token in ("mobile", "iphone", "android")):
        device = "Mobile"
    else:
        device = "Desktop" if user_agent else "Unknown"

    return {
        "user_agent": user_agent,
        "browser": browser,
        "device": device,
        "os": os_name,
        "accept_language": request.META.get("HTTP_ACCEPT_LANGUAGE", "")[:500],
        "referrer": request.META.get("HTTP_REFERER", "")[:4000],
        "sec_ch_ua": request.META.get("HTTP_SEC_CH_UA", "")[:500],
        "sec_ch_ua_mobile": request.META.get("HTTP_SEC_CH_UA_MOBILE", "")[:40],
        "sec_ch_ua_platform": request.META.get("HTTP_SEC_CH_UA_PLATFORM", "")[:120],
    }


def backfill_attempt_entry_audit(attempt: SurveyAttempt, request) -> SurveyAttempt:
    """Populate missing entry audit fields from a later request for the same RID.

    The first start-link request normally provides these values. This fallback
    also repairs an attempt created by an older web process during a rolling
    deployment, without replacing entry data that has already been recorded.
    """
    client_data = get_request_client_data(request)
    request_ip = get_request_ip(request)
    updates = {}

    if not attempt.initiation_ip and request_ip:
        updates["initiation_ip"] = request_ip

    field_sources = {
        "entry_user_agent": "user_agent",
        "entry_browser": "browser",
        "entry_device": "device",
        "entry_os": "os",
        "entry_referrer": "referrer",
        "entry_accept_language": "accept_language",
    }
    has_client_signal = any(client_data.get(key) for key in (
        "user_agent", "accept_language", "referrer", "sec_ch_ua", "sec_ch_ua_platform"
    ))
    if has_client_signal:
        for model_field, data_key in field_sources.items():
            if not getattr(attempt, model_field) and client_data.get(data_key):
                updates[model_field] = client_data[data_key]
        if not attempt.entry_client_data:
            updates["entry_client_data"] = client_data

    if updates:
        SurveyAttempt.objects.filter(pk=attempt.pk).update(**updates)
        for field, value in updates.items():
            setattr(attempt, field, value)
    return attempt


def create_attempt(
    survey: Survey,
    platform_user,
    ip_address: str | None,
    client_data: dict | None = None,
    pid: str | None = None,
) -> SurveyAttempt:
    """Create one immutable respondent journey with fresh RID/PID/UID and CPI audit.

    The transaction makes identifier allocation and the historical attempt
    snapshots one unit. Database uniqueness is the final collision guard.
    """

    client_data = client_data or {}
    requested_pid = str(pid or "").strip()
    if requested_pid and (
        not requested_pid.isalnum() or not 6 <= len(requested_pid) <= 9
    ):
        raise ValueError("Invalid platform PID.")
    for attempt_number in range(10):
        try:
            with transaction.atomic():
                return SurveyAttempt.objects.create(
                    rid=generate_rid(),
                    # Preserve the PID copied in the entry URL on the first try.
                    # A database collision retries with a fresh server PID.
                    pid=(
                        requested_pid
                        if requested_pid and attempt_number == 0
                        else generate_platform_pid()
                    ),
                    prescreener_uid=generate_prescreener_uid(),
                    survey=survey,
                    platform_user=platform_user,
                    user_id=str(platform_user.pk),
                    supplier_code=supplier_code_from_entry_link(survey.entry_link),
                    source_cpi_snapshot=survey.cpi,
                    cpi_snapshot_source="captured",
                    payable_cpi_snapshot=survey.cpi,
                    cpi_currency_snapshot="USD",
                    initiation_ip=ip_address,
                    entry_user_agent=client_data.get("user_agent", ""),
                    entry_browser=client_data.get("browser", ""),
                    entry_device=client_data.get("device", ""),
                    entry_os=client_data.get("os", ""),
                    entry_referrer=client_data.get("referrer", ""),
                    entry_accept_language=client_data.get("accept_language", ""),
                    entry_client_data=client_data,
                )
        except IntegrityError:
            continue
    raise RuntimeError("Could not allocate unique RID, PID and UID identifiers")


def build_outbound_url(entry_link: str, rid: str, answers: dict) -> str:
    """Use the exact allocated entry link, replacing PID and adding trackId/profile answers."""
    parts = urlsplit(entry_link)
    query = parse_qsl(parts.query, keep_blank_values=True)
    outbound: list[tuple[str, str]] = []
    has_pid = False

    for key, value in query:
        lowered = key.lower()
        if lowered == "pid":
            outbound.append((key, rid))
            has_pid = True
        elif lowered != "trackid":
            outbound.append((key, value))

    if not has_pid:
        outbound.append(("PID", rid))
    outbound.append(("trackId", rid))

    for answer in answers.values():
        question_key = answer.get("question_key")
        upstream_values = answer.get("upstream_values") or []
        if not question_key:
            continue
        for value in upstream_values:
            outbound.append((str(question_key), str(value)))

    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(outbound), parts.fragment))


def status_identifiers_from_request(request) -> list[str]:
    """Return every distinct tracking identifier supplied by a provider.

    Some providers echo our canonical RID as ``tid``/``trackId`` while their
    field named ``rid`` contains a prescreener UID or provider PID.  Keeping all
    values lets the callback resolver find the correct attempt even when an
    upstream system includes an unrelated identifier before our own.
    """

    values = []
    for name in ("tid", "TID", "trackId", "rid", "RID", "pid", "PID", "qsid", "QSID"):
        value = request.GET.get(name)
        value = str(value or "").strip()
        if value and value not in values:
            values.append(value)
    return values


def status_rid_from_request(request) -> str:
    """Backward-compatible first callback identifier accessor."""

    values = status_identifiers_from_request(request)
    return values[0] if values else ""
