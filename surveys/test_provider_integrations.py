from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from vendors.models import Client, ClientIntegration
from .integrations import InnovateMRAPIError, InnovateMRClient
from .models import Survey, SurveyAttempt, TargetingQuestion
from .survey_flow import build_biobrain_outbound_url
from .views import _collect_prescreener_answers, _prescreener_questions


class FakeResponse:
    def __init__(self, payload): self.payload = payload
    def raise_for_status(self): return None
    def json(self): return self.payload


class CapturingSession:
    def __init__(self, *payloads): self.payloads = list(payloads); self.calls = []
    def get(self, url, **kwargs): self.calls.append((url, kwargs)); return FakeResponse(self.payloads.pop(0))


def integration(**overrides):
    values = {
        "provider_code": "biobrain", "base_url": "https://partner-api.voqall.com/api/v1/surveys",
        "inventory_endpoint": "", "paged_inventory_endpoint": "",
        "quota_endpoint_template": "https://partner-api.voqall.com/api/v1/survey-quotas/{survey_id}",
        "targeting_endpoint_template": "https://partner-api.voqall.com/api/v1/survey-qualifications/{survey_id}",
        "transaction_endpoint_template": "", "auth_header_name": "EQ-PARTNER-ACCESS-KEY", "auth_header_prefix": "",
        "inventory_result_key": "Surveys", "quota_result_key": "Quotas", "targeting_result_key": "Qualifications",
        "transaction_result_key": "result", "field_mapping": {}, "client": SimpleNamespace(name="Bio Brain"),
    }
    values.update(overrides); return SimpleNamespace(**values)


