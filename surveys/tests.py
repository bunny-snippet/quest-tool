from datetime import timedelta
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from .integrations import InnovateMRClient, InnovateMRNotFound, PagedSurveyResult
from .models import Survey, SurveyAttempt, SurveyQuota, SyncRun, TargetingQuestion
from .services import merge_inventory, parse_upstream_datetime, replace_survey_details, sync_surveys


def survey_payload(survey_id=12632, modified="09/11/2017, 11:50:27 pm PST", **overrides):
    payload = {
        "surveyId": survey_id,
        "surveyName": "Beverage habits",
        "N": 100,
        "supCmps": 3,
        "remainingN": 97,
        "LOI": 15,
        "IR": 10,
        "Country": "United States",
        "CountryCode": "US",
        "Language": "ENGLISH",
        "LanguageCode": "EN",
        "groupType": "Consumer",
        "deviceType": "All",
        "createdDate": "09/11/2017, 11:03:50 pm PST",
        "modifiedDate": modified,
        "entryLink": "https://example.test/start?pid=[%%pid%%]",
        "CPI": "4.50",
        "isQuota": True,
        "numberOfStarts": 4,
    }
    payload.update(overrides)
    return payload


class FakeClient:
    def __init__(self, full=None, paged=None):
        self.full = full or []
        self.paged = paged or []

    def get_allocated_surveys(self):
        return self.full

    def get_allocated_surveys_paged(self):
        return PagedSurveyResult(self.paged, 1)

    def get_quota_for_survey(self, survey_id):
        return [{"_id": "quota-a", "id": 780275, "quotaN": 10, "RemainingN": 9, "cmp": 1, "quotaStatus": "Open", "targeting": {"AGE": [{"ageStart": 18, "ageEnd": 35}]}}]

    def get_survey_targeting(self, survey_id):
        return [{"QuestionId": 2, "QuestionKey": "GENDER", "QuestionText": "What is your gender?", "QuestionType": "Single Punch", "QuestionCategory": "Demographic", "Options": [{"OptionId": 1, "OptionText": "Male"}]}]


class MergeAndDateTests(TestCase):
    def test_latest_modified_payload_wins_across_sources(self):
        older = survey_payload(surveyName="Old name")
        newer = survey_payload(modified="10/09/2017, 9:26:27 am PST", surveyName="New name")
        self.assertEqual(merge_inventory([older], [newer])[12632]["surveyName"], "New name")

    def test_pst_is_converted_to_utc(self):
        parsed = parse_upstream_datetime("09/11/2017, 11:50:27 pm PST")
        self.assertEqual(parsed.utcoffset(), timedelta(0))
        self.assertEqual(parsed.hour, 7)


class SurveySyncTests(TestCase):
    def test_sync_creates_one_deduplicated_survey_with_local_id(self):
        full = survey_payload(surveyName="Older")
        paged = survey_payload(modified="10/09/2017, 9:26:27 am PST", surveyName="Newest")
        summary = sync_surveys(FakeClient([full], [paged]))
        survey = Survey.objects.get(source_id=12632)
        self.assertEqual(summary.created, 1)
        self.assertEqual(survey.name, "Newest")
        self.assertEqual(len(survey.local_id), 14)
        self.assertTrue(survey.local_id.isdigit())
        self.assertEqual(survey.local_id[:6], timezone.localdate().strftime("%Y%m"))
        self.assertEqual(SyncRun.objects.get(pk=summary.run_id).fetched_paged, 1)

    def test_sync_updates_newer_record_and_closes_disappeared_survey(self):
        sync_surveys(FakeClient([survey_payload(1), survey_payload(2)], []))
        updated = survey_payload(1, modified="10/09/2017, 9:26:27 am PST", surveyName="Changed")
        summary = sync_surveys(FakeClient([updated], [updated]))
        self.assertEqual(summary.updated, 1)
        self.assertEqual(summary.closed, 1)
        self.assertEqual(Survey.objects.get(source_id=2).status, Survey.Status.CLOSED)

    def test_detail_replacement_is_atomic_and_normalized(self):
        survey = Survey.objects.create(source_id=12632, name="Test")
        replace_survey_details(FakeClient(), survey)
        self.assertEqual(SurveyQuota.objects.get().remaining, 9)
        self.assertEqual(TargetingQuestion.objects.get().key, "GENDER")
        survey.refresh_from_db()
        self.assertIsNotNone(survey.detail_synced_at)
        self.assertIsNotNone(survey.quota_synced_at)
        self.assertIsNotNone(survey.targeting_synced_at)


