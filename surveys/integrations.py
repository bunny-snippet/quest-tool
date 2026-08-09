import logging
from dataclasses import dataclass
from typing import Any

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class InnovateMRAPIError(RuntimeError):
    """Raised when the supplier API is unavailable or returns an invalid response."""


class InnovateMRNotFound(InnovateMRAPIError):
    """Raised when InnovateMR has no resource for the requested survey."""


@dataclass
class PagedSurveyResult:
    surveys: list[dict[str, Any]]
    pages: int


class InnovateMRClient:
    """Small server-side client for InnovateMR Supplier API v2."""

    def __init__(self, token: str | None = None, session: requests.Session | None = None, integration=None):
        self.integration = integration
        if token is None and integration is not None:
            from vendors.credentials import resolve_integration_token
            token = resolve_integration_token(integration)
        self.token = token if token is not None else settings.INNOVATEMR_API_TOKEN
        self.base_url = (integration.base_url if integration is not None else settings.INNOVATEMR_BASE_URL).rstrip("/")
        self.timeout = settings.INNOVATEMR_TIMEOUT_SECONDS
        self.page_size = settings.INNOVATEMR_PAGE_SIZE
        self.max_pages = settings.INNOVATEMR_MAX_PAGES
        self.session = session or requests.Session()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.token:
            raise InnovateMRAPIError("INNOVATEMR_API_TOKEN is not configured")
        try:
            response = self.session.get(
                f"{self.base_url}{path}",
                params=params,
                headers={"x-access-token": self.token, "Accept": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                raise InnovateMRNotFound(f"InnovateMR returned no data for {path}") from exc
            raise InnovateMRAPIError(f"InnovateMR request failed for {path}: {exc}") from exc
        except (requests.RequestException, ValueError) as exc:
            raise InnovateMRAPIError(f"InnovateMR request failed for {path}: {exc}") from exc

        if not isinstance(payload, dict) or payload.get("apiStatus") not in {None, "success"}:
            message = payload.get("msg", "Unexpected upstream response") if isinstance(payload, dict) else "Invalid JSON object"
            raise InnovateMRAPIError(f"InnovateMR rejected {path}: {message}")
        return payload

    @staticmethod
    def _result_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
        result = payload.get("result", [])
        if not isinstance(result, list):
            raise InnovateMRAPIError("InnovateMR 'result' must be a list")
        return [item for item in result if isinstance(item, dict)]

    def get_allocated_surveys(self) -> list[dict[str, Any]]:
        payload = self._get("/supply/getAllocatedSurveys")
        return self._result_list(payload)

    def test_connection(self) -> dict[str, Any]:
        """Validate authentication with one bounded read and expose no supplier payload."""
        original_page_size = self.page_size
        self.page_size = 1
        try:
            payload = self._get("/supply/getAllocatedSurveysPaged", params={"limit": 1})
            return {"ok": True, "records_visible": len(self._result_list(payload))}
        finally:
            self.page_size = original_page_size

    def get_allocated_surveys_paged(self) -> PagedSurveyResult:
        surveys: list[dict[str, Any]] = []
        next_cursor: str | None = None
        seen_cursors: set[str] = set()

        for page_number in range(1, self.max_pages + 1):
            params: dict[str, Any] = {"limit": self.page_size}
            if next_cursor:
                params["next"] = next_cursor
            payload = self._get("/supply/getAllocatedSurveysPaged", params=params)
            surveys.extend(self._result_list(payload))

            paging = payload.get("paging") or {}
            candidate = paging.get("next") if isinstance(paging, dict) else None
            if not candidate or candidate in seen_cursors:
                return PagedSurveyResult(surveys=surveys, pages=page_number)
            seen_cursors.add(candidate)
            next_cursor = candidate

        raise InnovateMRAPIError(f"Pagination exceeded INNOVATEMR_MAX_PAGES={self.max_pages}")

    def get_quota_for_survey(self, survey_id: int) -> list[dict[str, Any]]:
        return self._result_list(self._get(f"/supply/getQuotaForSurvey/{survey_id}"))

    def get_survey_targeting(self, survey_id: int) -> list[dict[str, Any]]:
        return self._result_list(self._get(f"/supply/getSurveyTargeting/{survey_id}"))

    def get_survey_transactions_by_pid(self, survey_id: int, pid: str) -> list[dict[str, Any]]:
        """Fetch transactions for one survey/PID pair; our PID is the attempt RID."""
        return self._result_list(self._get(f"/supply/getSurveyTransactionsByCond/{survey_id}/{pid}"))