class ConfigurableProviderClientTests(SimpleTestCase):
    @override_settings(INNOVATEMR_API_TOKEN="global-innovate-key")
    def test_client_integration_never_borrows_global_innovate_key(self):
        client = InnovateMRClient(token="", integration=integration(provider_code="custom"))
        with self.assertRaisesRegex(InnovateMRAPIError, "token is not configured"):
            client._headers()

    def test_biobrain_uses_exact_endpoint_header_and_normalizes_inventory(self):
        session = CapturingSession(
            {"status": "ok", "hasError": False, "Surveys": [{"SurveyId": 44, "Name": "Bio study", "Revenue": 2.5, "IncidentRate": 35, "LengthOfInterview": 12, "Completes": 80, "LanguageId": 9, "SurveyUrl": "https://respond.voqall.com/l?vq_sid=44", "Has_Quotas": True, "LastUpdatedOnUTC": "2026-08-09T10:00:00Z"}]},
            {"status": "ok", "hasError": False, "Languages": [{"Id": 9, "Name": "English", "CountryCode": "US"}]},
        )
        surveys = InnovateMRClient(token="secret", session=session, integration=integration()).get_allocated_surveys()
        self.assertEqual(session.calls[0][0], "https://partner-api.voqall.com/api/v1/surveys")
        self.assertEqual(session.calls[0][1]["headers"]["EQ-PARTNER-ACCESS-KEY"], "secret")
        self.assertNotIn("x-access-token", session.calls[0][1]["headers"])
        self.assertEqual((surveys[0]["surveyId"], surveys[0]["surveyName"], surveys[0]["CPI"]), (44, "Bio study", 2.5))
        self.assertEqual((surveys[0]["CountryCode"], surveys[0]["N"], surveys[0]["supCmps"]), ("US", 80, 0))
        self.assertTrue(session.calls[1][0].endswith("/api/v1/collection/languages"))

    def test_biobrain_detail_endpoints_are_configurable(self):
        session = CapturingSession(
            {"hasError": False, "Quotas": [{"QuotaId": 7, "Conditions": []}]},
            {"hasError": False, "Qualifications": [{"QualificationId": 9, "OptionIds": [1, 2]}]},
            {"hasError": False, "Qualification": {"Id": 9, "Code": "Gender", "QuestionText": "What is your gender?", "TypeName": "Single", "Options": []}},
            {"hasError": False, "Qualification": {"Id": 9, "Code": "Gender", "QuestionText": "What is your gender?", "TypeName": "Single", "Options": [{"OptionCode": 1, "OptionText": "Male"}, {"OptionCode": 2, "OptionText": "Female"}]}},
        )
        client = InnovateMRClient(token="secret", session=session, integration=integration())
        self.assertEqual(client.get_quota_for_survey(44)[0]["id"], 7); self.assertEqual(client.get_survey_targeting(44, language_id=9)[0]["QuestionId"], 9)
        self.assertTrue(session.calls[0][0].endswith("/survey-quotas/44")); self.assertTrue(session.calls[1][0].endswith("/survey-qualifications/44"))

    def test_biobrain_targeting_uses_localized_question_and_option_labels(self):
        session = CapturingSession(
            {"hasError": False, "Qualifications": [{"QualificationId": 59, "QualificationTypeId": 1, "OptionIds": [100, 200], "OptionCodes": [1, 2]}]},
            {"hasError": False, "Qualification": {"Id": 59, "Code": "Gender", "QuestionText": "What is your gender?", "TypeName": "Single", "Options": [{"OptionCode": 1, "OptionText": "Male"}, {"OptionCode": 2, "OptionText": "Female"}]}},
        )
        question = InnovateMRClient(
            token="secret", session=session, integration=integration()
        ).get_survey_targeting(44, language_id=9)[0]
        self.assertEqual(question["QuestionText"], "What is your gender?")
        self.assertEqual(question["QuestionKey"], "Gender")
        self.assertEqual(question["Options"], [
            {"OptionId": 100, "OptionCode": 1, "OptionText": "Male", "Qualifies": True},
            {"OptionId": 200, "OptionCode": 2, "OptionText": "Female", "Qualifies": True},
        ])

    def test_biobrain_uses_api2_when_legacy_host_returns_incomplete_metadata(self):
        session = CapturingSession(
            {"hasError": False, "Qualifications": [{"QualificationId": 60, "QualificationTypeId": 1, "OptionIds": [901, 902, 903], "OptionCodes": [1, 2, 3]}]},
            {"hasError": False, "Qualification": {"Id": 60, "Code": "GENDER", "TypeName": "SINGLE SELECT"}},
            {"hasError": False, "Qualification": {"Id": 60, "Code": "GENDER", "QuestionText": "What is your gender?", "TypeName": "SINGLE SELECT", "Options": [{"OptionCode": 1, "OptionText": "Male"}, {"OptionCode": 2, "OptionText": "Female"}, {"OptionCode": 3, "OptionText": "Other"}]}},
        )

        question = InnovateMRClient(
            token="secret", session=session, integration=integration()
        ).get_survey_targeting(44, language_id=9)[0]

        self.assertEqual([option["OptionText"] for option in question["Options"]], ["Male", "Female", "Other"])
        self.assertTrue(question["metadata_hydrated"])
        self.assertIn("partner-api2.voqall.com", session.calls[2][0])

    def test_biobrain_gender_has_readable_fallback_when_provider_omits_labels(self):
        session = CapturingSession(
            {"hasError": False, "Qualifications": [{"QualificationId": 60, "QualificationTypeId": 1, "OptionIds": [901, 902, 903], "OptionCodes": [1, 2, 3]}]},
            {"hasError": False, "Qualification": {"Id": 60, "Code": "GENDER", "TypeName": "SINGLE SELECT"}},
            {"hasError": False, "Qualification": {"Id": 60, "Code": "GENDER", "TypeName": "SINGLE SELECT"}},
        )

        question = InnovateMRClient(
            token="secret", session=session, integration=integration()
        ).get_survey_targeting(44, language_id=9)[0]

        self.assertEqual(question["QuestionText"], "What is your gender?")
        self.assertEqual([option["OptionText"] for option in question["Options"]], ["Male", "Female", "Other"])
        self.assertTrue(question["metadata_hydrated"])

    def test_biobrain_recovers_missing_language_and_varied_metadata_casing(self):
        session = CapturingSession(
            {"hasError": False, "Qualifications": [{"QualificationId": 60, "QualificationTypeId": 1, "OptionIds": [901, 902], "OptionCodes": [1, 2]}]},
            {"hasError": False, "Surveys": [{"SurveyId": 44, "LanguageID": 9}]},
            {"hasError": False, "data": {"qualification": {"id": 60, "code": "Gender", "questionText": "What is your gender?", "typeName": "Single", "options": [{"id": 901, "code": 1, "label": "Male"}, {"id": 902, "code": 2, "label": "Female"}]}}},
        )

        question = InnovateMRClient(
            token="secret", session=session, integration=integration()
        ).get_survey_targeting(44)[0]

        self.assertEqual(question["QuestionText"], "What is your gender?")
        self.assertEqual(question["QuestionKey"], "Gender")
        self.assertEqual([option["OptionText"] for option in question["Options"]], ["Male", "Female"])
        self.assertTrue(session.calls[1][0].endswith("/api/v1/surveys"))
        self.assertTrue(session.calls[2][0].endswith("/collection/languages/9/qualifications/60"))

    def test_biobrain_quota_conditions_use_readable_question_and_answers(self):
        session = CapturingSession(
            {"hasError": False, "Quotas": [{"QuotaId": 7, "Conditions": [{"QualificationId": 60, "QualificationTypeId": 1, "OptionIds": [901], "OptionCodes": [1]}]}]},
            {"hasError": False, "Qualification": {"Id": 60, "Code": "Gender", "QuestionText": "What is your gender?", "TypeName": "Single", "Options": [{"OptionCode": 1, "OptionText": "Male"}]}},
        )

        quota = InnovateMRClient(
            token="secret", session=session, integration=integration()
        ).get_quota_for_survey(44, language_id=9)[0]

        self.assertEqual(quota["targeting_details"], [{"name": "What is your gender?", "values": ["Male"]}])

    def test_biobrain_outbound_url_uses_canonical_rid_and_profile_uid(self):
        outbound = build_biobrain_outbound_url(
            "https://respond.voqall.com/l?vq_sid=44&vq_vid=7&vq_token=[#vq_tid#]&vq_uid=[#vq_tuid#]",
            "Abc123xYz9",
            "uidA-123",
            {
                "age": {"question_id": 59, "question_key": "AGE", "upstream_values": [33]},
                "gender": {"question_id": 60, "question_key": "GENDER", "upstream_values": [58], "provider_option_codes": ["1"]},
                "zip": {"question_id": 61, "question_key": "POSTAL_CODE", "upstream_values": ["10001"]},
            },
        )
        self.assertIn("vq_token=Abc123xYz9", outbound)
        self.assertIn("vq_uid=uidA-123", outbound)
        self.assertIn("Q59=33", outbound)
        self.assertIn("Q60=58", outbound)
        self.assertIn("Q61=10001", outbound)
        self.assertIn("age=33", outbound)
        self.assertIn("gender=1", outbound)
        self.assertIn("zip=10001", outbound)
        self.assertNotIn("%23", outbound)
        self.assertNotIn("trackId=", outbound)

    def test_custom_provider_field_mapping(self):
        session = CapturingSession({"data": {"items": [{"id": 8, "title": "Custom study"}]}})
        configured = integration(provider_code="custom", base_url="https://example.test/api", inventory_endpoint="surveys", auth_header_name="X-API-Key", inventory_result_key="data.items", field_mapping={"surveyId": "id", "surveyName": "title"})
        survey = InnovateMRClient(token="secret", session=session, integration=configured).get_allocated_surveys()[0]
        self.assertEqual(session.calls[0][0], "https://example.test/api/surveys"); self.assertEqual((survey["surveyId"], survey["surveyName"]), (8, "Custom study"))