class InnovateMRClientTests(TestCase):
    @override_settings(INNOVATEMR_API_TOKEN="secret-test-token", INNOVATEMR_MAX_PAGES=5)
    def test_paged_client_follows_cursor_without_leaking_token_to_query(self):
        first = Mock()
        first.raise_for_status.return_value = None
        first.json.return_value = {"apiStatus": "success", "result": [{"surveyId": 1}], "paging": {"next": "abc"}}
        second = Mock()
        second.raise_for_status.return_value = None
        second.json.return_value = {"apiStatus": "success", "result": [{"surveyId": 2}], "paging": {}}
        session = Mock()
        session.get.side_effect = [first, second]
        result = InnovateMRClient(session=session).get_allocated_surveys_paged()
        self.assertEqual([row["surveyId"] for row in result.surveys], [1, 2])
        self.assertEqual(session.get.call_args_list[1].kwargs["params"]["next"], "abc")
        self.assertEqual(session.get.call_args_list[0].kwargs["headers"]["x-access-token"], "secret-test-token")


class SurveyAPITests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.user = get_user_model().objects.create_user(username="employee", password="test-password")
        self.api.force_authenticate(self.user)
        self.client.force_login(self.user)
        self.survey = Survey.objects.create(
            source_id=9876,
            name="Mobile banking survey",
            country="United States",
            country_code="US",
            language_code="EN",
            status=Survey.Status.LIVE,
            sample_size=50,
            completes=10,
            entry_link="https://edgeapi.innovatemr.net/startSurvey?survNum=test&supCode=1150&PID=[%%pid%%]",
            source_modified_at=timezone.now() - timedelta(hours=2),
            detail_synced_at=timezone.now(),
            quota_synced_at=timezone.now(),
            targeting_synced_at=timezone.now(),
        )
        SurveyQuota.objects.create(survey=self.survey, source_key="q1", quota_id=1, sample_size=20, remaining=10)
        TargetingQuestion.objects.create(survey=self.survey, question_id=2, key="GENDER", text="Gender?", options=[])

    def test_list_filter_and_search(self):
        response = self.api.get(reverse("survey-list"), {"country": "US", "search": "banking"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["local_id"], self.survey.local_id)
        self.assertEqual(response.data["results"][0]["company_name"], "InnovateMR")
        self.assertIn("source_modified_display", response.data["results"][0])
        self.assertEqual(
            response.data["results"][0]["start_link"],
            f"http://testserver/survey/start?surveyId=9876&supplierCode=1150&userId={self.user.pk}&code={self.survey.local_id}",
        )

    def test_multi_value_filters_use_or_within_each_filter(self):
        Survey.objects.create(
            source_id=9877,
            company_name="Sample Partner",
            name="India finance survey",
            country="India",
            country_code="IN",
            status=Survey.Status.CLOSED,
        )
        response = self.api.get(reverse("survey-list"), {
            "country": "US,IN",
            "status": "live,closed",
            "company": "InnovateMR,Sample Partner",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)

    def test_detail_actions_return_cached_data(self):
        quota = self.api.get(reverse("survey-quotas", kwargs={"local_id": self.survey.local_id}))
        targeting = self.api.get(reverse("survey-targeting", kwargs={"local_id": self.survey.local_id}))
        self.assertEqual(quota.status_code, 200)
        self.assertEqual(quota.data[0]["quota_id"], 1)
        self.assertEqual(targeting.data[0]["key"], "GENDER")

    def test_missing_upstream_quota_is_an_empty_successful_result(self):
        self.survey.quota_synced_at = None
        self.survey.save(update_fields=["quota_synced_at"])
        upstream = Mock()
        upstream.get_quota_for_survey.side_effect = InnovateMRNotFound("no quota")
        with patch("surveys.views.InnovateMRClient", return_value=upstream):
            response = self.api.get(reverse("survey-quotas", kwargs={"local_id": self.survey.local_id}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])
        self.survey.refresh_from_db()
        self.assertIsNotNone(self.survey.quota_synced_at)

    def test_projects_and_dashboard_render(self):
        projects = self.client.get(reverse("projects"))
        self.assertContains(projects, "Survey inventory")
        self.assertContains(projects, "Pre-screening questions")
        self.assertNotContains(projects, "Quest")
        self.assertContains(self.client.get(reverse("dashboard")), "dashboard is ready")


class SurveyFlowTests(TestCase):
    def setUp(self):
        now = timezone.now()
        self.platform_user = get_user_model().objects.create_user(
            id=294, username="respondent", password="test-password"
        )
        self.survey = Survey.objects.create(
            source_id=32655971,
            name="Financial services",
            status=Survey.Status.LIVE,
            company_name="InnovateMR",
            country_code="US",
            language_code="EN",
            loi=12,
            entry_link="https://edgeapi.innovatemr.net/startSurvey?survNum=v8wdQrgP&supCode=1150&PID=[%%pid%%]",
            source_modified_at=now - timedelta(hours=1),
            targeting_synced_at=now,
        )
        self.question = TargetingQuestion.objects.create(
            survey=self.survey,
            question_id=2,
            key="GENDER",
            text="What is your gender?",
            question_type="Single Punch",
            category="Demographic",
            options=[{"OptionId": 1, "OptionText": "Male"}, {"OptionId": 2, "OptionText": "Female"}],
        )

    def test_full_prescreener_redirect_and_status_lifecycle(self):
        start = self.client.get(reverse("survey-start"), {
            "surveyId": self.survey.source_id,
            "supplierCode": "1150",
            "userId": "294",
            "code": self.survey.local_id,
        }, REMOTE_ADDR="10.10.10.10")
        self.assertEqual(start.status_code, 302)
        rid = parse_qs(urlsplit(start["Location"]).query)["rid"][0]
        self.assertEqual(len(rid), 10)
        self.assertTrue(any(char.isupper() for char in rid))
        self.assertTrue(any(char.islower() for char in rid))
        self.assertTrue(any(char.isdigit() for char in rid))

        form = self.client.get(reverse("survey-start"), {"rid": rid})
        self.assertContains(form, "What is your gender?")

        submit = self.client.post(reverse("survey-start"), {
            "rid": rid,
            f"question_{self.question.pk}": "2",
        })
        self.assertEqual(submit.status_code, 302)
        outbound = urlsplit(submit["Location"])
        params = parse_qs(outbound.query)
        self.assertEqual(params["PID"], [rid])
        self.assertEqual(params["trackId"], [rid])
        self.assertEqual(params["GENDER"], ["2"])

        callback = self.client.get(reverse("survey-status"), {"status": "1", "rid": rid}, REMOTE_ADDR="20.20.20.20")
        self.assertEqual(callback.status_code, 200)
        self.assertContains(callback, "Thank you for participating!")
        attempt = SurveyAttempt.objects.get(rid=rid)
        self.assertEqual(attempt.status, SurveyAttempt.Status.COMPLETED)
        self.assertEqual(attempt.platform_user, self.platform_user)
        self.assertEqual(attempt.user_id, "294")
        self.assertEqual(attempt.supplier_code, "1150")
        self.assertEqual(attempt.initiation_ip, "10.10.10.10")
        self.assertEqual(attempt.callback_ip, "20.20.20.20")
        self.assertIsNotNone(attempt.loi_seconds)

    def test_status_requires_known_rid(self):
        response = self.client.get(reverse("survey-status"), {"status": "3", "rid": "Aa1Bb2Cc3D"})
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "could not be attached", status_code=404)

    def test_invalid_start_values_never_create_attempt_or_show_questions(self):
        valid = {
            "surveyId": str(self.survey.source_id),
            "supplierCode": "1150",
            "userId": str(self.platform_user.pk),
            "code": self.survey.local_id,
        }
        invalid_variants = [
            {**valid, "userId": "999999"},
            {**valid, "code": "20260800000000"},
            {**valid, "supplierCode": "9999"},
            {**valid, "unexpected": "injected"},
        ]

        for query in invalid_variants:
            with self.subTest(query=query):
                response = self.client.get(reverse("survey-start"), query)
                self.assertIn(response.status_code, {400, 404})
                self.assertContains(response, "Invalid survey link", status_code=response.status_code)
                self.assertNotContains(response, "What is your gender?", status_code=response.status_code)

        self.assertEqual(SurveyAttempt.objects.count(), 0)

    def test_canonical_rid_rejects_extra_params_and_inactive_user(self):
        start = self.client.get(reverse("survey-start"), {
            "surveyId": self.survey.source_id,
            "supplierCode": "1150",
            "userId": self.platform_user.pk,
            "code": self.survey.local_id,
        })
        rid = parse_qs(urlsplit(start["Location"]).query)["rid"][0]

        injected = self.client.get(reverse("survey-start"), {"rid": rid, "userId": self.platform_user.pk})
        self.assertContains(injected, "Invalid survey link", status_code=400)

        self.platform_user.is_active = False
        self.platform_user.save(update_fields=["is_active"])
        inactive = self.client.get(reverse("survey-start"), {"rid": rid})
        self.assertContains(inactive, "Invalid survey link", status_code=404)
