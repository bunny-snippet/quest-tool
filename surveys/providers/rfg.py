import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from django.db import transaction
from django.utils import timezone

from surveys.models import Survey, SurveyQuota, TargetingQuestion

from .base import (
    NormalizedSurvey,
    ProviderConfigurationError,
    ProviderError,
    SurveyProvider,
    environment_value,
)


class ResearchForGoodProvider(SurveyProvider):
    code = "rfg"
    label = "Research For Good"
    default_base_url = "https://api.researchforgood.com/API"
    minimum_sync_interval_seconds = 600
    credential_fields = (("apid", "APID environment key"), ("secret", "Secret environment key"))

    def __init__(self, integration, *, session=None, clock=None):
        super().__init__(integration, session=session or requests.Session())
        refs = integration.credential_env_keys or {}
        self.apid = environment_value(refs.get("apid"), "RFG apid")
        self.secret = environment_value(refs.get("secret"), "RFG secret")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", self.secret):
            raise ProviderConfigurationError("RFG secret must resolve to a 32-character hexadecimal value.")
        # The documentation links to /API/, but the live endpoint returns 404
        # for that path. RFG accepts signed POST requests at /API exactly.
        self.base_url = (integration.base_url or self.default_base_url).rstrip("/")
        parsed_base = urlsplit(self.base_url)
        if (
            parsed_base.scheme != "https"
            or parsed_base.hostname != "api.researchforgood.com"
            or parsed_base.path != "/API"
            or parsed_base.query
            or parsed_base.fragment
        ):
            raise ProviderConfigurationError("RFG base URL must be https://api.researchforgood.com/API.")
        self.timeout = int((integration.config or {}).get("timeout_seconds", 30))
        self.clock = clock or time.time

    def _command(self, payload: dict) -> dict:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        timestamp = str(int(self.clock()))
        signature = hmac.new(
            bytes.fromhex(self.secret),
            f"{timestamp}{body}".encode("utf-8"),
            hashlib.sha1,
        ).hexdigest()
        try:
            response = self.session.post(
                self.base_url,
                params={"apid": self.apid, "time": timestamp, "hash": signature},
                data=body.encode("utf-8"),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            # Requests exceptions often include the fully signed URL. Never copy
            # that URL (APID/hash) into API responses or persistent audit logs.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f" (HTTP {status})" if status else ""
            raise ProviderError(f"Research For Good request failed{suffix}.") from exc
        except ValueError as exc:
            raise ProviderError("Research For Good returned invalid JSON.") from exc
        if not isinstance(data, dict):
            raise ProviderError("Research For Good returned an invalid JSON response.")
        if data.get("result") != 0:
            raise ProviderError(str(data.get("message") or f"Research For Good result={data.get('result')}"))
        result = data.get("response") or {}
        if not isinstance(result, dict):
            raise ProviderError("Research For Good response payload must be an object.")
        return result

    def test_connection(self) -> dict:
        marker = f"quest-tool-{int(self.clock())}"
        response = self._command({"command": "test/copy/1", "marker": marker})
        return {"provider": self.code, "authenticated": True, "echo_received": response.get("marker") == marker}

    def inventory(self) -> list[dict]:
        config = self.integration.config or {}
        command = {"command": "livealert/inventory/1", "allowRecontacts": bool(config.get("allow_recontacts", False)), "type": 1}
        if config.get("country"):
            command["country"] = str(config["country"]).upper()
        if config.get("category") in {"B2B", "B2C"}:
            command["category"] = config["category"]
        projects = self._command(command).get("projects") or []
        if not isinstance(projects, list):
            raise ProviderError("Research For Good inventory projects must be a list.")
        return [row for row in projects if isinstance(row, dict) and row.get("rfg_id")]

    @staticmethod
    def _datetime(value):
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.replace(tzinfo=dt_timezone.utc) if parsed.tzinfo is None else parsed.astimezone(dt_timezone.utc)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _money(value):
        try:
            cleaned = re.sub(r"[^0-9.\-]", "", str(value or ""))
            return Decimal(cleaned) if cleaned else None
        except (InvalidOperation, ValueError):
            return None

    def normalize_inventory_item(self, payload, seen_at):
        desired = max(0, int(payload.get("desiredCompletes") or 0))
        completed = max(0, int(payload.get("currentCompletes") or 0))
        state = int(payload.get("state") or 0)
        modified = self._datetime(payload.get("lastModified"))
        phone = int(payload.get("phoneSupported") or 0)
        tablet = int(payload.get("tabletSupported") or 0)
        devices = ["Desktop"]
        if phone == 1:
            devices.append("Mobile")
        if tablet == 1:
            devices.append("Tablet")
        return NormalizedSurvey(
            source_key=str(payload["rfg_id"]),
            numeric_source_id=None,
            modified_at=modified,
            raw_data=payload,
            values={
                "company_name": self.integration.client.name,
                "name": str(payload.get("title") or ""),
                "status": Survey.Status.LIVE if state == 2 else Survey.Status.CLOSED,
                "sample_size": desired,
                "completes": completed,
                "remaining": max(0, desired - completed),
                "cpi": self._money(payload.get("cpi")),
                "loi": max(0, int(payload.get("estimatedLOI") or 0)),
                "incidence_rate": self._money(payload.get("estimatedIR")),
                "country": str(payload.get("country") or "").upper(),
                "country_code": str(payload.get("country") or "").upper(),
                "device_type": ", ".join(devices),
                "job_category": str(payload.get("category") or ""),
                "is_pii_required": bool(payload.get("collectsPII")),
                "is_recontact": bool(payload.get("isRecontact")),
                "source_modified_at": modified,
                "last_seen_at": seen_at,
                "raw_data": payload,
            },
        )

    def targeting(self, source_key):
        return self._command({"command": "livealert/targeting/1", "rfg_id": source_key, "zipsOnly": False})

    def datapoint(self, name):
        return self._command({"command": "livealert/datapoint/1", "name": name})

    def create_link(self, source_key):
        return str(self._command({"command": "livealert/createLink/1", "rfg_id": source_key}).get("link") or "")

    @staticmethod
    def _question_id(value):
        return -int(hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:15], 16)

    def refresh_details(self, survey):
        targeting = self.targeting(survey.source_key)
        datapoints = targeting.get("datapoints") if isinstance(targeting.get("datapoints"), list) else []
        questions = [
            TargetingQuestion(survey=survey, question_id=self._question_id("rfg-birthday"), key="RFG_BIRTHDAY", text="What is your date of birth?", question_type="date", category="Required profile", options=[], raw_data={"mandatory_link_parameter": "birthday"}),
            TargetingQuestion(survey=survey, question_id=self._question_id("rfg-gender"), key="RFG_GENDER", text="What is your gender?", question_type="single", category="Required profile", options=[{"OptionId": "M", "OptionText": "Male"}, {"OptionId": "F", "OptionText": "Female"}], raw_data={"mandatory_link_parameter": "gender"}),
            TargetingQuestion(survey=survey, question_id=self._question_id("rfg-postal"), key="RFG_POSTAL_CODE", text="What is your postal code?", question_type="text", category="Required profile", options=[], raw_data={"mandatory_link_parameter": "postalCode"}),
        ]
        for target in datapoints:
            if not isinstance(target, dict) or not target.get("name") or target.get("name") in {"Age", "Gender"}:
                continue
            metadata = self.datapoint(target["name"])
            question_type = int(metadata.get("type") or 0)
            if question_type in {13, 15, 16, 17, 18}:
                continue
            locale = str((self.integration.config or {}).get("locale", "en-US"))
            question_texts = metadata.get("question") if isinstance(metadata.get("question"), dict) else {}
            answers = metadata.get("answers") if isinstance(metadata.get("answers"), list) else []
            allowed = {int(item["choice"]) for item in target.get("values", []) if isinstance(item, dict) and str(item.get("choice", "")).isdigit()}
            options = []
            for index, answer in enumerate(answers):
                if index == 0 or not isinstance(answer, dict) or (allowed and index not in allowed):
                    continue
                options.append({"OptionId": index, "OptionText": answer.get(locale) or answer.get("en-US") or f"Choice {index}"})
            questions.append(TargetingQuestion(
                survey=survey,
                question_id=self._question_id(metadata.get("property") or target["name"]),
                key=str(metadata.get("property") or target["name"]),
                text=str(question_texts.get(locale) or question_texts.get("en-US") or target["name"]),
                question_type="multi" if question_type == 1 else "single",
                category="RFG targeting",
                options=options,
                raw_data={"targeting": target, "datapoint": metadata},
            ))
        quotas = targeting.get("quotas") if isinstance(targeting.get("quotas"), list) else []
        quota_rows = []
        for index, quota in enumerate(quotas):
            if not isinstance(quota, dict):
                continue
            remaining = quota.get("completesLeft", quota.get("startsLeft", 0))
            key = hashlib.sha256(json.dumps(quota, sort_keys=True, default=str).encode()).hexdigest()
            quota_rows.append(SurveyQuota(
                survey=survey,
                source_key=key,
                title=f"Quota {index + 1}",
                name=str(quota.get("quotaLimitBy") or "RFG quota"),
                remaining=max(0, int(remaining or 0)),
                status="Throttled" if quota.get("quotaThrottle") == 1 else "Open",
                targeting={"datapoints": quota.get("datapoints") or []},
                raw_data=quota,
            ))
        link = survey.entry_link or self.create_link(survey.source_key)
        now = timezone.now()
        with transaction.atomic():
            survey.targeting_questions.all().delete()
            survey.quotas.all().delete()
            TargetingQuestion.objects.bulk_create(questions)
            SurveyQuota.objects.bulk_create(quota_rows)
            survey.entry_link = link
            survey.has_quota = bool(quota_rows)
            survey.targeting_synced_at = now
            survey.quota_synced_at = now
            survey.detail_synced_at = now
            survey.save(update_fields=["entry_link", "has_quota", "targeting_synced_at", "quota_synced_at", "detail_synced_at", "updated_at"])

    def duplicate_check(self, survey, attempt, ip_address):
        response = self._command({"command": "livealert/duplicateCheck/1", "rfg_id": survey.source_key, "fingerprint": 0, "rid": attempt.rid, "ip": ip_address or ""})
        return bool(response.get("isDuplicate"))

    @staticmethod
    def _answer_map(answers):
        return {str(item.get("question_key") or ""): item.get("upstream_values") or item.get("values") or [] for item in answers.values()}

    def build_outbound_url(self, survey, attempt, answers):
        values = self._answer_map(answers)
        birthday = (values.get("RFG_BIRTHDAY") or [""])[0]
        gender = (values.get("RFG_GENDER") or [""])[0]
        postal = (values.get("RFG_POSTAL_CODE") or [""])[0]
        try:
            datetime.strptime(str(birthday), "%Y-%m-%d")
        except ValueError as exc:
            raise ProviderError("Date of birth must use YYYY-MM-DD format.") from exc
        if str(gender).upper() not in {"M", "F", "1", "2"}:
            raise ProviderError("Select a valid gender for Research For Good.")
        if not postal:
            raise ProviderError("Postal code is required for Research For Good.")
        parts = urlsplit(survey.entry_link)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.update({
            "rid": attempt.rid,
            "country": survey.country_code,
            "postalCode": postal,
            "gender": str(gender).upper(),
            "birthday": birthday,
            "integration": str(self.integration.pk),
            "code": survey.local_id,
        })
        for key, selected in values.items():
            if key.startswith("RFG_") or not selected:
                continue
            query[key] = ",".join(str(value) for value in selected)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
