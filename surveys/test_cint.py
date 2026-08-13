import base64
import hashlib
import hmac
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory

from prescreener_vault.cint_email_pool import add_real_email
from prescreener_vault.constants import DATABASE_ALIAS
from prescreener_vault.models import CintRespondentEmail
from vendors.models import Client, ClientIntegration
from vendors.serializers import ClientIntegrationSerializer

from .models import ProviderQuestionMapping, Survey, SurveyAttempt, TargetingQuestion
from .provider_services import sync_cint_redirect_contracts, sync_client_integration
from .providers import ProviderError
from .providers.cint import CintProvider
from .serializers import SurveyListSerializer, SurveyQuotaSerializer, TargetingQuestionSerializer
from .views import _prescreener_questions


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

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.payloads.pop(0))

    def put(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.payloads.pop(0))


DEFINITIONS = {
    "ApiResult": 0,
    "AllCountryLanguages": [
        {"Id": "6", "Code": "ENG-CA", "Name": "English - Canada"},
        {"Id": "7", "Code": "ENG-IN", "Name": "English - India"},
        {"Id": "8", "Code": "ENG-GB", "Name": "English - United Kingdom"},
        {"Id": "9", "Code": "ENG-US", "Name": "English - United States"},
        {"Id": "10", "Code": "FRE-FR", "Name": "French - France"},
        {"Id": "76", "Code": "HIN-IN", "Name": "Hindi - India"},
    ],
    "AllSampleTypes": [
        {"Id": "1", "Code": "B2C", "Name": "Consumer"},
        {"Id": "2", "Code": "B2B", "Name": "Business-to-business"},
    ],
    "AllStudyTypes": [{"Id": "1", "Code": "ADH", "Name": "Ad hoc"}],
}