class BioBrainPrescreenerCompatibilityTests(TestCase):
    def test_biobrain_passes_age_and_postal_to_provider_without_local_quota_rejection(self):
        client = Client.objects.create(
            code="biobrain-bypass-test", name="BioBrain", provider_code="biobrain"
        )
        integration = ClientIntegration.objects.create(
            client=client,
            name="BioBrain validation test",
            provider_code="biobrain",
            base_url="https://partner-api.voqall.com/api/v1/surveys",
        )
        survey = Survey.objects.create(
            source_id=45, source_key="45", client=client, integration=integration,
            company_name="BioBrain",
        )
        age = TargetingQuestion.objects.create(
            survey=survey, question_id=60, key="AGE", text="What is your age?",
            question_type="Numeric", options=[{"OptionId": 1, "OptionText": "18-24", "ageStart": 18, "ageEnd": 24}],
        )
        postal = TargetingQuestion.objects.create(
            survey=survey, question_id=61, key="POSTAL_CODE", text="What is your postal code?",
            question_type="Text", raw_data={"targeting_choices": ["10001"]},
        )

        request = RequestFactory().post("/", {
            f"question_{age.pk}": "65",
            f"question_{postal.pk}": "99999",
        })
        answers, errors = _collect_prescreener_answers(request, survey)

        self.assertEqual(errors, [])
        self.assertEqual(answers[str(age.pk)]["upstream_values"], ["65"])
        self.assertEqual(answers[str(postal.pk)]["upstream_values"], ["99999"])

    def test_legacy_numeric_options_render_without_server_error(self):
        survey = Survey.objects.create(
            source_id=44,
            source_key="44",
            name="Bio study",
            company_name="BioBrain",
        )
        TargetingQuestion.objects.create(
            survey=survey,
            question_id=59,
            key="Q59",
            text="What is your gender?",
            question_type="Single",
            options=[100, 200],
        )

        question = next(
            item for item in _prescreener_questions(survey)
            if item["model"].question_id == 59
        )

        self.assertEqual(
            question["options"],
            [
                {"value": "100", "label": "100", "selected": False},
                {"value": "200", "label": "200", "selected": False},
            ],
        )


class BioBrainDataPageTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_superuser(
            username="biobrain-owner", email="owner@example.test", password="test-password"
        )
        self.employee = get_user_model().objects.create_user(
            username="biobrain-employee", password="test-password"
        )
        client = Client.objects.create(
            code="biobrain-data-test", name="BioBrain", provider_code="biobrain"
        )
        integration = ClientIntegration.objects.create(
            client=client,
            name="BioBrain Data test",
            provider_code="biobrain",
            base_url="https://partner-api.voqall.com/api/v1/surveys",
        )
        survey = Survey.objects.create(
            source_id=998811, source_key="998811", client=client,
            integration=integration, company_name="BioBrain",
        )
        question = TargetingQuestion.objects.create(
            survey=survey, question_id=59, key="Gender",
            text="What is your gender?", question_type="Single",
            options=[{"OptionId": 2, "OptionText": "Female"}],
        )
        self.attempt = SurveyAttempt.objects.create(
            rid="Bi0DataP1g", survey=survey, platform_user=self.employee,
            user_id=str(self.employee.pk), submitted_at=timezone.now(),
            answers={str(question.pk): {
                "question_id": question.question_id,
                "question_text": question.text,
                "question_category": "BioBrain targeting",
                "values": ["2"],
            }},
        )

    def test_super_admin_can_view_filled_biobrain_answers(self):
        self.client.force_login(self.owner)

        response = self.client.get(reverse("biobrain-data"), {"search": self.attempt.rid})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.attempt.rid)
        self.assertContains(response, "What is your gender?")
        self.assertContains(response, "Female")
        self.assertContains(response, "Data")

    def test_regular_user_cannot_open_biobrain_data(self):
        self.client.force_login(self.employee)

        self.assertEqual(self.client.get(reverse("biobrain-data")).status_code, 403)
