"""Cint Exchange Model 2 / Method B polling and respondent-entry adapter."""

import base64
import hashlib
import hmac
import json
import logging
import re
import time
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from prescreener_vault.cint_email_pool import assigned_email_hash
from surveys.models import Survey, SurveyQuota, TargetingQuestion
from vendors.credentials import resolve_integration_token

from .base import (
    NormalizedSurvey,
    ProviderConfigurationError,
    ProviderError,
    SurveyProvider,
    environment_value,
)


logger = logging.getLogger(__name__)


class CintProvider(SurveyProvider):
    """Cint Exchange Supply Model 2 inventory using the REST polling method."""

    code = "cint"
    label = "Cint Exchange"
    default_base_url = "https://api.samplicio.us"
    minimum_sync_interval_seconds = 60
    credential_fields = (("token", "Authorization API key"),)

    DEFINITIONS = (
        "Lookup/v1/BasicLookups/BundledLookups/"
        "CountryLanguages,Industries,SampleTypes,StudyTypes,SupplierLinkTypes,SurveyStatuses"
    )

    def __init__(self, integration, *, session=None):
        """Resolve Cint credentials, supplier identity and polling/link policy."""

        super().__init__(integration, session=session or requests.Session())
        self.api_key = resolve_integration_token(integration)
        if not self.api_key:
            raise ProviderConfigurationError(
                "Configure the Cint API key using an environment variable or encrypted credential."
            )
        self.supplier_code = str(integration.supplier_code or "").strip()
        if not re.fullmatch(r"\d{1,40}", self.supplier_code):
            raise ProviderConfigurationError("Cint requires the real numeric Supplier Code.")
        self.base_url = (integration.base_url or self.default_base_url).rstrip("/") + "/"
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "https" or parsed.hostname != "api.samplicio.us":
            raise ProviderConfigurationError(
                "Cint production Base URL must be https://api.samplicio.us."
            )
        config = integration.config or {}
        self.timeout = max(5, min(int(config.get("timeout_seconds", 45)), 120))
        self.request_wall_timeout = max(
            self.timeout,
            min(int(config.get("request_wall_timeout_seconds", 150)), 300),
        )
        self.include_open = config.get("include_open_opportunities", True) is not False
        self.include_allocated = config.get("include_allocated_surveys", True) is not False
        self.manage_supplier_links = config.get("manage_supplier_links", True) is not False
        self.create_missing_supplier_links = config.get("create_missing_supplier_links", True) is not False
        self.hash_key_env = str(config.get("hash_key_env") or "CINT_HASH_KEY").strip()
        if not self.include_open and not self.include_allocated:
            raise ProviderConfigurationError(
                "Enable at least one Cint inventory source: open opportunities or allocated surveys."
            )
        self._country_languages = {}
        self._sample_types = {}
        self._study_types = {}

    def _request(self, path, *, method="GET", payload=None, allow_not_found=False):
        """Execute one authenticated Cint call with response and wall-time guards."""

        url = urljoin(self.base_url, str(path).lstrip("/"))
        started = time.monotonic()
        try:
            headers = {"Authorization": self.api_key, "Accept": "application/json"}
            method = str(method or "GET").upper()
            if method in {"POST", "PUT"}:
                headers["Content-Type"] = "application/json"
                sender = self.session.post if method == "POST" else self.session.put
                response = sender(
                    url, headers=headers, json=payload or {}, timeout=(10, self.timeout)
                )
            else:
                response = self.session.get(
                    url, headers=headers, timeout=(10, self.timeout), stream=True
                )
            if allow_not_found and response.status_code == 404:
                return {}
            response.raise_for_status()
            if method == "GET" and hasattr(response, "iter_content"):
                body = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if time.monotonic() - started > self.request_wall_timeout:
                        response.close()
                        raise ProviderError(
                            f"Cint Exchange request exceeded {self.request_wall_timeout} seconds."
                        )
                    if chunk:
                        body.extend(chunk)
                data = json.loads(body.decode(response.encoding or "utf-8"))
            else:
                data = response.json()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f" (HTTP {status})" if status else ""
            raise ProviderError(f"Cint Exchange request failed{suffix}.") from exc
        except ValueError as exc:
            raise ProviderError("Cint Exchange returned invalid JSON.") from exc
        if not isinstance(data, dict):
            raise ProviderError("Cint Exchange returned an invalid response object.")
        result = data.get("ApiResult", data.get("ApiResultCode", 0))
        try:
            failed = int(result or 0) != 0
        except (TypeError, ValueError):
            failed = bool(result)
        if failed:
            messages = data.get("ApiMessages") or []
            message = "; ".join(str(item) for item in messages if item)[:1000]
            raise ProviderError(message or f"Cint Exchange returned API result {result}.")
        return data

    def explorer_read(self, path):
        """Run one server allow-listed Cint read for the protected API explorer."""
        return self._request(path)

    def explorer_create_supplier_link(self, survey_number):
        """Create one callback-enabled OWS link from the protected API explorer."""
        return self._request(
            f"Supply/v1/SupplierLinks/Create/{survey_number}/{self.supplier_code}",
            method="POST",
            payload=self._redirect_payload(),
        )

    @staticmethod
    def _rows(data, key):
        """Return a list-valued response member and reject non-list shapes."""

        rows = data.get(key) or []
        if not isinstance(rows, list):
            raise ProviderError(f"Cint Exchange response field {key} must be a list.")
        return [row for row in rows if isinstance(row, dict)]

    def _load_definitions(self):
        """Load country/language, sample and study lookups once per adapter."""

        data = self._request(self.DEFINITIONS)
        self._country_languages = {
            str(item.get("Id")): item
            for item in self._rows(data, "AllCountryLanguages")
            if item.get("Id") is not None
        }
        self._sample_types = {
            str(item.get("Id")): item
            for item in self._rows(data, "AllSampleTypes")
            if item.get("Id") is not None
        }
        self._study_types = {
            str(item.get("Id")): item
            for item in self._rows(data, "AllStudyTypes")
            if item.get("Id") is not None
        }
        return data

    def test_connection(self):
        """Verify API key and Supplier Code with a bounded lookup request."""

        definitions = self._load_definitions()
        inventory = self._request(f"Supply/v1/Surveys/Inventory/{self.supplier_code}")
        ids = inventory.get("SupplyAllocationSurveyIDs") or []
        return {
            "provider": self.code,
            "authenticated": True,
            "supplier_code": self.supplier_code,
            "country_languages": len(definitions.get("AllCountryLanguages") or []),
            "allocated_survey_ids": len(ids) if isinstance(ids, list) else 0,
        }

    def inventory(self):
        """Merge open opportunities and this supplier's allocated surveys."""

        self._load_definitions()
        merged = {}
        if self.include_allocated:
            data = self._request(
                f"Supply/v1/Surveys/SupplierAllocations/All/{self.supplier_code}"
            )
            for row in self._rows(data, "SupplierAllocationSurveys"):
                if row.get("SurveyNumber") is None:
                    continue
                key = str(row["SurveyNumber"])
                merged[key] = {
                    **merged.get(key, {}),
                    **row,
                    "_cint_inventory_source": "allocated",
                }
        if self.include_open:
            try:
                data = self._request(f"Supply/v1/Surveys/AllOfferwall/{self.supplier_code}")
            except ProviderError:
                if not merged:
                    raise
                logger.warning(
                    "Cint open-opportunity inventory failed; continuing with %s allocated surveys "
                    "for integration=%s.",
                    len(merged), self.integration.pk,
                    exc_info=True,
                )
            else:
                for row in self._rows(data, "Surveys"):
                    if row.get("SurveyNumber") is None:
                        continue
                    key = str(row["SurveyNumber"])
                    merged[key] = {
                        **row,
                        **merged.get(key, {}),
                        "_cint_inventory_source": (
                            "allocated" if key in merged else "open_opportunity"
                        ),
                    }
        return list(merged.values())

    @staticmethod
    def _integer(value, default=0):
        """Parse a provider scalar into an integer without raising."""

        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _decimal(value):
        """Parse a provider money/rate scalar into Decimal or ``None``."""

        if isinstance(value, dict):
            value = value.get("Value", value.get("value"))
        if value in (None, ""):
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None

    @staticmethod
    def _microsoft_datetime(value):
        """Parse Cint's Microsoft JSON date representation when present."""

        match = re.search(r"/Date\((\d+)(?:[+-]\d{4})?\)/", str(value or ""))
        if not match:
            return None
        try:
            return datetime.fromtimestamp(int(match.group(1)) / 1000, tz=dt_timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None

    @staticmethod
    def _same_supplier(left, right):
        """Compare supplier identifiers after string normalization."""

        left = str(left or "").strip()
        right = str(right or "").strip()
        return left == right or left.lstrip("0") == right.lstrip("0")

    def _supplier_allocation(self, payload):
        """Select this integration's allocation from a Cint survey payload."""

        candidates = []
        for field in ("SupplierAllocations", "OfferwallAllocations"):
            candidates.extend(item for item in payload.get(field) or [] if isinstance(item, dict))
        return next(
            (
                item
                for item in candidates
                if self._same_supplier(item.get("SupplierCode"), self.supplier_code)
            ),
            candidates[0] if candidates else {},
        )

    @staticmethod
    def _supplier_link(payload, allocation=None):
        """Extract a usable supplier-link object from known Cint envelopes."""

        allocation = allocation or {}
        candidates = [
            payload.get("SupplierLink"), payload.get("Target"), payload.get("TargetModel"),
            allocation.get("Target"), allocation.get("TargetModel"),
        ]
        return next((item for item in candidates if isinstance(item, dict)), {})

    def _country_language(self, identifier):
        """Expand CountryLanguageID into normalized market/language fields."""

        item = self._country_languages.get(str(identifier), {})
        code = str(item.get("Code") or "").strip().upper()
        name = str(item.get("Name") or "").strip()
        code_parts = [part for part in re.split(r"[-_]", code) if part]
        name_parts = [part.strip() for part in name.split(" - ", 1)]
        language = name_parts[0] if name_parts else ""
        country = name_parts[1] if len(name_parts) > 1 else name
        return {
            "country": country,
            "country_code": code_parts[-1] if len(code_parts) > 1 else "",
            "language": language,
            "language_code": code_parts[0] if len(code_parts) > 1 else "",
        }

    def _survey_type(self, sample_type_id):
        """Normalize Cint sample type into the platform's B2B/B2C label."""

        item = self._sample_types.get(str(sample_type_id), {})
        value = str(item.get("Code") or item.get("Name") or "").strip()
        compact = re.sub(r"[^A-Z0-9]", "", value.upper())
        if "BUSINESS" in compact or compact == "B2B":
            return "B2B"
        if "CONSUMER" in compact or compact == "B2C":
            return "B2C"
        return value[:20]

    def normalize_inventory_item(self, payload, seen_at):
        """Convert one Cint inventory row into the provider-neutral survey DTO."""

        source_key = str(payload.get("SurveyNumber") or "").strip()
        if not source_key.isdigit():
            raise ProviderError("Cint inventory item is missing a numeric SurveyNumber.")
        allocation = self._supplier_allocation(payload)
        target = self._supplier_link(payload, allocation)
        remaining_known = "TotalRemaining" in payload or any(
            key in allocation for key in ("AllocationRemaining", "HedgeRemaining")
        )
        remaining = self._integer(payload.get("TotalRemaining"), -1)
        if remaining < 0 and remaining_known:
            remaining = max(
                0,
                self._integer(allocation.get("AllocationRemaining"))
                + self._integer(allocation.get("HedgeRemaining")),
            )
        completes_known = "OverallCompletes" in payload or any(
            key in allocation for key in ("AchievedCompletes", "OfferwallCompletes")
        )
        completes = self._integer(
            payload.get("OverallCompletes"),
            self._integer(
                allocation.get("AchievedCompletes"),
                self._integer(allocation.get("OfferwallCompletes")),
            ),
        )
        cpi = self._decimal(payload.get("RPI")) or self._decimal(target.get("RPI"))
        country_language = self._country_language(payload.get("CountryLanguageID"))
        sample_type = self._sample_types.get(str(payload.get("SampleTypeID")), {})
        study_type = self._study_types.get(str(payload.get("StudyTypeID")), {})
        raw_data = dict(payload)
        raw_data["_cint_country_language"] = {
            "id": payload.get("CountryLanguageID"),
            **country_language,
        }
        raw_data["_cint_sample_type"] = sample_type
        raw_data["_cint_study_type"] = study_type
        values = {
            "company_name": self.integration.client.name,
            "name": str(payload.get("SurveyName") or f"Cint survey {source_key}"),
            "status": Survey.Status.LIVE,
            "starts": 0,
            "loi": max(
                0,
                self._integer(
                    payload.get("LengthOfInterview"),
                    self._integer(payload.get("BidLengthOfInterview")),
                ),
            ),
            "incidence_rate": self._decimal(payload.get("BidIncidence")),
            **country_language,
            "group_type": str(study_type.get("Name") or study_type.get("Code") or ""),
            "buyer_id": str(payload.get("AccountName") or "").strip(),
            "survey_type": self._survey_type(payload.get("SampleTypeID")),
            "device_type": "",
            "entry_link": str(target.get("LiveLink") or target.get("LiveSupplierLink") or "").strip(),
            "test_entry_link": str(target.get("TestLink") or target.get("TestSupplierLink") or "").strip(),
            "job_category": str(payload.get("IndustryID") or ""),
            "has_quota": True,
            "is_pii_required": bool(payload.get("CollectsPII")),
            "is_recontact": bool(payload.get("RespondentPIDs")),
            "source_created_at": self._microsoft_datetime(payload.get("FieldBeginDate")),
            "source_modified_at": None,
            "last_seen_at": seen_at,
            "raw_data": raw_data,
        }
        if remaining_known:
            values["remaining"] = max(0, remaining)
        if completes_known:
            values["completes"] = max(0, completes)
        if remaining_known or completes_known:
            values["sample_size"] = max(0, completes if completes_known else 0) + max(
                0, remaining if remaining_known else 0
            )
        if cpi is not None:
            values["cpi"] = cpi
        return NormalizedSurvey(
            source_key=source_key,
            numeric_source_id=int(source_key),
            modified_at=None,
            raw_data=raw_data,
            values=values,
        )

    def _question_library(self, country_language_id):
        """Return localized question metadata keyed by numeric QuestionID."""

        data = self._request(
            f"Lookup/v1/QuestionLibrary/AllQuestions/{country_language_id}"
        )
        return {
            self._integer(item.get("QuestionID"), -1): item
            for item in self._rows(data, "Questions")
            if self._integer(item.get("QuestionID"), -1) >= 0
        }

    def _question_metadata(self, country_language_id, question_id, library):
        """Use the library row or fetch a missing question individually."""

        if question_id in library:
            return library[question_id]
        data = self._request(
            f"Lookup/v1/QuestionLibrary/QuestionById/{country_language_id}/{question_id}",
            allow_not_found=True,
        )
        return data.get("Question") if isinstance(data.get("Question"), dict) else {}

    def _question_options(self, country_language_id, question_id):
        """Fetch localized option/PreCode rows for one question."""

        data = self._request(
            f"Lookup/v1/QuestionLibrary/AllQuestionOptions/{country_language_id}/{question_id}",
            allow_not_found=True,
        )
        return self._rows(data, "QuestionOptions") if data else []

    @staticmethod
    def _conditions(rows):
        """Normalize Cint qualification/quota rows into question constraints."""

        conditions = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            question_id = CintProvider._integer(row.get("QuestionID"), -1)
            if question_id < 0:
                continue
            conditions.append({
                "question_id": question_id,
                "operator": str(row.get("LogicalOperator") or "OR").upper(),
                "precodes": [str(value) for value in row.get("PreCodes") or []],
                "raw": row,
            })
        return conditions

    @staticmethod
    def _numeric_ranges(values):
        """Compress qualifying integer values into readable inclusive ranges."""

        numbers = sorted({
            int(value)
            for value in values
            if str(value).strip().isdigit() and 0 <= int(value) <= 125
        })
        if not numbers:
            return []
        ranges = []
        start = previous = numbers[0]
        for number in numbers[1:]:
            if number == previous + 1:
                previous = number
                continue
            ranges.append({"min": start, "max": previous})
            start = previous = number
        ranges.append({"min": start, "max": previous})
        return ranges

    def refresh_details(self, survey):
        """Hydrate qualifications, quotas, hints, mappings and supplier link."""

        survey_number = survey.source_key
        country_language_id = self._integer(
            (survey.raw_data or {}).get("CountryLanguageID"), -1
        )
        if country_language_id < 0:
            raise ProviderError("Cint survey has no CountryLanguageID for question lookup.")

        qualification_data = self._request(
            f"Supply/v1/SurveyQualifications/BySurveyNumberForOfferwall/{survey_number}"
        )
        quota_data = self._request(
            f"Supply/v1/SurveyQuotas/BySurveyNumber/{survey_number}/{self.supplier_code}"
        )
        qualification = qualification_data.get("SurveyQualification") or {}
        qualification_conditions = self._conditions(qualification.get("Questions"))
        quotas = self._rows(quota_data, "SurveyQuotas")
        quota_conditions = [
            condition
            for quota in quotas
            for condition in self._conditions(quota.get("Questions"))
        ]
        question_ids = sorted({
            item["question_id"] for item in qualification_conditions + quota_conditions
        })
        library = self._question_library(country_language_id)
        metadata = {}
        options = {}
        for question_id in question_ids:
            metadata[question_id] = self._question_metadata(
                country_language_id, question_id, library
            )
            options[question_id] = self._question_options(
                country_language_id, question_id
            )

        merged_qualifications = {}
        for condition in qualification_conditions:
            current = merged_qualifications.setdefault(condition["question_id"], {
                **condition,
                "precodes": [],
                "raw_conditions": [],
            })
            current["precodes"] = list(dict.fromkeys(
                current["precodes"] + condition["precodes"]
            ))
            current["raw_conditions"].append(condition["raw"])

        question_rows = []
        for condition in merged_qualifications.values():
            question_id = condition["question_id"]
            question = metadata.get(question_id) or {}
            accepted = set(condition["precodes"])
            question_name = str(question.get("Name") or "").strip()
            question_text = str(question.get("QuestionText") or "").strip()
            normalized_name = re.sub(r"[^A-Z0-9]", "", question_name.upper())
            is_age_question = (
                question_id == 42
                or normalized_name == "AGE"
                or re.search(r"\b(?:your\s+)?age\b", question_text, re.IGNORECASE)
            )
            option_rows = [
                {
                    "OptionId": str(option.get("Precode")),
                    "OptionText": str(option.get("OptionText") or option.get("Precode")),
                    "ParentItemText": option.get("ParentItemText"),
                }
                for option in options.get(question_id, [])
                if option.get("Precode") is not None
            ]
            if not option_rows:
                option_rows = [
                    {"OptionId": precode, "OptionText": precode}
                    for precode in condition["precodes"]
                ]
            question_rows.append(TargetingQuestion(
                survey=survey,
                question_id=question_id,
                key=question_name or f"CINT_Q_{question_id}",
                text=question_text or f"Cint question {question_id}",
                question_type=str(question.get("QuestionType") or "Qualification"),
                category="Cint qualification",
                options=option_rows,
                raw_data={
                    "provider": "cint",
                    "logical_operator": condition["operator"],
                    "targeting_choices": sorted(accepted),
                    "targeting_age_ranges": (
                        self._numeric_ranges(accepted) if is_age_question else []
                    ),
                    "qualification": condition["raw_conditions"],
                },
            ))

        def readable_condition(condition):
            question_id = condition["question_id"]
            question = metadata.get(question_id) or {}
            labels = {
                str(option.get("Precode")): str(
                    option.get("OptionText") or option.get("Precode")
                )
                for option in options.get(question_id, [])
                if option.get("Precode") is not None
            }
            return {
                "name": str(question.get("QuestionText") or question.get("Name") or question_id),
                "values": [labels.get(value, value) for value in condition["precodes"]],
                "operator": condition["operator"],
            }

        quota_rows = []
        for index, quota in enumerate(quotas):
            quota_id = self._integer(quota.get("SurveyQuotaID"), 0)
            quota_type = str(quota.get("SurveyQuotaType") or "Client")
            remaining = max(0, self._integer(quota.get("NumberOfRespondents")))
            conditions = self._conditions(quota.get("Questions"))
            live = quota_data.get("SurveyStillLive") is not False
            quota_rows.append(SurveyQuota(
                survey=survey,
                source_key=str(quota_id or f"cint-{index}"),
                quota_id=quota_id or None,
                title=f"{quota_type} quota",
                name=f"{quota_type} quota",
                sample_size=0,
                completes=0,
                remaining=remaining,
                status="Closed" if not live else "Full" if remaining == 0 else "Open",
                targeting={"questions": quota.get("Questions") or []},
                raw_data={
                    **quota,
                    "provider": "cint",
                    "quotaLimitBy": "completes",
                    "_target_known": False,
                    "_completed_known": False,
                    "targeting_details": [readable_condition(item) for item in conditions],
                },
            ))

        total_quota = next(
            (
                item for item in quotas
                if str(item.get("SurveyQuotaType") or "").lower() == "total"
            ),
            quotas[0] if quotas else {},
        )
        total_remaining = max(0, self._integer(total_quota.get("NumberOfRespondents")))
        quota_cpi = self._decimal(total_quota.get("RPI"))
        now = timezone.now()
        with transaction.atomic():
            survey.targeting_questions.all().delete()
            survey.quotas.all().delete()
            TargetingQuestion.objects.bulk_create(question_rows)
            SurveyQuota.objects.bulk_create(quota_rows)
            survey.has_quota = bool(quota_rows)
            if quotas:
                survey.remaining = total_remaining
            if quota_cpi is not None:
                survey.cpi = quota_cpi
            survey.status = (
                Survey.Status.CLOSED
                if quota_data.get("SurveyStillLive") is False
                else Survey.Status.LIVE
            )
            survey.targeting_synced_at = now
            survey.quota_synced_at = now
            survey.detail_synced_at = now
            survey.save(update_fields=[
                "has_quota", "remaining", "cpi", "status", "targeting_synced_at",
                "quota_synced_at", "detail_synced_at", "updated_at",
            ])
        from surveys.mappings import sync_survey_mappings
        sync_survey_mappings(survey)

        if self.manage_supplier_links:
            self.ensure_supplier_link(survey)

    def _redirect_payload(self):
        """Build OWS callbacks that return Cint MID into the platform RID route."""

        base = str(settings.PUBLIC_APP_BASE_URL or "").rstrip("/")
        if not base:
            raise ProviderConfigurationError(
                "Set PUBLIC_APP_BASE_URL before Cint can create callback-enabled supplier links."
            )
        parsed = urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProviderConfigurationError("PUBLIC_APP_BASE_URL must be an absolute HTTP(S) URL.")
        callback = f"{base}/survey"
        return {
            "SupplierLinkTypeCode": "OWS",
            "TrackingTypeCode": "NONE",
            "DefaultLink": f"{callback}?status=2&rid=[%MID%]",
            "SuccessLink": f"{callback}?status=1&rid=[%MID%]",
            "FailureLink": f"{callback}?status=2&rid=[%MID%]",
            "OverQuotaLink": f"{callback}?status=3&rid=[%MID%]",
            "QualityTerminationLink": f"{callback}?status=4&rid=[%MID%]",
        }

    def redirect_contract_fingerprint(self):
        """Identify the exact supplier redirect contract configured upstream."""

        payload = self._redirect_payload()
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def update_supplier_link_redirects(self, survey):
        """Configure callbacks for one Cint survey using its real supplier code.

        This method is intentionally invoked by background inventory work rather
        than the browser copy action. That keeps copy-link latency independent of
        Cint and lets a per-integration lease serialize large update batches.
        """

        # Update operates on a supplier-specific link. Open opportunities may
        # reach inventory before that link exists, so create/retrieve it once
        # before applying the explicit redirect contract.
        if not survey.entry_link:
            self.ensure_supplier_link(survey)
        payload = self._redirect_payload()
        result = self._request(
            f"Supply/v1/SupplierLinks/Update/{survey.source_key}/{self.supplier_code}",
            method="PUT",
            payload=payload,
        )
        raw_data = dict(survey.raw_data or {})
        raw_data["_cint_redirect_contract"] = self.redirect_contract_fingerprint()
        raw_data["_cint_redirect_synced_at"] = timezone.now().isoformat()
        raw_data["_cint_redirect_supplier_code"] = self.supplier_code
        link = self._supplier_link(result)
        live_link = str(link.get("LiveLink") or "").strip() if link else ""
        test_link = str(link.get("TestLink") or "").strip() if link else ""
        update_fields = ["raw_data", "updated_at"]
        survey.raw_data = raw_data
        if live_link:
            survey.entry_link = live_link
            update_fields.append("entry_link")
        if test_link:
            survey.test_entry_link = test_link
            update_fields.append("test_entry_link")
        survey.save(update_fields=update_fields)
        return result

    def ensure_supplier_link(self, survey):
        """Retrieve the supplier link, creating an OWS link only when it is missing."""
        if survey.entry_link:
            return survey.entry_link
        path = f"Supply/v1/SupplierLinks/BySurveyNumber/{survey.source_key}/{self.supplier_code}"
        result = self._request(path, allow_not_found=True)
        link = self._supplier_link(result)
        if not link and self.create_missing_supplier_links:
            link = self._supplier_link(self._request(
                f"Supply/v1/SupplierLinks/Create/{survey.source_key}/{self.supplier_code}",
                method="POST",
                payload=self._redirect_payload(),
            ))
        live_link = str(link.get("LiveLink") or "").strip() if link else ""
        test_link = str(link.get("TestLink") or "").strip() if link else ""
        if live_link:
            survey.entry_link = live_link
            survey.test_entry_link = test_link
            raw_data = dict(survey.raw_data or {})
            raw_data["_cint_supplier_link"] = {
                key: value for key, value in (link or {}).items()
                if key not in {"DefaultLink", "SuccessLink", "FailureLink", "OverQuotaLink", "QualityTerminationLink"}
            }
            survey.raw_data = raw_data
            survey.save(update_fields=["entry_link", "test_entry_link", "raw_data", "updated_at"])
        return live_link

    @staticmethod
    def _entry_signature(unsigned_url, key):
        """Return Cint's URL-safe HMAC-SHA1 signature for the exact unsigned URL."""

        if not unsigned_url.endswith("&"):
            raise ProviderConfigurationError("Cint hash input must include the trailing ampersand.")
        digest = hmac.new(
            key.encode("utf-8"), unsigned_url.encode("utf-8"), hashlib.sha1
        ).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def build_outbound_url(self, survey, attempt, answers):
        """Send UID as PID, RID as MID, email hash and a signed answer payload."""

        live_link = survey.entry_link or self.ensure_supplier_link(survey)
        if not live_link:
            raise ProviderConfigurationError("Cint has not returned a live supplier link for this survey.")
        parsed = urlsplit(live_link)
        hostname = (parsed.hostname or "").lower()
        if hostname != "samplicio.us" and not hostname.endswith(".samplicio.us"):
            raise ProviderConfigurationError("Cint returned an unexpected supplier-link hostname.")
        pid = str(attempt.prescreener_uid or "").strip()
        if not pid:
            raise ProviderConfigurationError("Cint requires the pre-screener UID as persistent PID.")
        email_digest = assigned_email_hash(pid, attempt.rid)
        query = [
            (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in {"pid", "mid", "hash", "cint_email"}
        ]
        query.extend([
            ("PID", pid),
            ("MID", attempt.rid),
            ("cint_email", email_digest),
        ])
        for answer in answers.values():
            question_id = answer.get("question_id")
            if question_id in (None, ""):
                continue
            for value in answer.get("upstream_values") or []:
                query.append((str(question_id), str(value)))
        unsigned_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), "")) + "&"
        signature = self._entry_signature(unsigned_url, environment_value(self.hash_key_env, "Cint hash key"))
        signed_url = f"{unsigned_url}hash={signature}"
        if len(signed_url) > 1999:
            raise ProviderConfigurationError("Cint respondent entry link exceeds the 1999-character limit.")
        return signed_url
