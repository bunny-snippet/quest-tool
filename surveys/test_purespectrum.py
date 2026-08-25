from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from vendors.models import Client, ClientIntegration
from vendors.serializers import ClientIntegrationSerializer

from .models import Survey, SurveyAttempt
from .provider_services import sync_client_integration
from .providers.base import ProviderError
from .providers.purespectrum import PureSpectrumProvider


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class RecordingSession:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.payloads.pop(0))


class PureSpectrumProviderTests(TestCase):
    def setUp(self):
        self.client_record = Client.objects.create(
            code="purespectrum", name="PureSpectrum", provider_code="purespectrum"
        )
        self.integration = ClientIntegration.objects.create(
            client=self.client_record,
            name="Fusion Match",
            provider_code="purespectrum",
            base_url="https://fusionapi.spectrumsurveys.com/surveys/fusionMatch",
            credential_env_key="PURESPECTRUM_TEST_ACCESS_TOKEN",
            sync_interval_seconds=60,
        )

    @patch.dict("os.environ", {"PURESPECTRUM_TEST_ACCESS_TOKEN": "private-token"}, clear=False)
    def test_inventory_sends_exact_three_params_and_never_member_id(self):
        session = RecordingSession(
            {"surveys": [{"surveyId": "US-1", "entryLink": "https://survey.test/?rid=%5BRID%5D"}]},
            {"surveys": [{"surveyId": "IN-1", "entryLink": "https://survey.test/?rid=%5BRID%5D"}]},
            {"surveys": [{"surveyId": "GB-1", "entryLink": "https://survey.test/?rid=%5BRID%5D"}]},
        )
        provider = PureSpectrumProvider(self.integration, session=session)

        inventory = provider.inventory()

        self.assertEqual(len(inventory), 3)
        self.assertEqual(
            [call[1]["params"]["respondentLocalization"] for call in session.calls],
            ["en_US", "en_IN", "en_GB"],
        )
        for url, kwargs in session.calls:
            self.assertEqual(url, PureSpectrumProvider.default_base_url)
            self.assertEqual(kwargs["params"], {
                "respondentId": "[RID]",
                "respondentLocalization": kwargs["params"]["respondentLocalization"],
                "maxNumberOfSurveysReturned": 200,
            })
            self.assertNotIn("memberId", kwargs["params"])
            self.assertEqual(kwargs["headers"]["access-token"], "private-token")

    @patch.dict("os.environ", {"PURESPECTRUM_TEST_ACCESS_TOKEN": "private-token"}, clear=False)
    def test_inventory_row_maps_to_projects_market_and_client_entry_link(self):
        provider = PureSpectrumProvider(
            self.integration, session=RecordingSession({"surveys": []})
        )
        normalized = provider.normalize_inventory_item({
            "surveyId": "581e7b",
            "cpi": "2.75",
            "estimatedLoi": 14,
            "ir": "37.5",
            "fullOrPartialMatch": "FULL",
            "entryLink": "https://survey.test/start?respondentId=%5BRID%5D",
            "_respondentLocalization": "en_GB",
        }, timezone.now())

        self.assertEqual(normalized.source_key, "en_GB:581e7b")
        self.assertEqual(normalized.values["country_code"], "GB")
        self.assertEqual(normalized.values["language_code"], "EN")
        self.assertEqual(normalized.values["cpi"], Decimal("2.75"))
        self.assertEqual(normalized.values["loi"], 14)
        self.assertEqual(normalized.values["incidence_rate"], Decimal("37.5"))
        self.assertEqual(
            normalized.values["entry_link"],
            "https://survey.test/start?respondentId=%5BRID%5D",
        )

    @patch.dict("os.environ", {"PURESPECTRUM_TEST_ACCESS_TOKEN": "private-token"}, clear=False)
    @patch("surveys.provider_services.get_provider")
    def test_sync_persists_each_locale_as_project_inventory(self, get_provider_mock):
        session = RecordingSession(
            {"surveys": [{"surveyId": "PS-1", "entryLink": "https://survey.test/?rid=%5BRID%5D"}]},
            {"surveys": [{"surveyId": "PS-1", "entryLink": "https://survey.test/?rid=%5BRID%5D"}]},
            {"surveys": [{"surveyId": "PS-1", "entryLink": "https://survey.test/?rid=%5BRID%5D"}]},
        )
        get_provider_mock.return_value = PureSpectrumProvider(
            self.integration, session=session
        )

        run = sync_client_integration(self.integration, refresh_details=False)

        self.assertEqual(run.created, 3)
        self.assertEqual(
            set(Survey.objects.filter(integration=self.integration).values_list("source_key", flat=True)),
            {"en_US:PS-1", "en_IN:PS-1", "en_GB:PS-1"},
        )

    @patch.dict("os.environ", {"PURESPECTRUM_TEST_ACCESS_TOKEN": "private-token"}, clear=False)
    def test_pre_screener_details_and_rid_replacement(self):
        survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_key="en_US:PS-1",
            country="United States",
            country_code="US",
            language="English",
            language_code="EN",
            entry_link=(
                "https://survey.test/start?respondentId=%5BRID%5D"
                "&localization=en_US"
            ),
        )
        provider = PureSpectrumProvider(
            self.integration, session=RecordingSession({"surveys": []})
        )
        provider.refresh_details(survey)
        attempt = SurveyAttempt.objects.create(
            rid="Ab12Cd34Ef", survey=survey, user_id="1"
        )

        outbound = provider.build_outbound_url(survey, attempt, answers={
            "age": {"question_key": "AGE", "values": ["31"]},
            "gender": {"question_key": "GENDER", "values": ["111"]},
            "postal": {"question_key": "POSTAL_CODE", "values": ["94105"]},
        })

        self.assertEqual(
            outbound,
            "https://survey.test/start?respondentId=Ab12Cd34Ef&localization=en_US",
        )
        self.assertNotIn("memberId", outbound)
        self.assertEqual(
            set(survey.targeting_questions.values_list("question_id", flat=True)),
            {211, 212, 229},
        )

    @patch.dict("os.environ", {"PURESPECTRUM_TEST_ACCESS_TOKEN": "private-token"}, clear=False)
    def test_entry_link_without_rid_placeholder_is_rejected(self):
        provider = PureSpectrumProvider(
            self.integration, session=RecordingSession({"surveys": []})
        )
        survey = SimpleNamespace(entry_link="https://survey.test/start?fixed=1")
        attempt = SimpleNamespace(rid="Ab12Cd34Ef")

        with self.assertRaisesMessage(ProviderError, "required [RID] placeholder"):
            provider.build_outbound_url(survey, attempt, answers={})

    def test_serializer_locks_fusion_contract_and_requires_env_reference(self):
        serializer = ClientIntegrationSerializer(data={
            "client": self.client_record.pk,
            "name": "Second Fusion connection",
            "provider_code": "purespectrum",
            "base_url": "https://fusionapi.spectrumsurveys.com/surveys/fusionMatch",
            "credential_env_key": "PURESPECTRUM_ACCESS_TOKEN",
            "sync_interval_seconds": 60,
            "scheduled_sync_enabled": False,
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        integration = serializer.save()
        self.assertEqual(integration.auth_header_name, "access-token")
        self.assertEqual(integration.inventory_result_key, "surveys")
        self.assertEqual(integration.config, {"timeout_seconds": 30})

        invalid = ClientIntegrationSerializer(data={
            "client": self.client_record.pk,
            "name": "Bad Fusion connection",
            "provider_code": "purespectrum",
            "base_url": "https://example.test/fusionMatch",
            "credential_env_key": "raw token value",
        })
        self.assertFalse(invalid.is_valid())

    @override_settings(CLIENT_INTEGRATION_PURESPECTRUM_SYNC_INTERVAL_SECONDS=60)
    @patch("surveys.tasks.sync_client_integration_task.delay")
    def test_verified_integration_is_automatically_dispatched(self, delay):
        from .tasks import dispatch_due_integrations_task

        ClientIntegration.objects.exclude(pk=self.integration.pk).delete()
        self.integration.last_test_status = "success"
        self.integration.last_sync_started_at = timezone.now() - timedelta(seconds=61)
        self.integration.save(update_fields=["last_test_status", "last_sync_started_at"])

        result = dispatch_due_integrations_task()

        self.assertEqual(result["queued"], [self.integration.pk])
        delay.assert_called_once_with(self.integration.pk)
