import base64
import hashlib
import hmac
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory

from vendors.models import Client, ClientIntegration
from vendors.serializers import ClientIntegrationSerializer

from .models import ProviderQuestionMapping, Survey, SurveyAttempt, TargetingQuestion
from .provider_services import sync_client_integration
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


DEFINITIONS = {
    "ApiResult": 0,
    "AllCountryLanguages": [
        {"Id": "9", "Code": "ENG-US", "Name": "English - United States"}
    ],
    "AllSampleTypes": [
        {"Id": "1", "Code": "B2C", "Name": "Consumer"},
        {"Id": "2", "Code": "B2B", "Name": "Business-to-business"},
    ],
    "AllStudyTypes": [{"Id": "1", "Code": "ADH", "Name": "Ad hoc"}],
}


class CintProviderTests(TestCase):
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

    @patch.dict("os.environ", {"TEST_CINT_API_KEY": "cint-secret"}, clear=False)
    def test_inventory_merges_open_and_allocated_surveys(self):
        session = RecordingSession(
            DEFINITIONS,
            {
                "ApiResult": 0,
                "SupplierAllocationSurveys": [{
                    "SurveyName": "Allocated business study",
                    "SurveyNumber": 143479,
                    "AccountName": "Buyer B",
                    "CountryLanguageID": 9,
                    "LengthOfInterview": 8,
                    "BidIncidence": 45,
                    "SampleTypeID": 2,
                    "StudyTypeID": 1,
                }],
            },
            {
                "ApiResult": 0,
                "Surveys": [{
                    "SurveyName": "Open consumer study",
                    "SurveyNumber": 457751,
                    "AccountName": "Buyer A",
                    "CountryLanguageID": 9,
                    "LengthOfInterview": 12,
                    "BidIncidence": 30,
                    "RPI": {"Value": 1.35, "CurrencyCode": "USD"},
                    "OverallCompletes": 5,
                    "TotalRemaining": 95,
                    "SampleTypeID": 1,
                    "StudyTypeID": 1,
                }],
            },
        )
        provider = CintProvider(self.integration, session=session)
        with patch("surveys.provider_services.get_provider", return_value=provider):
            run = sync_client_integration(self.integration)

        self.assertEqual((run.created, run.unique_surveys), (2, 2))
        open_survey = Survey.objects.get(integration=self.integration, source_key="457751")
        allocated = Survey.objects.get(integration=self.integration, source_key="143479")
        self.assertEqual(open_survey.cpi, Decimal("1.35"))
        self.assertEqual((open_survey.completes, open_survey.remaining), (5, 95))
        self.assertEqual(open_survey.country_code, "US")
        self.assertEqual(open_survey.language_code, "ENG")
        self.assertEqual(open_survey.survey_type, "B2C")
        self.assertEqual(allocated.survey_type, "B2B")
        self.assertIsNone(allocated.cpi)
        self.assertEqual(allocated.entry_link, "")
        self.assertEqual(session.calls[0][1]["headers"]["Authorization"], "cint-secret")
        self.assertTrue(session.calls[1][0].endswith(
            "/Supply/v1/Surveys/SupplierAllocations/All/0050"
        ))
        self.assertTrue(session.calls[2][0].endswith("/Supply/v1/Surveys/AllOfferwall/0050"))

    @patch.dict("os.environ", {"TEST_CINT_API_KEY": "cint-secret"}, clear=False)
    def test_inventory_keeps_allocated_surveys_when_open_opportunities_fail(self):
        provider = CintProvider(self.integration, session=RecordingSession())
        allocated = {
            "ApiResult": 0,
            "SupplierAllocationSurveys": [{"SurveyNumber": 143479}],
        }
        with patch.object(provider, "_load_definitions"), patch.object(
            provider, "_request", side_effect=[allocated, ProviderError("open inventory timeout")]
        ):
            rows = provider.inventory()

        self.assertEqual([row["SurveyNumber"] for row in rows], [143479])

    @patch.dict("os.environ", {"TEST_CINT_API_KEY": "cint-secret"}, clear=False)
    def test_inventory_sync_preserves_hydrated_supplier_link_when_list_omits_it(self):
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
        session = RecordingSession(
            DEFINITIONS,
            {
                "ApiResult": 0,
                "SupplierAllocationSurveys": [{
                    "SurveyNumber": 143479,
                    "SurveyName": "Allocated survey without embedded link",
                    "CountryLanguageID": 9,
                }],
            },
            {"ApiResult": 0, "Surveys": []},
        )
        provider = CintProvider(self.integration, session=session)
        with patch("surveys.provider_services.get_provider", return_value=provider):
            sync_client_integration(self.integration)

        survey.refresh_from_db()
        self.assertIn("SID=live-sid", survey.entry_link)
        self.assertIn("SID=test-sid", survey.test_entry_link)
        self.assertEqual(
            survey.raw_data["_cint_supplier_link"]["SupplierLinkID"], 99
        )

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
                "targeting_age_ranges": [{"min": 18, "max": 20}],
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
                "exampleuser@gmail.com".encode("utf-8")
            ).hexdigest(),
            url,
        )

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
        self.assertFalse(integration.encrypted_api_token)