class CintProviderTests(TestCase):
    databases = {"default", DATABASE_ALIAS}

    def setUp(self):
        self.client_record = Client.objects.create(
            code="cint", name="Cint Exchange", provider_code="cint"
        )
        self.integration = ClientIntegration.objects.create(
            client=self.client_record,
            name="Cint Model 2 polling",
            provider_code="cint",
            base_url="https://api.samplicio.us",
            credential_env_key="TEST_CINT_API_KEY",
            supplier_code="0050",
            sync_interval_seconds=60,
            detail_refresh_batch=1,
        )

    @patch("surveys.providers.cint.time.sleep")
    @patch.dict("os.environ", {"TEST_CINT_API_KEY": "cint-secret"}, clear=False)
    def test_inventory_uses_only_approved_locales_and_inclusive_rpi_bands(self, sleep_mock):
        session = RecordingSession(
            {"ApiResult": 0, "Surveys": [
                {"SurveyNumber": 6001, "CountryLanguageID": 6, "RPI": {"Value": 0.97}},
                {"SurveyNumber": 6002, "CountryLanguageID": 6, "RPI": {"Value": 4.10}},
                {"SurveyNumber": 6003, "CountryLanguageID": 6, "RPI": {"Value": 0.96}},
                {"SurveyNumber": 6004, "CountryLanguageID": 6, "RPI": {"Value": 4.11}},
                {"SurveyNumber": 6099, "CountryLanguageID": 9, "RPI": {"Value": 2}},
            ]},
            {"ApiResult": 0, "Surveys": [
                {"SurveyNumber": 7001, "CountryLanguageID": 7, "RPI": {"Value": 2.50}},
            ]},
            {"ApiResult": 0, "Surveys": [
                {"SurveyNumber": 8001, "CountryLanguageID": 8, "RPI": {"Value": 0.97}},
                {"SurveyNumber": 8002, "CountryLanguageID": 8, "RPI": {"Value": 2.10}},
                {"SurveyNumber": 8003, "CountryLanguageID": 8, "RPI": {"Value": 2.11}},
            ]},
            {"ApiResult": 0, "Surveys": [
                {"SurveyNumber": 9001, "CountryLanguageID": 9, "RPI": {"Value": 4}},
            ]},
            {"ApiResult": 0, "Surveys": [
                {"SurveyNumber": 10001, "CountryLanguageID": 10, "RPI": {"Value": 1}},
            ]},
            {"ApiResult": 0, "Surveys": [
                {"SurveyNumber": 76001, "CountryLanguageID": 76, "RPI": {"Value": 4}},
            ]},
            {"ApiResult": 0, "SupplierAllocationSurveys": []},
        )
        provider = CintProvider(self.integration, session=session)
        with patch.object(provider, "_load_definitions"):
            rows = provider.inventory()

        self.assertEqual(
            [str(row["SurveyNumber"]) for row in rows],
            ["6001", "6002", "7001", "8001", "8002", "9001", "10001", "76001"],
        )
        self.assertEqual(
            provider.rejected_source_keys,
            {"6003", "6004", "6099", "8003"},
        )
        requested = [
            call[0].split("/ByCountryLanguage/", 1)[1]
            for call in session.calls
            if "/ByCountryLanguage/" in call[0]
        ]
        self.assertEqual(
            requested,
            ["6/0050", "7/0050", "8/0050", "9/0050", "10/0050", "76/0050"],
        )
        self.assertIn("/SupplierAllocations/All/0050", session.calls[-1][0])
        self.assertEqual(sleep_mock.call_count, 6)

    @patch("surveys.providers.cint.time.sleep")
    @patch.dict("os.environ", {"TEST_CINT_API_KEY": "cint-secret"}, clear=False)
    def test_inventory_imports_all_allocated_rows_matching_the_same_policy(self, sleep_mock):
        Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_key="9001",
            source_id=9001,
            cpi=Decimal("1.50"),
            remaining=40,
        )
        empty_feed = {"ApiResult": 0, "Surveys": []}
        session = RecordingSession(
            empty_feed, empty_feed, empty_feed, empty_feed, empty_feed, empty_feed,
            {"ApiResult": 0, "SupplierAllocationSurveys": [
                {
                    "SurveyNumber": 9001,
                    "CountryLanguageID": 9,
                    "RPI": {"Value": 1.50},
                    "BidLengthOfInterview": 35,
                    "LengthOfInterview": 99,
                    "BidIncidence": 50,
                    "TotalRemaining": 36,
                },
                {
                    "SurveyNumber": 9999,
                    "CountryLanguageID": 9,
                    "OfferwallAllocations": [{
                        "SupplierCode": "0050",
                        "TargetModel": {"RPI": {"value": 1.25}},
                    }],
                    "TotalRemaining": 200,
                },
                {
                    "SurveyNumber": 9998,
                    "CountryLanguageID": 8,
                    "RPI": {"Value": 2.11},
                    "TotalRemaining": 200,
                },
            ]},
        )
        provider = CintProvider(self.integration, session=session)
        with patch.object(provider, "_load_definitions"):
            rows = provider.inventory()

        self.assertEqual([row["SurveyNumber"] for row in rows], [9001, 9999])
        normalized = provider.normalize_inventory_item(rows[0], timezone.now())
        self.assertEqual(normalized.values["loi"], 35)
        self.assertEqual(normalized.values["incidence_rate"], Decimal("50"))
        self.assertEqual(normalized.values["remaining"], 36)
        self.assertEqual(normalized.values["sample_size"], 36)
        self.assertEqual(sleep_mock.call_count, 6)

    @override_settings(PUBLIC_APP_BASE_URL="https://api.exchange-ip.com")
    @patch.dict("os.environ", {"TEST_CINT_API_KEY": "cint-secret"}, clear=False)
    def test_new_allocated_row_with_live_link_uses_put_not_duplicate_create(self):
        payload = {
            "SurveyNumber": 9999,
            "CountryLanguageID": 9,
            "RPI": {"Value": 1.25},
            "OfferwallAllocations": [{
                "SupplierCode": "0050",
                "TargetModel": {
                    "LiveSupplierLink": "https://samplicio.us/s/default.aspx?SID=allocated&PID=",
                    "RPI": {"value": 1.25},
                },
            }],
            "_cint_inventory_source": "supplier_allocations_inventory",
        }
        session = RecordingSession({"ApiResult": 0})
        provider = CintProvider(self.integration, session=session)
        normalized = provider.normalize_inventory_item(payload, timezone.now())

        prepared = provider.prepare_inventory_item(normalized)

        self.assertEqual(len(session.calls), 1)
        self.assertIn("/SupplierLinks/Update/9999/0050", session.calls[0][0])
        self.assertIn("SID=allocated", prepared.values["entry_link"])
        self.assertEqual(prepared.raw_data["_cint_redirect_method"], "PUT")

    @override_settings(PUBLIC_APP_BASE_URL="https://api.exchange-ip.com")
    @patch.dict("os.environ", {"TEST_CINT_API_KEY": "cint-secret"}, clear=False)
    def test_sync_posts_new_links_and_puts_existing_links_before_db_write(self):
        existing = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_key="143479",
            company_name="Cint Exchange",
            entry_link="https://samplicio.us/s/default.aspx?SID=existing&PID=",
        )
        session = RecordingSession(
            {"ApiResult": 0, "SupplierLink": {
                "LiveLink": "https://samplicio.us/s/default.aspx?SID=new&PID=",
                "TestLink": "https://samplicio.us/s/default.aspx?SID=new-test&PID=",
            }},
            {"ApiResult": 0},
        )
        provider = CintProvider(self.integration, session=session)
        provider._country_languages = {"9": DEFINITIONS["AllCountryLanguages"][3]}
        provider._sample_types = {"1": DEFINITIONS["AllSampleTypes"][0]}
        provider._study_types = {"1": DEFINITIONS["AllStudyTypes"][0]}
        inventory = [
            {
                "SurveyNumber": 457751, "SurveyName": "New survey",
                "CountryLanguageID": 9, "RPI": {"Value": 1.35},
                "SampleTypeID": 1, "StudyTypeID": 1,
            },
            {
                "SurveyNumber": 143479, "SurveyName": "Updated survey",
                "CountryLanguageID": 9, "RPI": {"Value": 2.25},
                "SampleTypeID": 1, "StudyTypeID": 1,
            },
        ]
        with patch.object(provider, "inventory", return_value=inventory), patch(
            "surveys.provider_services.get_provider", return_value=provider
        ):
            run = sync_client_integration(self.integration)

        self.assertEqual((run.created, run.updated, run.unique_surveys), (1, 1, 2))
        self.assertEqual(len(session.calls), 2)
        self.assertIn("/SupplierLinks/Create/457751/0050", session.calls[0][0])
        self.assertIn("/SupplierLinks/Update/143479/0050", session.calls[1][0])
        self.assertEqual(session.calls[0][1]["json"], provider._redirect_payload())
        self.assertEqual(session.calls[1][1]["json"], provider._redirect_payload())
        created = Survey.objects.get(integration=self.integration, source_key="457751")
        self.assertIn("SID=new", created.entry_link)
        self.assertEqual(created.raw_data["_cint_redirect_method"], "POST")
        existing.refresh_from_db()
        self.assertEqual(existing.cpi, Decimal("2.25"))
        self.assertIn("SID=existing", existing.entry_link)
        self.assertEqual(existing.raw_data["_cint_redirect_method"], "PUT")

    @override_settings(PUBLIC_APP_BASE_URL="https://api.exchange-ip.com")
    @patch.dict("os.environ", {"TEST_CINT_API_KEY": "cint-secret"}, clear=False)
    def test_failed_link_provisioning_skips_new_survey_entirely(self):
        provider = CintProvider(self.integration, session=RecordingSession())
        provider._country_languages = {"9": DEFINITIONS["AllCountryLanguages"][3]}
        inventory = [{
            "SurveyNumber": 457751,
            "CountryLanguageID": 9,
            "RPI": {"Value": 1.35},
        }]
        with patch.object(provider, "inventory", return_value=inventory), patch.object(
            provider, "_request", side_effect=ProviderError("link create failed")
        ), patch("surveys.provider_services.get_provider", return_value=provider):
            run = sync_client_integration(self.integration)

        self.assertEqual(run.status, "partial")
        self.assertEqual(run.detail_failures, 1)
        self.assertFalse(
            Survey.objects.filter(integration=self.integration, source_key="457751").exists()
        )

    @override_settings(PUBLIC_APP_BASE_URL="https://api.exchange-ip.com")
    @patch.dict("os.environ", {"TEST_CINT_API_KEY": "cint-secret"}, clear=False)
    def test_inventory_sync_preserves_hydrated_supplier_link_on_put(self):
        survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_id=143479,
            source_key="143479",
            company_name="Cint Exchange",
            entry_link="https://samplicio.us/s/default.aspx?SID=live-sid&PID=",
            test_entry_link="https://samplicio.us/s/default.aspx?SID=test-sid&PID=",
            raw_data={"_cint_supplier_link": {"SupplierLinkID": 99}},
        )
        provider = CintProvider(
            self.integration,
            session=RecordingSession({"ApiResult": 0}),
        )
        provider._country_languages = {"9": DEFINITIONS["AllCountryLanguages"][3]}
        inventory = [{
            "SurveyNumber": 143479,
            "SurveyName": "Existing survey",
            "CountryLanguageID": 9,
            "RPI": {"Value": 1.50},
        }]
        with patch.object(provider, "inventory", return_value=inventory), patch(
            "surveys.provider_services.get_provider", return_value=provider
        ):
            sync_client_integration(self.integration)

        survey.refresh_from_db()
        self.assertIn("SID=live-sid", survey.entry_link)
        self.assertIn("SID=test-sid", survey.test_entry_link)
        self.assertEqual(survey.raw_data["_cint_supplier_link"]["SupplierLinkID"], 99)
        self.assertEqual(
            survey.raw_data["_cint_redirect_contract"],
            provider.redirect_contract_fingerprint(),
        )

    @override_settings(PUBLIC_APP_BASE_URL="https://api.exchange-ip.com")
    @patch.dict("os.environ", {"TEST_CINT_API_KEY": "cint-secret"}, clear=False)
    def test_unchanged_existing_inventory_does_not_repeat_redirect_put(self):
        provider = CintProvider(self.integration, session=RecordingSession())
        provider._country_languages = {"9": DEFINITIONS["AllCountryLanguages"][3]}
        payload = {
            "SurveyNumber": 143479,
            "SurveyName": "Stable survey",
            "CountryLanguageID": 9,
            "RPI": {"Value": 1.50},
        }
        normalized = provider.normalize_inventory_item(payload, timezone.now())
        values = dict(normalized.values)
        values["entry_link"] = "https://samplicio.us/s/default.aspx?SID=stable&PID="
        values["raw_data"] = {
            **normalized.raw_data,
            "_cint_redirect_contract": provider.redirect_contract_fingerprint(),
            "_cint_redirect_supplier_code": provider.supplier_code,
        }
        Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_id=normalized.numeric_source_id,
            source_key=normalized.source_key,
            **values,
        )
        with patch.object(provider, "inventory", return_value=[payload]), patch(
            "surveys.provider_services.get_provider", return_value=provider
        ):
            run = sync_client_integration(self.integration)

        self.assertEqual(run.unchanged, 1)
        self.assertEqual(provider.session.calls, [])

    @override_settings(PUBLIC_APP_BASE_URL="https://api.exchange-ip.com")
    @patch.dict("os.environ", {"TEST_CINT_API_KEY": "cint-secret"}, clear=False)
    def test_changed_inventory_does_not_repeat_put_when_redirect_contract_is_current(self):
        provider = CintProvider(self.integration, session=RecordingSession())
        provider._country_languages = {"9": DEFINITIONS["AllCountryLanguages"][3]}
        old_payload = {
            "SurveyNumber": 143479,
            "SurveyName": "Old name",
            "CountryLanguageID": 9,
            "RPI": {"Value": 1.50},
        }
        normalized = provider.normalize_inventory_item(old_payload, timezone.now())
        values = dict(normalized.values)
        values["entry_link"] = "https://samplicio.us/s/default.aspx?SID=stable&PID="
        values["raw_data"] = {
            **normalized.raw_data,
            "_cint_redirect_contract": provider.redirect_contract_fingerprint(),
            "_cint_redirect_supplier_code": provider.supplier_code,
        }
        survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_id=normalized.numeric_source_id,
            source_key=normalized.source_key,
            **values,
        )
        changed_payload = {
            **old_payload,
            "SurveyName": "Updated name",
            "RPI": {"Value": 2.25},
        }

        with patch.object(provider, "inventory", return_value=[changed_payload]), patch(
            "surveys.provider_services.get_provider", return_value=provider
        ):
            run = sync_client_integration(self.integration)

        survey.refresh_from_db()
        self.assertEqual(run.updated, 1)
        self.assertEqual(survey.name, "Updated name")
        self.assertEqual(survey.cpi, Decimal("2.25"))
        self.assertEqual(provider.session.calls, [])
        self.assertEqual(
            survey.raw_data["_cint_redirect_contract"],
            provider.redirect_contract_fingerprint(),
        )

    @override_settings(PUBLIC_APP_BASE_URL="https://api.exchange-ip.com")
    @patch.dict("os.environ", {"TEST_CINT_API_KEY": "cint-secret"}, clear=False)
    def test_redirect_update_uses_real_supplier_code_and_mid_callbacks(self):
        survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_id=82199770,
            source_key="82199770",
            company_name="Cint Exchange",
            entry_link="https://samplicio.us/s/default.aspx?SID=existing&PID=",
        )
        session = RecordingSession({"ApiResult": 0})
        provider = CintProvider(self.integration, session=session)

        provider.update_supplier_link_redirects(survey)

        self.assertEqual(len(session.calls), 1)
        url, kwargs = session.calls[0]
        self.assertEqual(
            url,
            "https://api.samplicio.us/Supply/v1/SupplierLinks/Update/82199770/0050",
        )
        self.assertEqual(kwargs["headers"]["Authorization"], "cint-secret")
        self.assertEqual(kwargs["json"], {
            "SupplierLinkTypeCode": "OWS",
            "TrackingTypeCode": "NONE",
            "DefaultLink": "https://api.exchange-ip.com/survey?status=2&rid=[%MID%]",
            "SuccessLink": "https://api.exchange-ip.com/survey?status=1&rid=[%MID%]",
            "FailureLink": "https://api.exchange-ip.com/survey?status=2&rid=[%MID%]",
            "OverQuotaLink": "https://api.exchange-ip.com/survey?status=3&rid=[%MID%]",
            "QualityTerminationLink": "https://api.exchange-ip.com/survey?status=4&rid=[%MID%]",
        })
        survey.refresh_from_db()
        self.assertEqual(
            survey.raw_data["_cint_redirect_contract"],
            provider.redirect_contract_fingerprint(),
        )

    @override_settings(PUBLIC_APP_BASE_URL="https://api.exchange-ip.com")
    @patch.dict("os.environ", {"TEST_CINT_API_KEY": "cint-secret"}, clear=False)
    def test_redirect_batch_skips_surveys_already_on_current_contract(self):
        provider = CintProvider(self.integration, session=RecordingSession({"ApiResult": 0}))
        fingerprint = provider.redirect_contract_fingerprint()
        configured = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_key="82199769",
            raw_data={
                "_cint_redirect_contract": fingerprint,
                "_cint_redirect_supplier_code": provider.supplier_code,
            },
        )
        pending = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_key="82199770",
            entry_link="https://samplicio.us/s/default.aspx?SID=pending&PID=",
        )

        with patch("surveys.provider_services.get_provider", return_value=provider):
            result = sync_cint_redirect_contracts(self.integration, batch_size=25)

        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["remaining"], 0)
        self.assertIn("/Update/82199770/0050", provider.session.calls[0][0])
        configured.refresh_from_db()
        pending.refresh_from_db()
        self.assertEqual(configured.raw_data["_cint_redirect_contract"], fingerprint)
        self.assertEqual(pending.raw_data["_cint_redirect_contract"], fingerprint)

    @override_settings(PUBLIC_APP_BASE_URL="https://api.exchange-ip.com")
    @patch.dict("os.environ", {"TEST_CINT_API_KEY": "cint-secret"}, clear=False)
    def test_redirect_update_creates_missing_supplier_link_before_put(self):
        survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_key="82199771",
        )
        session = RecordingSession(
            {"ApiResult": 0},
            {"ApiResult": 0, "SupplierLink": {
                "LiveLink": "https://samplicio.us/s/default.aspx?SID=new&PID=",
                "TestLink": "https://samplicio.us/s/default.aspx?SID=test&PID=test",
            }},
            {"ApiResult": 0},
        )
        provider = CintProvider(self.integration, session=session)

        provider.update_supplier_link_redirects(survey)

        self.assertEqual(len(session.calls), 3)
        self.assertIn("/SupplierLinks/BySurveyNumber/82199771/0050", session.calls[0][0])
        self.assertIn("/SupplierLinks/Create/82199771/0050", session.calls[1][0])
        self.assertIn("/SupplierLinks/Update/82199771/0050", session.calls[2][0])

    @patch.dict("os.environ", {"TEST_CINT_API_KEY": "cint-secret"}, clear=False)
    def test_refresh_details_builds_targeting_and_quota_drawer_data(self):
        survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_id=143479,
            source_key="143479",
            company_name="Cint Exchange",
            name="Cint details study",
            country="United States",
            country_code="US",
            language="English",
            language_code="ENG",
            raw_data={"CountryLanguageID": 9},
        )
        session = RecordingSession(
            {
                "ApiResult": 0,
                "SurveyQualification": {
                    "SurveyNumber": 143479,
                    "Questions": [{
                        "QuestionID": 43,
                        "LogicalOperator": "OR",
                        "PreCodes": ["1"],
                    }],
                },
            },
            {
                "ApiResult": 0,
                "SurveyNumber": 143479,
                "SurveyStillLive": True,
                "SurveyQuotas": [{
                    "SurveyQuotaID": 1781601,
                    "SurveyQuotaType": "Client",
                    "RPI": {"Value": 2.20, "CurrencyCode": "USD"},
                    "Conversion": 12,
                    "NumberOfRespondents": 10,
                    "Questions": [{
                        "QuestionID": 43,
                        "LogicalOperator": "OR",
                        "PreCodes": ["1"],
                    }],
                }],
            },
            {
                "ApiResult": 0,
                "Questions": [{
                    "Name": "GENDER",
                    "QuestionID": 43,
                    "QuestionText": "What is your gender?",
                    "QuestionType": "Single Punch",
                }],
            },
            {
                "ApiResult": 0,
                "QuestionOptions": [
                    {"OptionText": "Male", "Precode": "1", "QuestionID": 43},
                    {"OptionText": "Female", "Precode": "2", "QuestionID": 43},
                ],
            },
            {
                "ApiResult": 0,
                "SupplierLink": {
                    "LiveLink": "https://samplicio.us/s/default.aspx?SID=live-sid&PID=",
                    "TestLink": "https://samplicio.us/s/default.aspx?SID=test-sid&PID=test",
                },
            },
        )
        CintProvider(self.integration, session=session).refresh_details(survey)
        survey.refresh_from_db()

        question = survey.targeting_questions.get(question_id=43)
        question_data = TargetingQuestionSerializer(question).data
        self.assertEqual(question_data["text"], "What is your gender?")
        self.assertEqual(
            [(item["OptionText"], item["Qualifies"]) for item in question_data["options"]],
            [("Male", True), ("Female", False)],
        )
        self.assertEqual(question_data["targeting_note"], "Qualifying answer: Male")
        quota = survey.quotas.get(source_key="1781601")
        quota_data = SurveyQuotaSerializer(quota).data
        self.assertFalse(quota_data["target_known"])
        self.assertFalse(quota_data["completed_known"])
        self.assertEqual(quota_data["remaining"], 10)
        self.assertEqual(quota_data["targeting_details"][0]["values"], ["Male"])
        self.assertEqual(survey.cpi, Decimal("2.20"))
        self.assertIn("SID=live-sid", survey.entry_link)
        self.assertIsNotNone(survey.detail_synced_at)
        mapping = ProviderQuestionMapping.objects.get(
            provider_code="cint", external_question_id="43"
        )
        self.assertEqual(mapping.canonical_question.code, "gender")
        self.assertEqual(
            set(mapping.option_mappings.values_list("canonical_option__code", flat=True)),
            {"male", "female"},
        )

    def test_cint_prescreener_shows_qualifying_age_and_choice_hints(self):
        self.assertEqual(
            CintProvider._numeric_ranges(["18", "19", "20", "25"]),
            [{"min": 18, "max": 20}, {"min": 25, "max": 25}],
        )
        survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_id=143480,
            source_key="143480",
            company_name="Cint Exchange",
        )
        TargetingQuestion.objects.create(
            survey=survey,
            question_id=42,
            key="AGE",
            text="What is your age?",
            question_type="Numeric",
            category="Cint qualification",
            options=[
                {"OptionId": "18", "OptionText": "18"},
                {"OptionId": "19", "OptionText": "19"},
                {"OptionId": "20", "OptionText": "20"},
            ],
            raw_data={
                "provider": "cint",
                "targeting_choices": ["18", "19", "20"],
            },
        )
        TargetingQuestion.objects.create(
            survey=survey,
            question_id=43,
            key="GENDER",
            text="What is your gender?",
            question_type="Single Punch",
            category="Cint qualification",
            options=[
                {"OptionId": "1", "OptionText": "Male"},
                {"OptionId": "2", "OptionText": "Female"},
            ],
            raw_data={"provider": "cint", "targeting_choices": ["1"]},
        )

        age, gender = _prescreener_questions(survey)
        self.assertEqual((age["min_value"], age["max_value"]), (18, 20))
        self.assertEqual(age["targeting_note"], "Qualifying age: 18\u201320")
        self.assertEqual(gender["targeting_note"], "Qualifying answer: Male")
        self.assertEqual(gender["options"], [{"value": "1", "label": "Male", "selected": False}])

    @patch.dict(
        "os.environ",
        {"TEST_CINT_API_KEY": "cint-secret", "CINT_HASH_KEY": "hash-secret"},
        clear=False,
    )
    @override_settings(RESPONDENT_EMAIL_ENCRYPTION_KEY="test-respondent-email-key")
    def test_outbound_link_uses_uid_pid_rid_mid_profile_and_hmac_sha1(self):
        user = get_user_model().objects.create_user(
            username="cint-user", email="Example.User+test@gmail.com", password="test-pass"
        )
        survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_id=143479,
            source_key="143479",
            company_name="Cint Exchange",
            country_code="US",
            language_code="ENG",
            entry_link="https://samplicio.us/s/default.aspx?SID=live-sid&PID=",
        )
        user.is_superuser = True
        user.is_staff = True
        user.save(update_fields=["is_superuser", "is_staff"])
        request = APIRequestFactory().get("/api/v1/surveys/")
        request.user = user
        public_start = SurveyListSerializer(survey, context={"request": request}).data["start_link"]
        self.assertIn("supplierCode=1000", public_start)
        self.assertNotIn("supplierCode=0050", public_start)
        attempt = SurveyAttempt.objects.create(
            rid="Ab3dE5fG7h",
            prescreener_uid="Ab12-Cd34-Ef56-Gh78",
            survey=survey,
            platform_user=user,
            user_id=str(user.pk),
        )
        add_real_email("Real.Respondent+pool@gmail.com")
        url = CintProvider(self.integration).build_outbound_url(survey, attempt, {
            "question": {"question_id": 43, "upstream_values": ["1"]},
        })
        self.assertRegex(url, r"&hash=[A-Za-z0-9_-]{27}$")
        unsigned, signature = url.rsplit("hash=", 1)
        expected = base64.urlsafe_b64encode(
            hmac.new(b"hash-secret", unsigned.encode("utf-8"), hashlib.sha1).digest()
        ).decode("ascii").rstrip("=")
        self.assertEqual(signature, expected)
        self.assertIn("PID=Ab12-Cd34-Ef56-Gh78", url)
        self.assertIn("MID=Ab3dE5fG7h", url)
        self.assertIn("43=1", url)
        self.assertIn(
            "cint_email=" + hashlib.sha256(
                "realrespondent@gmail.com".encode("utf-8")
            ).hexdigest(),
            url,
        )
        user.email = "changed-employee-email@example.com"
        user.save(update_fields=["email"])
        retry_url = CintProvider(self.integration).build_outbound_url(survey, attempt, {})
        self.assertIn(
            "cint_email=" + hashlib.sha256(
                "realrespondent@gmail.com".encode("utf-8")
            ).hexdigest(),
            retry_url,
        )
        identity = CintRespondentEmail.objects.using(DATABASE_ALIAS).get()
        self.assertEqual(identity.assigned_uid, attempt.prescreener_uid)
        self.assertEqual(identity.use_count, 1)

    @patch("surveys.views.get_provider")
    def test_unsynced_live_cint_survey_has_copy_link_and_hydrates_on_first_start(
        self, get_provider_mock
    ):
        admin = get_user_model().objects.create_superuser(
            "cint-link-owner", "cint-link-owner@example.com", "pass"
        )
        survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_key="143479-lazy-link",
            country_code="US",
            status=Survey.Status.LIVE,
            entry_link="",
        )
        api = APIClient()
        api.force_authenticate(admin)
        listing = api.get("/api/v1/surveys/", {"search": survey.source_key})
        self.assertEqual(listing.status_code, 200)
        start_link = listing.data["results"][0]["start_link"]
        self.assertIn(f"surveyId={survey.source_key}", start_link)
        self.assertIn("supplierCode=1000", start_link)

        def hydrate(target):
            target.entry_link = "https://samplicio.us/s/default.aspx?SID=lazy-cint&PID="
            target.targeting_synced_at = timezone.now()
            target.quota_synced_at = timezone.now()
            target.detail_synced_at = timezone.now()
            target.save(update_fields=[
                "entry_link", "targeting_synced_at", "quota_synced_at",
                "detail_synced_at", "updated_at",
            ])

        get_provider_mock.return_value.refresh_details.side_effect = hydrate
        response = self.client.get(start_link)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/survey/start?rid=", response["Location"])
        get_provider_mock.assert_called_once_with(self.integration)
        get_provider_mock.return_value.refresh_details.assert_called_once()
        survey.refresh_from_db()
        self.assertTrue(survey.entry_link)

    @override_settings(PUBLIC_APP_BASE_URL="https://api.exchange-ip.com")
    @patch.dict("os.environ", {"TEST_CINT_API_KEY": "cint-secret"}, clear=False)
    def test_missing_supplier_link_is_created_with_four_platform_redirects(self):
        survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_id=143479,
            source_key="143479",
            company_name="Cint Exchange",
        )
        not_found = FakeResponse({})
        not_found.status_code = 404
        session = RecordingSession(
            {},
            {
                "ApiResult": 0,
                "SupplierLink": {
                    "LiveLink": "https://samplicio.us/s/default.aspx?SID=new-sid&PID=",
                    "TestLink": "https://samplicio.us/s/default.aspx?SID=test-sid&PID=test",
                },
            },
        )
        session.payloads[0] = not_found

        # Preserve a real response object for the expected 404 short-circuit.
        original_get = session.get
        def get(url, **kwargs):
            if session.payloads and isinstance(session.payloads[0], FakeResponse):
                session.calls.append((url, kwargs))
                return session.payloads.pop(0)
            return original_get(url, **kwargs)
        session.get = get

        provider = CintProvider(self.integration, session=session)
        provider.ensure_supplier_link(survey)
        survey.refresh_from_db()
        self.assertIn("SID=new-sid", survey.entry_link)
        payload = session.calls[1][1]["json"]
        self.assertEqual(payload["SupplierLinkTypeCode"], "OWS")
        self.assertEqual(payload["TrackingTypeCode"], "NONE")
        self.assertEqual(payload["SuccessLink"], "https://api.exchange-ip.com/survey?status=1&rid=[%MID%]")
        self.assertEqual(payload["FailureLink"], "https://api.exchange-ip.com/survey?status=2&rid=[%MID%]")
        self.assertEqual(payload["OverQuotaLink"], "https://api.exchange-ip.com/survey?status=3&rid=[%MID%]")
        self.assertEqual(payload["QualityTerminationLink"], "https://api.exchange-ip.com/survey?status=4&rid=[%MID%]")

    def test_serializer_applies_official_cint_contract_without_a_secret_value(self):
        client = Client.objects.create(code="new-cint", name="New Cint", provider_code="cint")
        serializer = ClientIntegrationSerializer(data={
            "client": client.pk,
            "name": "Production",
            "provider_code": "cint",
            "base_url": "https://api.samplicio.us",
            "credential_env_key": "CINT_API_KEY",
            "supplier_code": "1234",
            "sync_interval_seconds": 60,
            "detail_refresh_batch": 1,
            "scheduled_sync_enabled": False,
            "transaction_result_key": "",
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        integration = serializer.save()
        self.assertEqual(integration.auth_header_name, "Authorization")
        self.assertEqual(integration.quota_result_key, "SurveyQuotas")
        self.assertEqual(integration.transaction_result_key, "result")
        self.assertEqual(integration.credential_env_key, "CINT_API_KEY")
        self.assertEqual(
            integration.inventory_endpoint,
            "/Supply/v1/Surveys/AllOfferwall/ByCountryLanguage/"
            "{country_language_id}/{supplier_code}",
        )
        self.assertEqual(integration.inventory_result_key, "Surveys")
        self.assertEqual(integration.paged_inventory_endpoint, "")
        self.assertFalse(integration.encrypted_api_token)
