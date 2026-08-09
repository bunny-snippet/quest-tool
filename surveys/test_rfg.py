import hashlib
import hmac
import json
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from vendors.models import Client, ClientIntegration

from .models import Survey, SurveyAttempt
from .provider_services import sync_client_integration
from .providers.base import NormalizedSurvey, ProviderConfigurationError
from .providers.rfg import ResearchForGoodProvider


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class RecordingSession:
    def __init__(self, payload):
        self.payload = payload
        self.request = None

    def post(self, url, **kwargs):
        self.request = (url, kwargs)
        return FakeResponse(self.payload)


class FakeProvider:
    def __init__(self, integration):
        self.integration = integration

    def inventory(self):
        return [{"rfg_id": "RFG123456-001", "lastModified": "2026-08-09T10:00:00Z"}]

    def normalize_inventory_item(self, payload, seen_at):
        return NormalizedSurvey(
            source_key=payload["rfg_id"],
            numeric_source_id=None,
            modified_at=datetime(2026, 8, 9, 10, tzinfo=dt_timezone.utc),
            values={
                "company_name": self.integration.client.name,
                "name": "RFG Opinion Study",
                "status": Survey.Status.LIVE,
                "sample_size": 100,
                "completes": 10,
                "remaining": 90,
                "cpi": Decimal("2.50"),
                "country": "US",
                "country_code": "US",
                "source_modified_at": datetime(2026, 8, 9, 10, tzinfo=dt_timezone.utc),
                "last_seen_at": seen_at,
                "raw_data": payload,
            },
            raw_data=payload,
        )


@override_settings(PUBLIC_SUPPLIER_CODE="1000")
class ResearchForGoodIntegrationTests(TestCase):
    def setUp(self):
        self.client_record = Client.objects.create(code="rfg-client", name="Research For Good", provider_code="rfg")
        self.integration = ClientIntegration.objects.create(
            client=self.client_record,
            name="RFG Live Alert",
            provider_code="rfg",
            base_url="https://api.researchforgood.com/API/",
            credential_env_keys={"apid": "RFG_APID", "secret": "RFG_SECRET"},
            sync_interval_seconds=600,
        )

    @patch.dict("os.environ", {"RFG_APID": "publisher", "RFG_SECRET": "00112233445566778899aabbccddeeff"}, clear=False)
    def test_request_signs_exact_json_with_hmac_sha1(self):
        session = RecordingSession({"result": 0, "response": {"marker": "quest-tool-1700000000"}})
        provider = ResearchForGoodProvider(self.integration, session=session, clock=lambda: 1700000000)
        provider.test_connection()
        request_url, kwargs = session.request
        self.assertEqual(request_url, "https://api.researchforgood.com/API")
        body = kwargs["data"].decode()
        expected = hmac.new(
            bytes.fromhex("00112233445566778899aabbccddeeff"),
            f"1700000000{body}".encode(),
            hashlib.sha1,
        ).hexdigest()
        self.assertEqual(kwargs["params"], {"apid": "publisher", "time": "1700000000", "hash": expected})
        self.assertEqual(json.loads(body)["command"], "test/copy/1")

    def test_missing_environment_secret_fails_without_storing_secret(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ProviderConfigurationError):
                ResearchForGoodProvider(self.integration)
        self.assertEqual(self.integration.credential_env_keys["secret"], "RFG_SECRET")

    @patch("surveys.provider_services.get_provider")
    def test_sync_is_scoped_by_client_and_accepts_string_provider_id(self, get_provider_mock):
        get_provider_mock.return_value = FakeProvider(self.integration)
        other_client = Client.objects.create(code="other-rfg", name="Other RFG", provider_code="rfg")
        Survey.objects.create(client=other_client, source_key="RFG123456-001", name="Same provider ID, other account")
        run = sync_client_integration(self.integration, refresh_details=False)
        self.assertEqual(run.created, 1)
        survey = Survey.objects.get(client=self.client_record, source_key="RFG123456-001")
        self.assertEqual(survey.integration, self.integration)
        self.assertEqual(survey.cpi, Decimal("2.50"))
        self.assertEqual(Survey.objects.filter(source_key="RFG123456-001").count(), 2)

    def test_superadmin_can_discover_provider_and_create_non_secret_integration(self):
        admin = get_user_model().objects.create_superuser("owner", "owner@example.com", "pass")
        api = APIClient(); api.force_authenticate(admin)
        response = api.get("/api/v1/vendors/integrations/providers/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["code"], "rfg")
        self.assertEqual(
            {provider["code"] for provider in response.json()},
            {"rfg", "innovatemr", "biobrain", "custom"},
        )
        response = api.post("/api/v1/vendors/integrations/", {
            "client": self.client_record.pk,
            "name": "RFG UI connection",
            "provider_code": "rfg",
            "base_url": "https://api.researchforgood.com/API/",
            "credential_env_keys": {"apid": "RFG_APID", "secret": "RFG_SECRET"},
            "config": {
                "country": "US",
                "category": "B2C",
                "allow_recontacts": False,
                "callback_security_mode": "ip",
            },
            "supplier_code": "1000",
            "sync_interval_seconds": 600,
            "detail_refresh_batch": 3,
            "scheduled_sync_enabled": False,
            "is_active": True,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.json())
        response = api.get(f"/api/v1/vendors/integrations/{self.integration.pk}/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["credential_env_keys"]["secret"], "RFG_SECRET")
        self.assertNotIn("00112233445566778899aabbccddeeff", json.dumps(body))
        self.client.force_login(admin)
        page = self.client.get("/organization/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Secure upstream connection")
        self.assertContains(page, "Test connection")

        integration_page = self.client.get("/client-integrations/")
        self.assertEqual(integration_page.status_code, 200)
        self.assertContains(integration_page, "RFG credential references")
        self.assertContains(integration_page, "No provider is assumed automatically")
        self.assertContains(integration_page, "Custom REST API")
        self.assertNotContains(integration_page, 'id="provider" value="innovatemr"')
        self.assertNotContains(integration_page, 'placeholder="InnovateMR production"')

    def test_trusted_rfg_callback_completes_attempt(self):
        self.integration.last_test_status = "success"
        self.integration.save(update_fields=["last_test_status"])
        survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_key="RFG123456-001",
            entry_link="https://rfg.example/start",
            country_code="US",
        )
        attempt = SurveyAttempt.objects.create(rid="Abc123Xyz9", survey=survey, user_id="42", status=SurveyAttempt.Status.REDIRECTED)
        response = self.client.get("/survey/rfg/callback", {"result": "1", "rid": attempt.rid}, REMOTE_ADDR="15.222.163.99")
        self.assertEqual(response.status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.COMPLETED)
        self.assertTrue(attempt.is_verified)
        self.assertEqual(attempt.status_source, "rfg_callback")
