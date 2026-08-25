"""PureSpectrum Fusion Match inventory and respondent routing adapter."""

import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import requests
from django.db import transaction
from django.utils import timezone

from surveys.models import Survey, TargetingQuestion

from .base import (
    NormalizedSurvey,
    ProviderConfigurationError,
    ProviderError,
    SurveyProvider,
    environment_value,
)


class PureSpectrumProvider(SurveyProvider):
    """Expose the three approved Fusion Match markets as project inventory."""

    code = "purespectrum"
    label = "PureSpectrum Fusion Match"
    default_base_url = "https://fusionapi.spectrumsurveys.com/surveys/fusionMatch"
    minimum_sync_interval_seconds = 60
    credential_fields = (("token", "Access-token environment key"),)
    localizations = ("en_US", "en_IN", "en_GB")
    market_codes = {
        "en_US": ("US", "United States"),
        "en_IN": ("IN", "India"),
        "en_GB": ("GB", "United Kingdom"),
    }
    respondent_placeholder = "[RID]"
    max_surveys = 200

    def __init__(self, integration, *, session=None):
        super().__init__(integration, session=session or requests.Session())
        reference = str(
            integration.credential_env_key
            or (integration.credential_env_keys or {}).get("token")
            or ""
        ).strip()
        self.access_token = environment_value(reference, "PureSpectrum access token")
        self.base_url = (integration.base_url or self.default_base_url).rstrip("/")
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "fusionapi.spectrumsurveys.com"
            or parsed.path.rstrip("/") != "/surveys/fusionMatch"
            or parsed.query
            or parsed.fragment
        ):
            raise ProviderConfigurationError(
                "PureSpectrum base URL must be "
                "https://fusionapi.spectrumsurveys.com/surveys/fusionMatch."
            )
        try:
            self.timeout = int((integration.config or {}).get("timeout_seconds", 30))
        except (TypeError, ValueError) as exc:
            raise ProviderConfigurationError(
                "PureSpectrum timeout_seconds must be a whole number."
            ) from exc
        if not 1 <= self.timeout <= 120:
            raise ProviderConfigurationError(
                "PureSpectrum timeout_seconds must be between 1 and 120."
            )

    def _fetch_locale(self, localization):
        """Call one approved market without ever adding a memberId parameter."""

        if localization not in self.localizations:
            raise ProviderConfigurationError(
                "PureSpectrum localization must be en_US, en_IN, or en_GB."
            )
        try:
            response = self.session.get(
                self.base_url,
                params={
                    "respondentId": self.respondent_placeholder,
                    "respondentLocalization": localization,
                    "maxNumberOfSurveysReturned": self.max_surveys,
                },
                headers={
                    "access-token": self.access_token,
                    "Accept": "application/json",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f" (HTTP {status})" if status else ""
            raise ProviderError(f"PureSpectrum request failed{suffix}.") from exc
        except ValueError as exc:
            raise ProviderError("PureSpectrum returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise ProviderError("PureSpectrum returned an invalid JSON response.")
        surveys = payload.get("surveys")
        if not isinstance(surveys, list):
            raise ProviderError("PureSpectrum response field 'surveys' must be a list.")
        rows = []
        for survey in surveys:
            if not isinstance(survey, dict) or survey.get("surveyId") in (None, ""):
                continue
            rows.append({**survey, "_respondentLocalization": localization})
        return rows

    def test_connection(self):
        """Verify the token and documented Fusion Match response contract."""

        surveys = self._fetch_locale(self.localizations[0])
        return {
            "provider": self.code,
            "authenticated": True,
            "localization": self.localizations[0],
            "records_visible": len(surveys),
        }

    def inventory(self):
        """Fetch only en_US, en_IN and en_GB, using three explicit calls."""

        return [
            survey
            for localization in self.localizations
            for survey in self._fetch_locale(localization)
        ]

    @staticmethod
    def _decimal(value):
        try:
            return Decimal(str(value)) if value not in (None, "") else None
        except (InvalidOperation, TypeError, ValueError):
            return None

    @staticmethod
    def _integer(value):
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def normalize_inventory_item(self, payload, seen_at):
        """Convert one locale-specific Fusion survey into a Projects row."""

        localization = str(payload.get("_respondentLocalization") or "")
        if localization not in self.localizations:
            raise ProviderError("PureSpectrum inventory row has an unsupported localization.")
        survey_id = str(payload["surveyId"])
        country_code, country_name = self.market_codes[localization]
        match_type = str(payload.get("fullOrPartialMatch") or "").strip()
        entry_link = str(payload.get("entryLink") or "").strip()
        return NormalizedSurvey(
            source_key=f"{localization}:{survey_id}",
            numeric_source_id=None,
            modified_at=None,
            raw_data=dict(payload),
            values={
                "company_name": self.integration.client.name,
                "name": str(payload.get("surveyName") or f"PureSpectrum {survey_id}"),
                "status": Survey.Status.LIVE if entry_link else Survey.Status.CLOSED,
                "sample_size": 0,
                "completes": 0,
                "remaining": 0,
                "cpi": self._decimal(payload.get("cpi")),
                "loi": self._integer(payload.get("estimatedLoi")),
                "incidence_rate": self._decimal(payload.get("ir")),
                "country": country_name,
                "country_code": country_code,
                "language": "English",
                "language_code": "EN",
                "group_type": match_type,
                "survey_type": match_type[:20],
                "device_type": "Desktop, Mobile, Tablet",
                "entry_link": entry_link,
                "source_modified_at": None,
                "last_seen_at": seen_at,
                "raw_data": dict(payload),
            },
        )

    def refresh_details(self, survey):
        """Install the minimal local pre-screener used before RID substitution."""

        questions = [
            TargetingQuestion(
                survey=survey,
                question_id=212,
                key="AGE",
                text="What is your age?",
                question_type="Numeric",
                category="Required profile",
                options=[],
                raw_data={
                    "provider": self.code,
                    "qualification_id": 212,
                    "targeting_age_ranges": [{"min": 13, "max": 120}],
                },
            ),
            TargetingQuestion(
                survey=survey,
                question_id=211,
                key="GENDER",
                text="What is your gender?",
                question_type="Single Punch",
                category="Required profile",
                options=[
                    {"OptionId": "111", "OptionText": "Male"},
                    {"OptionId": "112", "OptionText": "Female"},
                    {"OptionId": "113", "OptionText": "Prefer not to say"},
                ],
                raw_data={"provider": self.code, "qualification_id": 211},
            ),
            TargetingQuestion(
                survey=survey,
                question_id=229,
                key="POSTAL_CODE",
                text="What is your postal code?",
                question_type="Text",
                category="Required profile",
                options=[],
                raw_data={
                    "provider": self.code,
                    "qualification_id": 229,
                    "country": survey.country_code,
                },
            ),
        ]
        now = timezone.now()
        with transaction.atomic():
            survey.targeting_questions.all().delete()
            survey.quotas.all().delete()
            TargetingQuestion.objects.bulk_create(questions)
            survey.has_quota = False
            survey.targeting_synced_at = now
            survey.quota_synced_at = now
            survey.detail_synced_at = now
            survey.save(update_fields=[
                "has_quota", "targeting_synced_at", "quota_synced_at",
                "detail_synced_at", "updated_at",
            ])

    def build_outbound_url(self, survey, attempt, answers):
        """Replace the client entry link's [RID] placeholder with our journey RID."""

        entry_link = str(survey.entry_link or "").strip()
        if not entry_link:
            raise ProviderConfigurationError(
                "PureSpectrum did not return an entry link for this survey."
            )
        replacement_count = 0

        def replace(_match):
            nonlocal replacement_count
            replacement_count += 1
            return attempt.rid

        outbound_url = re.sub(r"\[RID\]|%5BRID%5D", replace, entry_link, flags=re.IGNORECASE)
        if replacement_count == 0:
            raise ProviderError(
                "PureSpectrum entry link does not contain the required [RID] placeholder."
            )
        parsed = urlsplit(outbound_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ProviderError("PureSpectrum returned an invalid respondent entry link.")
        if len(outbound_url) > 3000:
            raise ProviderError("PureSpectrum respondent entry link is too long.")
        answer_map = {
            str(answer.get("question_key") or "").upper(): (
                answer.get("values") or []
            )
            for answer in answers.values()
        }
        try:
            age = int((answer_map.get("AGE") or [""])[0])
        except (TypeError, ValueError) as exc:
            raise ProviderError("Enter a valid age for PureSpectrum.") from exc
        if not 13 <= age <= 120:
            raise ProviderError("PureSpectrum age must be between 13 and 120.")
        gender = str((answer_map.get("GENDER") or [""])[0])
        if gender not in {"111", "112", "113"}:
            raise ProviderError("Select a valid gender for PureSpectrum.")
        postal_code = re.sub(
            r"[\s-]", "", str((answer_map.get("POSTAL_CODE") or [""])[0]).upper()
        )
        postal_patterns = {
            "US": r"\d{5}(?:\d{4})?",
            "IN": r"\d{6}",
            "GB": (
                r"(?:[A-Z]{2}\d[A-Z]\d[A-Z]{2}|[A-Z]\d[A-Z]\d[A-Z]{2}|"
                r"[A-Z]\d{2}[A-Z]{2}|[A-Z]\d{3}[A-Z]{2}|"
                r"[A-Z]{2}\d{2}[A-Z]{2}|[A-Z]{2}\d{3}[A-Z])"
            ),
        }
        if not re.fullmatch(postal_patterns.get(survey.country_code, r".+"), postal_code):
            raise ProviderError(
                f"Enter a valid postal code for {survey.country_code or 'this market'}."
            )
        return outbound_url
