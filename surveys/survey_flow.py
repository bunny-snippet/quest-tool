import secrets
import string
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.db import IntegrityError, transaction

from .models import Survey, SurveyAttempt


RID_ALPHABET = string.ascii_letters + string.digits


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


def get_request_ip(request) -> str | None:
    if settings.TRUST_X_FORWARDED_FOR:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


def supplier_code_from_entry_link(entry_link: str) -> str:
    query = dict(parse_qsl(urlsplit(entry_link).query, keep_blank_values=True))
    return str(query.get("supCode") or query.get("supplierCode") or "")


def create_attempt(survey: Survey, user_id: str, ip_address: str | None) -> SurveyAttempt:
    for _ in range(10):
        try:
            with transaction.atomic():
                return SurveyAttempt.objects.create(
                    rid=generate_rid(),
                    survey=survey,
                    user_id=user_id,
                    supplier_code=supplier_code_from_entry_link(survey.entry_link),
                    initiation_ip=ip_address,
                )
        except IntegrityError:
            continue
    raise RuntimeError("Could not allocate a unique RID")


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


def status_rid_from_request(request) -> str:
    for name in ("rid", "RID", "pid", "PID", "qsid", "QSID", "trackId"):
        value = request.GET.get(name)
        if value:
            return value.strip()
    return ""

