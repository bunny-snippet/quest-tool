import csv
import os
import zipfile
from datetime import datetime, time, timedelta
from decimal import Decimal
from io import BytesIO, StringIO
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo
from xml.etree import ElementTree

from django.test import RequestFactory, TestCase, override_settings
from django.contrib.auth import get_user_model
from django.core.cache import caches
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import AccessFunction, EmployeeProfile, Role, UserFunctionOverride
from vendors.models import Client, ClientIntegration, OrganizationUnit, VendorCommercialProfile

from .dashboard import dashboard_comparison_window, dashboard_range_window
from .integrations import InnovateMRClient, InnovateMRNotFound, PagedSurveyResult
from .identifiers import generate_platform_pid, is_valid_platform_pid
from .models import (
    Survey,
    SurveyAttempt,
    SurveyProjectEntryIPClaim,
    SurveyQuota,
    SyncLease,
    SyncRun,
    TargetingQuestion,
)
from .providers.base import ProviderError
from .services import (
    merge_inventory,
    parse_upstream_datetime,
    reconcile_attempt_status,
    replace_survey_details,
    sync_surveys,
)
from .survey_flow import build_outbound_url, create_attempt
from .views import (
    PRESCREENER_MAX_LIST_VALUES,
    PRESCREENER_MAX_TEXT_LENGTH,
    _collect_prescreener_answers,
    _prescreener_questions,
)


def xlsx_rows(response, sheet_number=1):
    content = b"".join(response.streaming_content)
    with zipfile.ZipFile(BytesIO(content)) as workbook:
        root = ElementTree.fromstring(workbook.read(f"xl/worksheets/sheet{sheet_number}.xml"))
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows = []
    for row in root.findall(".//x:sheetData/x:row", namespace):
        values = []
        for cell in row.findall("x:c", namespace):
            inline = cell.find("x:is/x:t", namespace)
            numeric = cell.find("x:v", namespace)
            values.append(inline.text if inline is not None else numeric.text if numeric is not None else "")
        rows.append(values)
    return rows


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
        "BuyerId": 3690,
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

    def test_pst_label_uses_pacific_daylight_saving_offset(self):
        parsed = parse_upstream_datetime("09/11/2017, 11:50:27 pm PST")
        self.assertEqual(parsed.utcoffset(), timedelta(0))
        self.assertEqual(parsed.hour, 6)

    def test_summer_completion_time_converts_to_exact_ist_end_time(self):
        parsed = parse_upstream_datetime("08/08/2026, 3:46:24 am PST")
        ist = parsed.astimezone(ZoneInfo("Asia/Kolkata"))
        self.assertEqual((ist.hour, ist.minute, ist.second), (16, 16, 24))


class SurveySyncTests(TestCase):
    def test_sync_creates_one_deduplicated_survey_with_local_id(self):
        full = survey_payload(surveyName="Older")
        paged = survey_payload(modified="10/09/2017, 9:26:27 am PST", surveyName="Newest")
        summary = sync_surveys(FakeClient([full], [paged]))
        survey = Survey.objects.get(source_id=12632)
        self.assertEqual(summary.created, 1)
        self.assertEqual(survey.name, "Newest")
        self.assertEqual(survey.buyer_id, "3690")
        self.assertEqual(survey.survey_type, "B2C")
        self.assertEqual(survey.client, Client.objects.get(code="innovatemr"))
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

    @patch("surveys.services.invalidate_project_cache")
    def test_large_unchanged_sync_uses_bounded_queries_and_keeps_cache_warm(self, invalidate):
        integration = ClientIntegration.objects.filter(provider_code="innovatemr").first()
        payloads = [survey_payload(index) for index in range(1, 41)]

        created = sync_surveys(FakeClient(payloads, []), integration=integration)
        self.assertEqual(created.created, 40)
        self.assertEqual(Survey.objects.filter(integration=integration).count(), 40)
        self.assertEqual(
            Survey.objects.filter(integration=integration).values("local_id").distinct().count(),
            40,
        )
        invalidate.assert_called_once()

        invalidate.reset_mock()
        with CaptureQueriesContext(connection) as queries:
            unchanged = sync_surveys(FakeClient(payloads, []), integration=integration)

        self.assertEqual(unchanged.unchanged, 40)
        self.assertEqual(
            Survey.objects.filter(
                integration=integration,
                status=Survey.Status.LIVE,
            ).count(),
            40,
        )
        # Query count is bounded by inventory batches, not by survey count.
        # This protects the live 90k+ inventory from regressing to one SELECT
        # and one UPDATE per item.
        self.assertLess(len(queries), 15)
        invalidate.assert_not_called()

    def test_detail_replacement_is_atomic_and_normalized(self):
        survey = Survey.objects.create(source_id=12632, name="Test")
        replace_survey_details(FakeClient(), survey)
        self.assertEqual(SurveyQuota.objects.get().remaining, 9)
        self.assertEqual(TargetingQuestion.objects.get().key, "GENDER")
        survey.refresh_from_db()
        self.assertIsNotNone(survey.detail_synced_at)
        self.assertIsNotNone(survey.quota_synced_at)
        self.assertIsNotNone(survey.targeting_synced_at)

    @override_settings(
        CLIENT_INTEGRATION_INNOVATEMR_SYNC_INTERVAL_SECONDS=150,
        CLIENT_INTEGRATION_RFG_SYNC_INTERVAL_SECONDS=600,
    )
    @patch("surveys.tasks.sync_client_integration_task.delay")
    def test_dispatcher_automates_innovatemr_and_verified_rfg_at_fixed_intervals(self, delay):
        from .tasks import dispatch_due_integrations_task

        ClientIntegration.objects.all().delete()
        now = timezone.now()
        innovate_client = Client.objects.create(code="auto-innovate", name="Auto Innovate", provider_code="innovatemr")
        rfg_client = Client.objects.create(code="auto-rfg", name="Auto RFG", provider_code="rfg")
        custom_client = Client.objects.create(code="manual-custom", name="Manual Custom", provider_code="custom")
        innovate = ClientIntegration.objects.create(
            client=innovate_client, name="Innovate automatic", provider_code="innovatemr",
            base_url="https://supplier.innovatemr.net/api/v2", sync_interval_seconds=150,
            scheduled_sync_enabled=False, last_sync_started_at=now - timedelta(seconds=151),
        )
        rfg = ClientIntegration.objects.create(
            client=rfg_client, name="RFG automatic", provider_code="rfg",
            base_url="https://api.researchforgood.com/API", sync_interval_seconds=600,
            scheduled_sync_enabled=False, last_test_status="success",
            last_sync_started_at=now - timedelta(seconds=601),
        )
        ClientIntegration.objects.create(
            client=custom_client, name="Custom manual", provider_code="custom",
            base_url="https://example.test/api", sync_interval_seconds=60,
            scheduled_sync_enabled=False, last_sync_started_at=now - timedelta(days=1),
        )
        ClientIntegration.objects.create(
            client=rfg_client, name="RFG unverified", provider_code="rfg",
            base_url="https://api.researchforgood.com/API", sync_interval_seconds=600,
            scheduled_sync_enabled=False, last_test_status="",
            last_sync_started_at=now - timedelta(days=1),
        )

        result = dispatch_due_integrations_task()

        self.assertEqual(result["count"], 2)
        self.assertEqual({call.args[0] for call in delay.call_args_list}, {innovate.pk, rfg.pk})
        self.assertEqual(
            set(ClientIntegration.objects.filter(last_sync_status="queued").values_list("pk", flat=True)),
            {innovate.pk, rfg.pk},
        )


    @override_settings(
        CLIENT_INTEGRATION_INNOVATEMR_SYNC_INTERVAL_SECONDS=180,
        CLIENT_INTEGRATION_RFG_SYNC_INTERVAL_SECONDS=60,
    )
    def test_effective_interval_respects_record_environment_and_provider_floors(self):
        from .tasks import effective_sync_interval_seconds

        client = Client.objects.create(
            code="interval-floors", name="Interval floors", provider_code="rfg"
        )
        integration = ClientIntegration.objects.create(
            client=client,
            name="RFG interval floors",
            provider_code="rfg",
            base_url="https://api.researchforgood.com/API",
            sync_interval_seconds=900,
        )
        self.assertEqual(effective_sync_interval_seconds(integration), 900)

        integration.sync_interval_seconds = 60
        self.assertEqual(effective_sync_interval_seconds(integration), 600)

        integration.provider_code = "innovatemr"
        integration.sync_interval_seconds = 60
        self.assertEqual(effective_sync_interval_seconds(integration), 180)


    @override_settings(CLIENT_INTEGRATION_RFG_SYNC_INTERVAL_SECONDS=600)
    @patch("surveys.tasks.sync_client_integration_task.delay")
    def test_dispatcher_skips_recent_queue_marker_but_recovers_stale_marker(self, delay):
        from .tasks import dispatch_due_integrations_task

        ClientIntegration.objects.all().delete()
        client = Client.objects.create(
            code="rfg-queue-guard", name="RFG queue guard", provider_code="rfg"
        )
        recent = ClientIntegration.objects.create(
            client=client,
            name="Recent queued RFG",
            provider_code="rfg",
            base_url="https://api.researchforgood.com/API",
            sync_interval_seconds=600,
            last_test_status="success",
            last_sync_status="queued",
            last_sync_started_at=timezone.now() - timedelta(seconds=601),
        )
        stale = ClientIntegration.objects.create(
            client=client,
            name="Stale running RFG",
            provider_code="rfg",
            base_url="https://api.researchforgood.com/API",
            sync_interval_seconds=600,
            last_test_status="success",
            last_sync_status="running",
            last_sync_started_at=timezone.now() - timedelta(seconds=1801),
        )

        result = dispatch_due_integrations_task()

        self.assertEqual(result, {"queued": [stale.pk], "count": 1})
        delay.assert_called_once_with(stale.pk)
        recent.refresh_from_db()
        self.assertEqual(recent.last_sync_status, "queued")


    @patch("surveys.tasks.sync_client_integration_task.delay")
    def test_hidden_biobrain_is_queued_only_after_its_api_key_exists(self, delay):
        from .tasks import dispatch_due_integrations_task

        client = Client.objects.create(
            code="auto-biobrain", name="BioBrain", provider_code="biobrain", is_active=False
        )
        integration = ClientIntegration.objects.create(
            client=client, name="BioBrain automatic", provider_code="biobrain",
            base_url="https://partner-api.voqall.com/api/v1/surveys",
            credential_env_key="TEST_BIOBRAIN_API_KEY", scheduled_sync_enabled=True,
            sync_interval_seconds=60, last_sync_started_at=timezone.now() - timedelta(seconds=61),
        )
        with patch.dict(os.environ, {"TEST_BIOBRAIN_API_KEY": ""}):
            self.assertNotIn(integration.pk, dispatch_due_integrations_task()["queued"])
        with patch.dict(os.environ, {"TEST_BIOBRAIN_API_KEY": "bio-secret"}):
            self.assertIn(integration.pk, dispatch_due_integrations_task()["queued"])

    @patch("surveys.tasks.sync_client_integration_task.delay")
    def test_dispatcher_does_not_requeue_an_integration_with_an_active_lease(self, delay):
        from .tasks import dispatch_due_integrations_task

        ClientIntegration.objects.all().delete()
        client = Client.objects.create(
            code="leased-cint", name="Leased Cint", provider_code="cint"
        )
        integration = ClientIntegration.objects.create(
            client=client, name="Cint running", provider_code="cint",
            base_url="https://api.samplicio.us", supplier_code="50",
            last_test_status="success", last_sync_started_at=timezone.now() - timedelta(minutes=2),
        )
        self.assertTrue(SyncLease.acquire(f"integration-{integration.pk}-sync", seconds=300))

        result = dispatch_due_integrations_task()

        self.assertNotIn(integration.pk, result["queued"])
        delay.assert_not_called()

    @patch("surveys.tasks.sync_client_integration_task.delay")
    def test_dispatcher_does_not_poll_cint_when_opportunities_webhook_is_enabled(self, delay):
        from .tasks import dispatch_due_integrations_task

        ClientIntegration.objects.all().delete()
        client = Client.objects.create(
            code="webhook-cint", name="Webhook Cint", provider_code="cint"
        )
        integration = ClientIntegration.objects.create(
            client=client,
            name="Cint Feed Opportunities",
            provider_code="cint",
            base_url="https://api.samplicio.us",
            supplier_code="6528",
            last_test_status="success",
            last_sync_started_at=timezone.now() - timedelta(minutes=2),
            config={"opportunities_webhook_enabled": True},
        )

        result = dispatch_due_integrations_task()

        self.assertNotIn(integration.pk, result["queued"])
        delay.assert_not_called()

    @patch("surveys.tasks.sync_surveys")
    def test_successful_biobrain_inventory_publishes_hidden_client(self, sync_mock):
        from types import SimpleNamespace
        from .tasks import sync_client_integration_task

        client = Client.objects.create(
            code="publish-biobrain", name="BioBrain", provider_code="biobrain", is_active=False
        )
        integration = ClientIntegration.objects.create(
            client=client, name="BioBrain publish", provider_code="biobrain",
            base_url="https://partner-api.voqall.com/api/v1/surveys",
            credential_env_key="TEST_BIOBRAIN_PUBLISH_KEY", scheduled_sync_enabled=True,
        )
        sync_mock.return_value = SimpleNamespace(created=1, updated=0, unchanged=0, closed=0)
        with patch.dict(os.environ, {"TEST_BIOBRAIN_PUBLISH_KEY": "bio-secret"}):
            sync_client_integration_task(integration.pk)
        client.refresh_from_db()
        self.assertTrue(client.is_active)


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

    @override_settings(INNOVATEMR_API_TOKEN="secret-test-token")
    def test_transaction_lookup_uses_survey_and_rid_as_pid(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"apiStatus": "success", "result": [{"status": "Completed"}]}
        session = Mock()
        session.get.return_value = response
        result = InnovateMRClient(session=session).get_survey_transactions_by_pid(15978952, "Aa1Bb2Cc3D")
        self.assertEqual(result[0]["status"], "Completed")
        self.assertTrue(session.get.call_args.args[0].endswith("/supply/getSurveyTransactionsByCond/15978952/Aa1Bb2Cc3D"))


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
        self.assertNotIn("entry_link", response.data["results"][0])
        start_link = response.data["results"][0]["start_link"]
        parsed = urlsplit(start_link)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.path, reverse("survey-start"))
        self.assertEqual(set(query), {"entry"})
        self.assertNotIn(str(self.survey.source_id), start_link)
        self.assertNotIn(str(self.survey.local_id), start_link)
        self.assertNotIn(f"userId={self.user.pk}", start_link)

    def test_secure_start_link_allocates_pid_server_side_and_hides_rid(self):
        listing = self.api.get(reverse("survey-list"), {"search": "banking"})
        start_link = listing.data["results"][0]["start_link"]

        gate = self.client.get(start_link, REMOTE_ADDR="10.10.10.10")
        self.assertEqual(gate.status_code, 200)
        self.assertEqual(SurveyAttempt.objects.count(), 0)
        entry_token = parse_qs(urlsplit(start_link).query)["entry"][0]
        started = self.client.post(
            reverse("survey-start"),
            {"entry": entry_token},
            REMOTE_ADDR="10.10.10.10",
        )

        self.assertEqual(started.status_code, 302)
        continuation = urlsplit(started["Location"])
        self.assertEqual(set(parse_qs(continuation.query)), {"journey"})
        attempt = SurveyAttempt.objects.get(survey=self.survey, platform_user=self.user)
        journey = parse_qs(continuation.query)["journey"][0]
        self.assertNotIn(attempt.rid, started["Location"])
        self.assertNotIn(attempt.pid, started["Location"])

        form = self.client.get(started["Location"])
        self.assertContains(form, attempt.pid)
        self.assertNotContains(form, attempt.rid)
        self.assertContains(form, f'name="journey" value="{journey}"')
        self.assertNotContains(form, 'name="pid"')

        # Copying the opaque continuation into another browser session cannot
        # select or mutate the victim's attempt.
        attacker = self.client_class()
        rejected = attacker.get(started["Location"])
        self.assertEqual(rejected.status_code, 400)

        injected = self.client.post(reverse("survey-start"), {
            "journey": journey,
            "pid": attempt.pid,
            "question_1": "anything",
        })
        self.assertEqual(injected.status_code, 400)

    def test_each_copy_request_returns_a_new_server_signed_entry_link(self):
        listing = self.api.get(reverse("survey-list"), {"search": "banking"})
        original_link = listing.data["results"][0]["start_link"]
        original_token = parse_qs(urlsplit(original_link).query)["entry"][0]

        first = self.api.post(
            reverse("survey-entry-link"),
            {"entry": original_token},
            format="json",
        )
        first_token = parse_qs(urlsplit(first.data["start_link"]).query)["entry"][0]
        second = self.api.post(
            reverse("survey-entry-link"),
            {"entry": first_token},
            format="json",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(original_link, first.data["start_link"])
        self.assertNotEqual(first.data["start_link"], second.data["start_link"])
        self.assertEqual(self.client.get(first.data["start_link"]).status_code, 200)
        self.assertEqual(self.client.get(second.data["start_link"]).status_code, 200)
        self.assertEqual(SurveyAttempt.objects.count(), 0)

    def test_cached_frontend_entry_link_with_ignored_pid_remains_valid(self):
        listing = self.api.get(reverse("survey-list"), {"search": "banking"})
        token = parse_qs(
            urlsplit(listing.data["results"][0]["start_link"]).query
        )["entry"][0]

        response = self.client.get(reverse("survey-start"), {
            "entry": token,
            "pid": generate_platform_pid(),
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="entryGate"')
        self.assertEqual(SurveyAttempt.objects.count(), 0)

        malformed = self.client.get(reverse("survey-start"), {
            "entry": token,
            "pid": "not-valid!",
        })
        injected = self.client.get(reverse("survey-start"), {
            "entry": token,
            "pid": generate_platform_pid(),
            "unexpected": "value",
        })
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(injected.status_code, 400)

    def test_tampered_secure_start_link_is_rejected_without_creating_attempt(self):
        listing = self.api.get(reverse("survey-list"), {"search": "banking"})
        parsed = urlsplit(listing.data["results"][0]["start_link"])
        token = parse_qs(parsed.query)["entry"][0]
        position = len(token) // 2
        replacement = "A" if token[position] != "A" else "B"
        tampered = token[:position] + replacement + token[position + 1:]

        response = self.client.get(reverse("survey-start"), {"entry": tampered})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(SurveyAttempt.objects.count(), 0)

    @override_settings(ALLOW_LEGACY_UNSIGNED_ENTRY_LINKS=False)
    def test_unsigned_legacy_entry_parameters_are_rejected(self):
        response = self.client.get(reverse("survey-start"), {
            "surveyId": self.survey.source_id,
            "supplierCode": "1000",
            "userId": self.user.pk,
            "code": self.survey.local_id,
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(SurveyAttempt.objects.count(), 0)

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

    def test_buyer_and_survey_type_filters_are_server_side(self):
        self.survey.buyer_id = "3690"
        self.survey.group_type = "Consumer"
        self.survey.survey_type = "B2C"
        self.survey.save(update_fields=["buyer_id", "group_type", "survey_type"])
        Survey.objects.create(source_id=9880, buyer_id="4417", group_type="Business", survey_type="B2B")

        response = self.api.get(reverse("survey-list"), {"buyer_id": "3690", "survey_type": "B2C"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["buyer_id"], "3690")
        self.assertEqual(response.data["results"][0]["survey_type"], "B2C")

    def test_cpi_range_and_sort_are_applied_server_side(self):
        UserFunctionOverride.objects.create(
            user=self.user,
            function=AccessFunction.objects.get(code="projects.filter.cpi"),
            effect=UserFunctionOverride.Effect.ALLOW,
        )
        self.survey.cpi = "2.50"
        self.survey.save(update_fields=["cpi"])
        higher = Survey.objects.create(source_id=9878, name="Higher CPI", cpi="7.25")
        response = self.api.get(reverse("survey-list"), {
            "min_cpi": "3.00", "max_cpi": "8.00", "ordering": "-cpi",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["local_id"] for item in response.data["results"]], [higher.local_id])

    def test_cpi_filter_sort_and_export_use_role_adjusted_price(self):
        role = Role.objects.get(slug="team-lead")
        role.cpi_visibility_percent = Decimal("50.00")
        role.save(update_fields=["cpi_visibility_percent"])
        EmployeeProfile.objects.filter(user=self.user).update(role=role)
        self.user._state.fields_cache.pop("employee_profile", None)
        UserFunctionOverride.objects.create(
            user=self.user,
            function=AccessFunction.objects.get(code="projects.filter.cpi"),
            effect=UserFunctionOverride.Effect.ALLOW,
        )
        self.survey.cpi = Decimal("2.00")
        self.survey.save(update_fields=["cpi"])
        five_dollar_visible = Survey.objects.create(
            source_id=9888,
            name="Visible at five",
            cpi=Decimal("10.00"),
        )
        Survey.objects.create(
            source_id=9889,
            name="Visible above range",
            cpi=Decimal("20.00"),
        )

        params = {"min_cpi": "1.00", "max_cpi": "5.00", "ordering": "-cpi"}
        response = self.api.get(reverse("survey-list"), params)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["local_id"] for item in response.data["results"]],
            [five_dollar_visible.local_id, self.survey.local_id],
        )
        self.assertEqual(
            [Decimal(str(item["cpi"])) for item in response.data["results"]],
            [Decimal("5.00"), Decimal("1.00")],
        )

        export_rows = xlsx_rows(self.api.get(reverse("survey-export"), params))
        cpi_index = export_rows[0].index("CPI")
        survey_id_index = export_rows[0].index("Survey ID")
        exported = {
            str(row[survey_id_index]): Decimal(str(row[cpi_index]))
            for row in export_rows[1:]
        }
        self.assertEqual(exported, {
            str(five_dollar_visible.source_id): Decimal("5.00"),
            str(self.survey.source_id): Decimal("1.00"),
        })

    def test_project_completes_are_combined_across_all_users(self):
        second_user = get_user_model().objects.create_user(username="second-panelist")
        for index in range(7):
            SurveyAttempt.objects.create(
                rid=f"A{index:09d}",
                survey=self.survey,
                platform_user=self.user,
                user_id=str(self.user.pk),
                status=SurveyAttempt.Status.COMPLETED,
            )
        for index in range(5):
            SurveyAttempt.objects.create(
                rid=f"B{index:09d}",
                survey=self.survey,
                platform_user=second_user,
                user_id=str(second_user.pk),
                status=SurveyAttempt.Status.COMPLETED,
            )
        SurveyAttempt.objects.create(
            rid="C000000000",
            survey=self.survey,
            platform_user=second_user,
            user_id=str(second_user.pk),
            status=SurveyAttempt.Status.TERMINATED,
        )
        self.survey.completes = 999
        self.survey.save(update_fields=["completes"])

        caches["projects"].clear()
        with CaptureQueriesContext(connection) as captured:
            response = self.api.get(reverse("survey-list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"][0]["completes"], 12)
        self.assertEqual(response.data["results"][0]["progress_percent"], 24.0)
        list_sql = "\n".join(query["sql"] for query in captured.captured_queries)
        self.assertNotIn("SELECT COUNT(U0", list_sql)
        self.assertIn("GROUP BY", list_sql)
        self.assertIn("surveys_surveyattempt", list_sql)
        export_rows = xlsx_rows(self.api.get(reverse("survey-export")))
        self.assertEqual(
            Decimal(str(export_rows[1][export_rows[0].index("Completes")])),
            Decimal("12"),
        )

    def test_project_list_query_count_does_not_scale_with_page_rows(self):
        for index in range(12):
            Survey.objects.create(
                source_id=9900 + index,
                name=f"Query count project {index}",
                country="United States",
                country_code="US",
                status=Survey.Status.LIVE,
            )

        url = reverse("survey-list")
        caches["projects"].clear()
        self.addCleanup(caches["projects"].clear)
        warm_response = self.api.get(url, {"page_size": 20})
        self.assertEqual(warm_response.status_code, 200)

        def captured_response(page_size):
            with CaptureQueriesContext(connection) as captured:
                response = self.api.get(url, {"page_size": page_size})
            self.assertEqual(response.status_code, 200)
            return response, list(captured.captured_queries)

        single_row, single_row_queries = captured_response(1)
        multiple_rows, multiple_row_queries = captured_response(20)

        self.assertEqual(len(single_row.data["results"]), 1)
        self.assertGreater(len(multiple_rows.data["results"]), 1)
        self.assertEqual(len(single_row_queries), len(multiple_row_queries))

    def test_project_export_uses_filters_and_column_permissions(self):
        UserFunctionOverride.objects.create(
            user=self.user,
            function=AccessFunction.objects.get(code="projects.filter.cpi"),
            effect=UserFunctionOverride.Effect.ALLOW,
        )
        self.survey.cpi = "2.50"
        self.survey.save(update_fields=["cpi"])
        excluded = Survey.objects.create(source_id=9879, name="Excluded high CPI", cpi="8.00")
        response = self.api.get(reverse("survey-export"), {"max_cpi": "3.00", "ordering": "-cpi"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("projects-", response["Content-Disposition"])
        self.assertIn(".xlsx", response["Content-Disposition"])
        rows = xlsx_rows(response)
        self.assertIn("Project ID", rows[0])
        self.assertIn("CPI", rows[0])
        self.assertIn(str(self.survey.source_id), rows[1])
        self.assertNotIn(str(excluded.source_id), str(rows))

        UserFunctionOverride.objects.create(
            user=self.user,
            function=AccessFunction.objects.get(code="projects.column.cpi"),
            effect=UserFunctionOverride.Effect.DENY,
        )
        denied_response = self.api.get(reverse("survey-export"), {"max_cpi": "3.00"})
        denied_rows = xlsx_rows(denied_response)
        self.assertNotIn("CPI", denied_rows[0])

        UserFunctionOverride.objects.create(
            user=self.user,
            function=AccessFunction.objects.get(code="projects.column.client_name"),
            effect=UserFunctionOverride.Effect.DENY,
        )
        client_denied_rows = xlsx_rows(self.api.get(reverse("survey-export")))
        self.assertNotIn("Client", client_denied_rows[0])
        client_denied_list = self.api.get(reverse("survey-list"))
        self.assertNotIn("client_name", client_denied_list.data["results"][0])
        self.assertNotIn("display_company_name", client_denied_list.data["results"][0])
        self.assertNotIn("company_name", client_denied_list.data["results"][0])

    def test_cint_fr_export_matches_list_with_duplicate_country_input(self):
        cint = Client.objects.create(
            code="cint-fr-export",
            name="Cint Exchange",
            provider_code="cint",
        )
        self.survey.client = cint
        self.survey.company_name = cint.name
        self.survey.country = "France"
        self.survey.country_code = "FR"
        self.survey.status = Survey.Status.LIVE
        self.survey.save(update_fields=[
            "client", "company_name", "country", "country_code", "status",
        ])
        Survey.objects.create(
            source_id=9891,
            client=cint,
            company_name=cint.name,
            country="United States",
            country_code="US",
            status=Survey.Status.LIVE,
        )
        params = {
            "country": "FR,FR",
            "status": "live",
            "client_name": cint.name,
            "ordering": "-source_modified_at",
        }

        listing = self.api.get(reverse("survey-list"), params)
        export_rows = xlsx_rows(self.api.get(reverse("survey-export"), params))
        project_id_index = export_rows[0].index("Project ID")

        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.data["count"], 1)
        self.assertEqual(
            {row[project_id_index] for row in export_rows[1:]},
            {listing.data["results"][0]["local_id"]},
        )

    def test_detail_actions_return_cached_data(self):
        quota = self.api.get(reverse("survey-quotas", kwargs={"local_id": self.survey.local_id}))
        targeting = self.api.get(reverse("survey-targeting", kwargs={"local_id": self.survey.local_id}))
        combined = self.api.get(reverse("survey-details", kwargs={"local_id": self.survey.local_id}))
        self.assertEqual(quota.status_code, 200)
        self.assertEqual(quota.data[0]["quota_id"], 1)
        self.assertEqual(targeting.data[0]["key"], "GENDER")
        self.assertEqual(combined.status_code, 200)
        self.assertEqual(combined.data["quotas"], quota.data)
        self.assertEqual(combined.data["targeting"], targeting.data)

        detail = self.api.get(reverse("survey-detail", kwargs={"local_id": self.survey.local_id}))
        self.assertNotIn("entry_link", detail.data)
        self.assertNotIn("test_entry_link", detail.data)

    def test_combined_details_preserves_the_available_tab_on_partial_failure(self):
        self.survey.targeting_questions.all().delete()
        self.survey.targeting_synced_at = None
        self.survey.save(update_fields=["targeting_synced_at"])

        def refresh(_survey, detail_type):
            if detail_type == "targeting":
                raise ProviderError("Targeting is temporarily unavailable.")

        with patch(
            "surveys.views.SurveyViewSet._refresh_if_stale",
            side_effect=refresh,
        ):
            response = self.api.get(
                reverse("survey-details", kwargs={"local_id": self.survey.local_id})
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["targeting"], [])
        self.assertEqual(
            response.data["errors"]["targeting"],
            "Targeting is temporarily unavailable.",
        )
        self.assertNotIn("quotas", response.data["errors"])
        self.assertEqual(response.data["quotas"][0]["quota_id"], 1)

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

    def test_projects_render_and_employee_dashboard_is_restricted(self):
        projects = self.client.get(reverse("projects"))
        self.assertContains(projects, "Survey inventory")
        self.assertContains(projects, "Pre-screening questions")
        self.assertContains(projects, 'id="fromDateTime"')
        self.assertContains(projects, 'id="toDateTime"')
        self.assertNotContains(projects, 'id="fromTime"')
        self.assertContains(projects, 'id="exportProjects"')
        self.assertContains(projects, 'placeholder="Search country')
        self.assertContains(projects, 'placeholder="Search client')
        self.assertContains(projects, 'id="companyLabel">Client')
        self.assertNotContains(projects, 'id="cpiFilterTrigger"')
        self.assertNotContains(projects, "Quest")
        dashboard = self.client.get(reverse("dashboard"))
        self.assertEqual(dashboard.status_code, 403)

        profile = self.user.employee_profile
        profile.role = Role.objects.get(slug="admin")
        profile.save(update_fields=["role"])
        admin_projects = self.client.get(reverse("projects"))
        self.assertContains(admin_projects, 'id="cpiFilterTrigger"')
        self.assertContains(admin_projects, "CPI: highest to lowest")
        self.assertContains(admin_projects, 'id="cpiMinRange"')
        self.assertContains(admin_projects, 'id="cpiMaxRange"')


class PrescreenerAnswerValidationTests(TestCase):
    def setUp(self):
        self.survey = Survey.objects.create(
            source_key="prescreener-validation",
            name="Prescreener validation",
            status=Survey.Status.LIVE,
            company_name="Validation provider",
            country_code="US",
            language_code="EN",
        )
        self.gender = TargetingQuestion.objects.create(
            survey=self.survey,
            question_id=1,
            key="GENDER",
            text="What is your gender?",
            question_type="Single Punch",
            options=[
                {"OptionId": "1", "OptionText": "Male"},
                {"OptionId": "2", "OptionText": "Female"},
            ],
        )
        self.age = TargetingQuestion.objects.create(
            survey=self.survey,
            question_id=2,
            key="AGE",
            text="What is your age?",
            question_type="Numeric Open Ended",
            options=[{"OptionId": "adult", "ageStart": 18, "ageEnd": 64}],
        )
        self.hobbies = TargetingQuestion.objects.create(
            survey=self.survey,
            question_id=3,
            key="HOBBIES",
            text="Which hobbies do you enjoy?",
            question_type="Multi Punch",
            options=[
                {"OptionId": "a", "OptionText": "Art"},
                {"OptionId": "b", "OptionText": "Books"},
            ],
        )
        self.dob = TargetingQuestion.objects.create(
            survey=self.survey,
            question_id=4,
            key="DOB",
            text="What is your date of birth?",
            question_type="Date",
            options=[],
        )
        self.comment = TargetingQuestion.objects.create(
            survey=self.survey,
            question_id=5,
            key="COMMENT",
            text="Tell us something about yourself",
            question_type="Open Ended",
            options=[],
        )
        self.factory = RequestFactory()

    @staticmethod
    def field(question):
        return f"question_{question.pk}"

    def valid_payload(self):
        return {
            self.field(self.gender): "1",
            self.field(self.age): "30",
            self.field(self.hobbies): ["a"],
            self.field(self.dob): "01-01-1990",
            self.field(self.comment): "A valid answer",
        }

    def collect(self, payload):
        request = self.factory.post("/survey/start", payload)
        return _collect_prescreener_answers(request, self.survey)

    def test_scalar_controls_reject_repeated_post_keys(self):
        repeated_values = (
            (self.gender, ["1", "2"]),
            (self.age, ["30", "31"]),
            (self.dob, ["01-01-1990", "02-02-1990"]),
            (self.comment, ["first", "second"]),
        )
        for question, values in repeated_values:
            with self.subTest(question=question.key):
                payload = self.valid_payload()
                payload[self.field(question)] = values

                answers, errors = self.collect(payload)

                self.assertNotIn(str(question.pk), answers)
                self.assertTrue(any("exactly one answer" in error for error in errors))

    def test_checkbox_values_are_deduplicated_in_submission_order(self):
        payload = self.valid_payload()
        payload[self.field(self.hobbies)] = ["b", "a", "b"]

        answers, errors = self.collect(payload)

        self.assertEqual(errors, [])
        self.assertEqual(answers[str(self.hobbies.pk)]["values"], ["b", "a"])
        self.assertEqual(
            answers[str(self.hobbies.pk)]["upstream_values"], ["b", "a"]
        )

    def test_numeric_minimum_and_maximum_are_enforced_inclusively(self):
        for invalid_age in (17, 65):
            with self.subTest(invalid_age=invalid_age):
                payload = self.valid_payload()
                payload[self.field(self.age)] = str(invalid_age)

                answers, errors = self.collect(payload)

                self.assertNotIn(str(self.age.pk), answers)
                self.assertTrue(any("between 18 and 64" in error for error in errors))

        for boundary_age in (18, 64):
            with self.subTest(boundary_age=boundary_age):
                payload = self.valid_payload()
                payload[self.field(self.age)] = str(boundary_age)

                answers, errors = self.collect(payload)

                self.assertEqual(errors, [])
                self.assertEqual(
                    answers[str(self.age.pk)]["upstream_values"], [str(boundary_age)]
                )

    def test_open_ended_age_bands_are_accepted_through_99_for_every_provider(self):
        shapes = (
            (
                "innovatemr",
                [{"OptionId": "band", "OptionText": "65+", "ageStart": 65, "ageEnd": 65}],
                {"targeting_choices": ["band"]},
                65,
            ),
            (
                "toluna",
                [{"OptionId": "band", "OptionText": "65 and older"}],
                {
                    "targeting_choices": ["band"],
                    "targeting_age_ranges": [{"min": 65, "max": 65}],
                },
                65,
            ),
            (
                "biobrain",
                [{"OptionId": "band", "OptionText": "25 or older"}],
                {"targeting_choices": ["band"]},
                25,
            ),
            (
                "cint",
                [{"OptionId": "band", "OptionText": "65 and above"}],
                {"targeting_choices": ["band"]},
                65,
            ),
            (
                "rfg",
                [{"OptionId": "band", "OptionText": "25 years and over"}],
                {"targeting_choices": ["band"]},
                25,
            ),
            (
                "purespectrum",
                [],
                {"targeting_age_ranges": [{"min": 13, "max": 120}]},
                13,
            ),
            (
                "custom",
                [],
                {"targeting_age_ranges": [{"min": 25, "max": None, "label": "25+"}]},
                25,
            ),
        )
        for index, (provider_code, options, raw_data, minimum) in enumerate(shapes):
            with self.subTest(provider=provider_code):
                client = Client.objects.create(
                    code=f"age-{index}",
                    name=f"Age {provider_code}",
                    provider_code=provider_code,
                )
                integration = ClientIntegration.objects.create(
                    client=client,
                    name=f"Age {provider_code}",
                    provider_code=provider_code,
                    base_url=f"https://{provider_code}.example.test",
                )
                self.survey.integration = integration
                self.survey.save(update_fields=["integration", "updated_at"])
                self.age.options = options
                self.age.raw_data = raw_data
                self.age.save(update_fields=["options", "raw_data", "updated_at"])

                prepared = next(
                    item for item in _prescreener_questions(self.survey)
                    if item["model"].pk == self.age.pk
                )
                self.assertEqual((prepared["min_value"], prepared["max_value"]), (minimum, 99))

                for accepted_age in (minimum + 1, 99):
                    payload = self.valid_payload()
                    payload[self.field(self.age)] = str(accepted_age)
                    answers, errors = self.collect(payload)
                    self.assertEqual(errors, [])
                    self.assertEqual(
                        answers[str(self.age.pk)]["upstream_values"],
                        [str(accepted_age)],
                    )

                payload = self.valid_payload()
                payload[self.field(self.age)] = "100"
                answers, errors = self.collect(payload)
                self.assertNotIn(str(self.age.pk), answers)
                self.assertTrue(any("between" in error for error in errors))

    def test_explicit_closed_age_range_is_not_widened(self):
        self.age.options = [{
            "OptionId": "25-29",
            "OptionText": "25-29",
            "ageStart": 25,
            "ageEnd": 29,
        }]
        self.age.raw_data = {"targeting_choices": ["25-29"]}
        self.age.save(update_fields=["options", "raw_data", "updated_at"])

        prepared = next(
            item for item in _prescreener_questions(self.survey)
            if item["model"].pk == self.age.pk
        )
        self.assertEqual((prepared["min_value"], prepared["max_value"]), (25, 29))
        payload = self.valid_payload()
        payload[self.field(self.age)] = "30"
        answers, errors = self.collect(payload)
        self.assertNotIn(str(self.age.pk), answers)
        self.assertTrue(any("between 25 and 29" in error for error in errors))

    def test_text_and_checkbox_submission_limits_are_enforced(self):
        payload = self.valid_payload()
        payload[self.field(self.comment)] = "x" * (PRESCREENER_MAX_TEXT_LENGTH + 1)
        answers, errors = self.collect(payload)
        self.assertNotIn(str(self.comment.pk), answers)
        self.assertTrue(any("Answer is too long" in error for error in errors))

        payload = self.valid_payload()
        payload[self.field(self.hobbies)] = [
            "a" for _ in range(PRESCREENER_MAX_LIST_VALUES + 1)
        ]
        answers, errors = self.collect(payload)
        self.assertNotIn(str(self.hobbies.pk), answers)
        self.assertTrue(any("Too many answers" in error for error in errors))


@override_settings(
    ALLOW_LEGACY_UNSIGNED_ENTRY_LINKS=True,
    INNOVATEMR_CALLBACK_HASH_REQUIRED=False,
)
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
            "supplierCode": "1000",
            "userId": "294",
            "code": self.survey.local_id,
        }, REMOTE_ADDR="10.10.10.10", HTTP_USER_AGENT="Mozilla/5.0 (Windows NT 10.0) Chrome/126.0.0.0")
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
        self.assertEqual(params["supCode"], ["1150"])

        callback = self.client.get(
            reverse("survey-status"), {"status": "1", "rid": rid}, REMOTE_ADDR="20.20.20.20",
            HTTP_USER_AGENT="Mozilla/5.0 (Linux; Android 14; Mobile) Chrome/126.0.0.0",
        )
        self.assertEqual(callback.status_code, 302)
        attempt = SurveyAttempt.objects.get(rid=rid)
        self.assertEqual(
            callback["Location"],
            f"{reverse('survey-status')}?status=1&pid={attempt.pid}",
        )
        clean_result = self.client.get(callback["Location"])
        self.assertEqual(clean_result.status_code, 200)
        self.assertContains(clean_result, "Thank you for participating!")
        self.assertContains(clean_result, attempt.pid)
        self.assertEqual(attempt.status, SurveyAttempt.Status.COMPLETED)
        self.assertEqual(attempt.platform_user, self.platform_user)
        self.assertEqual(attempt.user_id, "294")
        self.assertEqual(attempt.supplier_code, "1150")
        self.assertEqual(attempt.initiation_ip, "10.10.10.10")
        self.assertEqual(attempt.callback_ip, "20.20.20.20")
        self.assertEqual(attempt.entry_browser, "Chrome 126.0.0.0")
        self.assertEqual(attempt.entry_device, "Desktop")
        self.assertEqual(attempt.exit_device, "Mobile")
        self.assertEqual(attempt.exit_os, "Android 14")
        self.assertIsNotNone(attempt.loi_seconds)

    @override_settings(ENFORCE_PROJECT_UNIQUE_ENTRY_IP=True)
    def test_duplicate_entry_ip_is_blocked_only_inside_the_same_project(self):
        first = self.client.get(reverse("survey-start"), {
            "surveyId": self.survey.source_id,
            "supplierCode": "1000",
            "userId": str(self.platform_user.pk),
            "code": self.survey.local_id,
        }, REMOTE_ADDR="31.13.71.44")
        self.assertEqual(first.status_code, 302)
        first_rid = parse_qs(urlsplit(first["Location"]).query)["rid"][0]
        first_attempt = SurveyAttempt.objects.get(rid=first_rid)
        first_claim = SurveyProjectEntryIPClaim.objects.get(
            survey=self.survey,
            ip_address="31.13.71.44",
        )
        self.assertEqual(first_claim.first_attempt, first_attempt)

        other = Survey.objects.create(
            source_id=32655972,
            name="Another client survey",
            status=Survey.Status.LIVE,
            company_name="Another client",
            country_code="US",
            language_code="EN",
            entry_link="https://edgeapi.innovatemr.net/startSurvey?survNum=other&supCode=1150&PID=[%%pid%%]",
            source_modified_at=timezone.now(),
            targeting_synced_at=timezone.now(),
        )
        second = self.client.get(reverse("survey-start"), {
            "surveyId": other.source_id,
            "supplierCode": "1000",
            "userId": str(self.platform_user.pk),
            "code": other.local_id,
        }, REMOTE_ADDR="31.13.71.44")

        self.assertEqual(second.status_code, 302)
        second_query = parse_qs(urlsplit(second["Location"]).query)
        self.assertIn("rid", second_query)
        self.assertNotIn("status", second_query)
        allowed = SurveyAttempt.objects.get(rid=second_query["rid"][0])
        self.assertEqual(allowed.survey, other)
        self.assertEqual(allowed.status, SurveyAttempt.Status.INITIATED)
        self.assertTrue(
            SurveyProjectEntryIPClaim.objects.filter(
                survey=other,
                ip_address="31.13.71.44",
                first_attempt=allowed,
            ).exists()
        )
        self.assertEqual(
            SurveyProjectEntryIPClaim.objects.filter(ip_address="31.13.71.44").count(),
            2,
        )

        duplicate_response = self.client.get(reverse("survey-start"), {
            "surveyId": self.survey.source_id,
            "supplierCode": "1000",
            "userId": str(self.platform_user.pk),
            "code": self.survey.local_id,
        }, REMOTE_ADDR="31.13.71.44")
        self.assertEqual(duplicate_response.status_code, 302)
        duplicate_query = parse_qs(urlsplit(duplicate_response["Location"]).query)
        self.assertEqual(duplicate_query["status"], ["4"])
        self.assertNotIn("rid", duplicate_query)
        duplicate = SurveyAttempt.objects.get(pid=duplicate_query["pid"][0])
        self.assertNotEqual(duplicate.rid, first_rid)
        self.assertEqual(duplicate.status, SurveyAttempt.Status.QUALITY_TERMINATED)
        self.assertEqual(duplicate.status_source, "local_duplicate_ip_guard")
        self.assertEqual(
            duplicate.upstream_transaction_data["local_ip_guard"]["reason"],
            "Duplicate IP address",
        )
        result = self.client.get(duplicate_response["Location"])
        self.assertEqual(result.status_code, 200)
        self.assertContains(result, "Duplicate entry blocked")
        self.assertContains(result, "Duplicate IP blocked")
        self.assertNotContains(result, "Invalid survey callback")
        self.assertEqual(
            SurveyProjectEntryIPClaim.objects.filter(
                survey=self.survey,
                ip_address="31.13.71.44",
            ).count(),
            1,
        )

    @override_settings(ENFORCE_SURVEY_TARGET_COUNTRY=True)
    def test_wrong_target_country_shows_recorded_reason_instead_of_invalid_callback(self):
        location = {
            "ip": "49.37.10.20",
            "country_code": "IN",
            "country": "India",
            "postal_code": "110001",
            "source": "test",
        }
        with patch("surveys.views.resolve_entry_geolocation", return_value=location):
            start = self.client.get(reverse("survey-start"), {
                "surveyId": self.survey.source_id,
                "supplierCode": "1000",
                "userId": str(self.platform_user.pk),
                "code": self.survey.local_id,
            }, REMOTE_ADDR=location["ip"])
            self.assertEqual(start.status_code, 302)
            rid = parse_qs(urlsplit(start["Location"]).query)["rid"][0]
            blocked = self.client.get(reverse("survey-start"), {"rid": rid})

        self.assertEqual(blocked.status_code, 302)
        blocked_query = parse_qs(urlsplit(blocked["Location"]).query)
        self.assertEqual(blocked_query["status"], ["4"])
        self.assertNotIn("rid", blocked_query)
        attempt = SurveyAttempt.objects.get(rid=rid)
        self.assertEqual(blocked_query["pid"], [attempt.pid])
        self.assertEqual(attempt.status_source, "local_country_guard")

        result = self.client.get(blocked["Location"])
        self.assertEqual(result.status_code, 200)
        self.assertContains(result, "Location not eligible")
        self.assertContains(result, "Wrong target country")
        self.assertContains(result, "detected country (IN)")
        self.assertContains(result, "target country (US)")
        self.assertNotContains(result, "Invalid survey callback")

    def test_innovate_profile_mapping_replaces_stale_values_and_protects_routing_keys(self):
        outbound = build_outbound_url(
            "https://edgeapi.innovatemr.net/startSurvey?survNum=test&supCode=1150&PID=old&trackId=old&GENDER=1",
            "Aa1Bb2Cc3D",
            {
                "gender": {"question_key": "GENDER", "upstream_values": ["2"]},
                "multi": {"question_key": "HOBBIES", "upstream_values": ["4", "4", "7"]},
                "reserved": {"question_key": "PID", "upstream_values": ["unsafe"]},
            },
        )

        params = parse_qs(urlsplit(outbound).query)
        self.assertEqual(params["PID"], ["Aa1Bb2Cc3D"])
        self.assertEqual(params["trackId"], ["Aa1Bb2Cc3D"])
        self.assertEqual(params["GENDER"], ["2"])
        self.assertEqual(params["HOBBIES"], ["4", "7"])

    def test_innovate_open_ended_age_and_zip_are_sent_as_actual_values(self):
        age = TargetingQuestion.objects.create(
            survey=self.survey,
            question_id=1,
            key="AGE",
            text="What is your age?",
            question_type="Numeric Open Ended",
            category="Demographic",
            options=[{"OptionId": 2, "ageStart": 18, "ageEnd": 34}],
        )
        zipcode = TargetingQuestion.objects.create(
            survey=self.survey,
            question_id=11,
            key="ZIPCODES",
            text="What is your zipcode?",
            question_type="Numeric Open Ended",
            category="Demographic",
            options=[
                {"OptionId": 77, "OptionText": "90012"},
                {"OptionId": 78, "OptionText": "02108"},
            ],
        )
        start = self.client.get(reverse("survey-start"), {
            "surveyId": self.survey.source_id,
            "supplierCode": "1000",
            "userId": self.platform_user.pk,
            "code": self.survey.local_id,
        })
        rid = parse_qs(urlsplit(start["Location"]).query)["rid"][0]
        form = self.client.get(reverse("survey-start"), {"rid": rid})
        self.assertContains(form, "What is your zipcode?")
        self.assertContains(form, "Required ZIP/postal codes: 90012, 02108")
        self.assertContains(form, "Enter your ZIP / postal code")
        self.assertContains(form, 'autocomplete="postal-code"')
        submit = self.client.post(reverse("survey-start"), {
            "rid": rid,
            f"question_{self.question.pk}": "1",
            f"question_{age.pk}": "24",
            f"question_{zipcode.pk}": "90012",
        })

        self.assertEqual(
            submit.status_code,
            302,
            msg=str(submit.context and submit.context.get("errors")),
        )
        params = parse_qs(urlsplit(submit["Location"]).query)
        self.assertEqual(params["AGE"], ["24"])
        self.assertEqual(params["ZIPCODES"], ["90012"])

    def test_innovate_zip_targeting_rejects_values_outside_provider_options(self):
        zipcode = TargetingQuestion.objects.create(
            survey=self.survey,
            question_id=11,
            key="ZIPCODES",
            text="What is your zipcode?",
            question_type="Numeric Open Ended",
            category="Geographic",
            options=[
                {"OptionId": 1, "OptionText": "A1A 1A1"},
                {"OptionId": 2, "OptionText": "B2B 2B2"},
            ],
        )
        start = self.client.get(reverse("survey-start"), {
            "surveyId": self.survey.source_id,
            "supplierCode": "1000",
            "userId": self.platform_user.pk,
            "code": self.survey.local_id,
        })
        rid = parse_qs(urlsplit(start["Location"]).query)["rid"][0]

        rejected = self.client.post(reverse("survey-start"), {
            "rid": rid,
            f"question_{self.question.pk}": "1",
            f"question_{zipcode.pk}": "C3C 3C3",
        })
        self.assertEqual(rejected.status_code, 200)
        self.assertContains(rejected, "Enter a ZIP/postal code accepted by this survey")
        self.assertEqual(
            SurveyAttempt.objects.get(rid=rid).status,
            SurveyAttempt.Status.INITIATED,
        )

        accepted = self.client.post(reverse("survey-start"), {
            "rid": rid,
            f"question_{self.question.pk}": "1",
            f"question_{zipcode.pk}": "a1a1a1",
        })
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(
            parse_qs(urlsplit(accepted["Location"]).query)["ZIPCODES"],
            ["A1A 1A1"],
        )

    def test_copied_platform_pid_is_preserved_and_separate_from_rid_and_uid(self):
        copied_pid = "A1bcD2eF3"
        start = self.client.get(reverse("survey-start"), {
            "surveyId": self.survey.source_id,
            "supplierCode": "1000",
            "userId": self.platform_user.pk,
            "code": self.survey.local_id,
            "pid": copied_pid,
        })

        self.assertEqual(start.status_code, 302)
        rid = parse_qs(urlsplit(start["Location"]).query)["rid"][0]
        attempt = SurveyAttempt.objects.get(rid=rid)
        self.assertEqual(attempt.pid, copied_pid)
        self.assertEqual(len(attempt.rid), 10)
        self.assertEqual(len(attempt.prescreener_uid), 19)
        self.assertNotEqual(attempt.pid, attempt.rid)
        self.assertNotEqual(attempt.pid, attempt.prescreener_uid)
        self.assertNotEqual(attempt.rid, attempt.prescreener_uid)

    def test_new_platform_pid_is_twelve_or_thirteen_mixed_characters(self):
        generated = [generate_platform_pid() for _ in range(50)]

        self.assertTrue(all(len(pid) in {12, 13} for pid in generated))
        self.assertTrue(all(pid.isalnum() for pid in generated))
        self.assertTrue(all(any(char.isupper() for char in pid) for pid in generated))
        self.assertTrue(all(any(char.islower() for char in pid) for pid in generated))
        self.assertTrue(all(any(char.isdigit() for char in pid) for pid in generated))
        self.assertTrue(all(is_valid_platform_pid(pid) for pid in generated))
        self.assertTrue(is_valid_platform_pid("A1bcD2eF3"))
        self.assertFalse(is_valid_platform_pid("Aa1Bb2Cc3D"))

    def test_invalid_platform_pid_never_creates_an_attempt(self):
        response = self.client.get(reverse("survey-start"), {
            "surveyId": self.survey.source_id,
            "supplierCode": "1000",
            "userId": self.platform_user.pk,
            "code": self.survey.local_id,
            "pid": "bad-pid-value",
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(SurveyAttempt.objects.count(), 0)

    def test_repeated_submission_keeps_the_first_redirect_immutable(self):
        start = self.client.get(reverse("survey-start"), {
            "surveyId": self.survey.source_id,
            "supplierCode": "1000",
            "userId": self.platform_user.pk,
            "code": self.survey.local_id,
        })
        rid = parse_qs(urlsplit(start["Location"]).query)["rid"][0]
        first = self.client.post(reverse("survey-start"), {
            "rid": rid,
            f"question_{self.question.pk}": "1",
        })
        self.assertEqual(first.status_code, 302)
        first_outbound = first["Location"]

        repeated = self.client.post(reverse("survey-start"), {
            "rid": rid,
            f"question_{self.question.pk}": "2",
        })

        self.assertRedirects(
            repeated,
            f"{reverse('survey-start')}?rid={rid}",
            fetch_redirect_response=False,
        )
        attempt = SurveyAttempt.objects.get(rid=rid)
        self.assertEqual(attempt.status, SurveyAttempt.Status.REDIRECTED)
        self.assertEqual(attempt.outbound_url, first_outbound)
        self.assertEqual(attempt.answers[str(self.question.pk)]["values"], ["1"])

    def test_status_requires_known_rid(self):
        response = self.client.get(reverse("survey-status"), {"status": "3", "rid": "Aa1Bb2Cc3D"})
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "could not be attached", status_code=404)

    def test_loi_includes_prescreener_time(self):
        now = timezone.now()
        attempt = SurveyAttempt.objects.create(
            rid="Aa1Bb2Cc3D",
            survey=self.survey,
            platform_user=self.platform_user,
            user_id=str(self.platform_user.pk),
            status=SurveyAttempt.Status.REDIRECTED,
            initiated_at=now - timedelta(minutes=65),
            submitted_at=now - timedelta(minutes=5),
            redirected_at=now - timedelta(minutes=5),
        )

        response = self.client.get(reverse("survey-status"), {"status": "1", "rid": attempt.rid})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"{reverse('survey-status')}?status=1&pid={attempt.pid}",
        )
        attempt.refresh_from_db()
        self.assertGreaterEqual(attempt.loi_seconds, 3900)
        self.assertLess(attempt.loi_seconds, 3910)

    @override_settings(TRUST_X_FORWARDED_FOR=True)
    def test_trusted_proxy_records_public_entry_and_exit_ips(self):
        start = self.client.get(reverse("survey-start"), {
            "surveyId": self.survey.source_id,
            "supplierCode": "1000",
            "userId": self.platform_user.pk,
            "code": self.survey.local_id,
        }, REMOTE_ADDR="127.0.0.1", HTTP_X_REAL_IP="8.8.8.8")
        rid = parse_qs(urlsplit(start["Location"]).query)["rid"][0]
        callback = self.client.get(
            reverse("survey-status"), {"status": "2", "rid": rid}, REMOTE_ADDR="127.0.0.1",
            HTTP_X_REAL_IP="1.1.1.1",
        )
        self.assertEqual(callback.status_code, 302)
        attempt = SurveyAttempt.objects.get(rid=rid)
        self.assertEqual(attempt.initiation_ip, "8.8.8.8")
        self.assertEqual(attempt.callback_ip, "1.1.1.1")
        self.assertEqual(attempt.status_source, "browser_callback")

    def test_provider_uid_callback_resolves_attempt_and_exposes_only_platform_pid(self):
        attempt = SurveyAttempt.objects.create(
            rid="Rt7Yu8Io9P",
            prescreener_uid="Ab1c-De2f-Gh3i-Jk4l",
            survey=self.survey,
            platform_user=self.platform_user,
            user_id=str(self.platform_user.pk),
            status=SurveyAttempt.Status.REDIRECTED,
        )

        callback = self.client.get(reverse("survey-status"), {
            "status": "3",
            "rid": attempt.prescreener_uid,
            "hash": "provider-transport-secret",
            "reason": "Quota capacity reached",
        })

        self.assertEqual(callback.status_code, 302)
        self.assertEqual(
            callback["Location"],
            f"{reverse('survey-status')}?status=3&pid={attempt.pid}",
        )
        self.assertNotIn("hash", callback["Location"])
        self.assertNotIn(attempt.rid, callback["Location"])
        self.assertNotIn(attempt.prescreener_uid, callback["Location"])

        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.OVER_QUOTA)
        self.assertEqual(attempt.callback_count, 1)
        self.assertEqual(
            attempt.upstream_transaction_data["browser_return"]["hash"],
            "[redacted]",
        )

        clean_result = self.client.get(callback["Location"])
        self.assertEqual(clean_result.status_code, 200)
        self.assertContains(clean_result, attempt.pid)
        attempt.refresh_from_db()
        self.assertEqual(attempt.callback_count, 1)

    def test_cint_callback_hash_is_redacted_before_clean_pid_result(self):
        cint_client = Client.objects.create(
            code="cint-universal-callback",
            name="Cint Exchange",
            provider_code="cint",
        )
        cint_integration = ClientIntegration.objects.create(
            client=cint_client,
            name="Cint callback",
            provider_code="cint",
            supplier_code="6528",
        )
        self.survey.client = cint_client
        self.survey.integration = cint_integration
        self.survey.save(update_fields=["client", "integration", "updated_at"])
        attempt = SurveyAttempt.objects.create(
            rid="Ci7Nt8Ri9D",
            survey=self.survey,
            platform_user=self.platform_user,
            user_id=str(self.platform_user.pk),
            status=SurveyAttempt.Status.REDIRECTED,
        )

        response = self.client.get(reverse("survey-status"), {
            "status": "4",
            "rid": attempt.rid,
            "hash": "signed-provider-value",
            "surveyId": self.survey.source_id,
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"{reverse('survey-status')}?status=4&pid={attempt.pid}",
        )
        attempt.refresh_from_db()
        self.assertEqual(
            attempt.upstream_transaction_data["cint_browser_return"]["hash"],
            "[redacted]",
        )

    def test_direct_localhost_is_not_saved_as_respondent_network_ip(self):
        start = self.client.get(reverse("survey-start"), {
            "surveyId": self.survey.source_id,
            "supplierCode": "1000",
            "userId": self.platform_user.pk,
            "code": self.survey.local_id,
        }, REMOTE_ADDR="127.0.0.1")
        rid = parse_qs(urlsplit(start["Location"]).query)["rid"][0]
        self.assertIsNone(SurveyAttempt.objects.get(rid=rid).initiation_ip)

    @override_settings(TRUST_X_FORWARDED_FOR=True)
    def test_rid_page_backfills_missing_entry_client_audit(self):
        start = self.client.get(reverse("survey-start"), {
            "surveyId": self.survey.source_id,
            "supplierCode": "1000",
            "userId": self.platform_user.pk,
            "code": self.survey.local_id,
        })
        rid = parse_qs(urlsplit(start["Location"]).query)["rid"][0]
        SurveyAttempt.objects.filter(rid=rid).update(
            initiation_ip=None,
            entry_user_agent="",
            entry_browser="",
            entry_device="",
            entry_os="",
            entry_referrer="",
            entry_accept_language="",
            entry_client_data={},
        )

        response = self.client.get(
            reverse("survey-start"),
            {"rid": rid},
            REMOTE_ADDR="127.0.0.1",
            HTTP_X_REAL_IP="8.8.8.8",
            HTTP_USER_AGENT="Mozilla/5.0 (Windows NT 10.0) Chrome/126.0.0.0",
            HTTP_ACCEPT_LANGUAGE="en-IN,en;q=0.9",
        )

        self.assertEqual(response.status_code, 200)
        attempt = SurveyAttempt.objects.get(rid=rid)
        self.assertEqual(attempt.initiation_ip, "8.8.8.8")
        self.assertEqual(attempt.entry_browser, "Chrome 126.0.0.0")
        self.assertEqual(attempt.entry_device, "Desktop")
        self.assertEqual(attempt.entry_os, "Windows 10.0")
        self.assertEqual(attempt.entry_accept_language, "en-IN,en;q=0.9")
        self.assertTrue(attempt.entry_user_agent.startswith("Mozilla/5.0"))
        self.assertEqual(attempt.entry_client_data["browser"], "Chrome 126.0.0.0")

    def test_invalid_start_values_never_create_attempt_or_show_questions(self):
        valid = {
            "surveyId": str(self.survey.source_id),
            "supplierCode": "1000",
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
            "supplierCode": "1000",
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


class StudiesTrackingTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_superuser(
            username="owner", email="owner@example.test", password="test-password"
        )
        self.kanik = get_user_model().objects.create_user(
            username="kanik", first_name="Kanik", last_name="Sharma", email="kanik@example.test"
        )
        self.other = get_user_model().objects.create_user(username="other", first_name="Other")
        self.survey = Survey.objects.create(
            client=Client.objects.create(code="tracking-client", name="Tracking Client"),
            source_id=555123,
            name="Consumer finance",
            company_name="InnovateMR",
            country="United States",
            country_code="US",
            language_code="EN",
            cpi="2.50",
            buyer_id="3690",
            survey_type="B2C",
            loi=10,
        )
        common = {
            "survey": self.survey,
            "supplier_code": "1150",
            "initiation_ip": "10.0.0.1",
            "callback_ip": "20.0.0.1",
            "entry_browser": "Chrome 126",
            "entry_device": "Desktop",
            "entry_os": "Windows 10",
        }
        self.complete = SurveyAttempt.objects.create(
            rid="Aa1Bb2Cc3D", prescreener_uid="Ab12-Cd34-Ef56-Gh78",
            platform_user=self.kanik, user_id=str(self.kanik.pk),
            status=SurveyAttempt.Status.COMPLETED, loi_seconds=82, callback_at=timezone.now(),
            source_cpi_snapshot="2.50", payable_cpi_snapshot="2.50", cpi_currency_snapshot="USD", **common,
        )
        SurveyAttempt.objects.create(
            rid="Ee4Ff5Gg6H", platform_user=self.other, user_id=str(self.other.pk),
            status=SurveyAttempt.Status.TERMINATED, **common,
        )
        self.api = APIClient()
        self.api.force_authenticate(self.owner)


    def test_studies_page_and_filtered_api_show_compact_tracking_data(self):
        get_user_model().objects.create_user(
            username="idle-studies", first_name="Idle", last_name="Studies", email="idle-studies@example.test"
        )
        Survey.objects.create(
            source_id=555999, name="Unused Canada inventory", company_name="InnovateMR",
            country="Canada", country_code="CA", cpi="1.00",
        )
        self.client.force_login(self.owner)
        page = self.client.get(reverse("studies"))
        self.assertContains(page, "Traffic Reports")
        self.assertContains(page, "Respondent activity")
        self.assertContains(page, 'id="studyFromDateTime"')
        self.assertContains(page, 'id="studyToDateTime"')
        self.assertNotContains(page, 'id="studyFromTime"')
        self.assertContains(page, 'id="exportStudies"')
        self.assertNotContains(page, "Export full CSV")
        self.assertContains(page, "Kanik Sharma")
        self.assertContains(page, "Idle Studies")
        self.assertContains(page, "Canada · CA")
        self.assertContains(page, '<th class="study-col-cpi">CPI</th>', html=True)
        self.assertContains(page, '<th class="study-col-rid">RID / UID</th>', html=True)
        self.assertContains(page, '<th class="study-col-pid">PID</th>', html=True)
        self.assertContains(page, 'data-multi-filter="branch"')
        self.assertContains(page, 'data-multi-filter="sub_branch"')
        self.assertContains(page, 'data-multi-filter="shift"')
        self.assertContains(page, 'aria-label="Search users"')
        self.assertContains(page, 'aria-label="Search countries"')
        self.assertContains(page, 'data-multi-filter="country"')
        self.assertContains(page, 'data-multi-filter="client"')
        self.assertContains(page, 'data-multi-filter="buyer_id"')
        self.assertContains(page, "Device</th>")
        self.assertContains(page, "Start</th>")
        self.assertContains(page, "End</th>")
        self.assertContains(page, 'id="studyMetricTotal"')
        self.assertContains(page, 'id="studyMetricConversion"')
        self.assertContains(page, 'id="studyMetricRevenue"')
        self.assertContains(page, 'class="sidebar-docs"')
        self.assertNotContains(page, 'class="topbar"')

        response = self.api.get(reverse("survey-attempt-list"), {
            "user": self.kanik.pk,
            "status": SurveyAttempt.Status.COMPLETED,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        result = response.data["results"][0]
        self.assertEqual(result["pid"], self.complete.pid)
        self.assertEqual(result["rid"], self.complete.rid)
        self.assertEqual(result["prescreener_uid"], self.complete.prescreener_uid)
        self.assertEqual(result["user_name"], "Kanik Sharma")
        self.assertEqual(result["entry_ip"], "10.0.0.1")
        self.assertEqual(result["exit_ip"], "20.0.0.1")
        self.assertEqual(result["entry_device"], "Desktop")
        self.assertEqual(result["country_code"], "US")
        self.assertEqual(result["country"], "United States")
        self.assertEqual(result["termination_reason"], "")
        self.assertEqual(str(result["source_cpi_snapshot"]), "2.50")
        self.assertIsNotNone(result["initiated_at"])
        self.assertIsNotNone(result["callback_at"])
        self.assertEqual(response.data["summary"]["total"], 1)
        self.assertEqual(response.data["summary"]["completed"], 1)
        self.assertEqual(response.data["summary"]["conversion_rate"], 100.0)
        self.assertEqual(response.data["summary"]["completed_devices"]["desktop"], 1)
        self.assertEqual(response.data["summary"]["completed_devices"]["mobile"], 0)
        self.assertEqual(float(response.data["summary"]["total_revenue"]), 2.50)
        self.assertEqual(response.data["summary"]["revenue_currency"], "USD")

        uid_search = self.api.get(
            reverse("survey-attempt-list"), {"search": self.complete.prescreener_uid}
        )
        self.assertEqual(uid_search.status_code, 200)
        self.assertEqual(uid_search.data["count"], 1)
        self.assertEqual(uid_search.data["results"][0]["pid"], self.complete.pid)

    def test_role_based_super_admin_sees_pid_rid_and_uid_together(self):
        role_admin = get_user_model().objects.create_user(
            username="traffic-role-super-admin",
            email="traffic-role-super-admin@example.test",
        )
        role_admin.employee_profile.role = Role.objects.get(slug="super-admin")
        role_admin.employee_profile.save(update_fields=["role", "updated_at"])
        attempt = SurveyAttempt.objects.create(
            rid="Su1Pe2Ra3D",
            prescreener_uid="Su1p-Er2a-Dm3i-Nu4d",
            survey=self.survey,
            platform_user=role_admin,
            user_id=str(role_admin.pk),
            status=SurveyAttempt.Status.INITIATED,
        )

        self.client.force_login(role_admin)
        page = self.client.get(reverse("studies"))
        self.assertContains(page, '<th class="study-col-rid">RID / UID</th>', html=True)
        self.assertContains(page, '<th class="study-col-pid">PID</th>', html=True)

        api = APIClient()
        api.force_authenticate(role_admin)
        response = api.get(reverse("survey-attempt-list"), {"search": attempt.rid})
        self.assertEqual(response.status_code, 200)
        row = response.data["results"][0]
        self.assertEqual(row["pid"], attempt.pid)
        self.assertEqual(row["rid"], attempt.rid)
        self.assertEqual(row["prescreener_uid"], attempt.prescreener_uid)

    def test_client_buyer_and_project_deep_link_filters(self):
        response = self.api.get(reverse("survey-attempt-list"), {
            "client": str(self.survey.client_id),
            "buyer_id": "3690",
            "internal_id": self.survey.local_id,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)
        self.client.force_login(self.owner)
        page = self.client.get(reverse("studies"), {"internal_id": self.survey.local_id})
        self.assertContains(page, "Project filter")
        self.assertContains(page, self.survey.local_id)

    def test_supplier_filter_lists_and_filters_external_supplier_attempts(self):
        supplier = get_user_model().objects.create_user(
            username="traffic-supplier",
            first_name="Traffic",
            last_name="Supplier",
            email="traffic-supplier@example.test",
        )
        self.complete.vendor = supplier
        self.complete.save(update_fields=["vendor", "updated_at"])

        self.client.force_login(self.owner)
        page = self.client.get(reverse("studies"))
        self.assertContains(page, 'data-multi-filter="supplier"')
        self.assertContains(page, "Traffic Supplier")
        self.assertContains(page, 'aria-label="Search suppliers"')

        response = self.api.get(reverse("survey-attempt-list"), {"supplier": supplier.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["pid"], self.complete.pid)

    def test_traffic_report_api_exposes_clean_provider_termination_reason(self):
        self.complete.status = SurveyAttempt.Status.TERMINATED
        self.complete.upstream_transaction_data = [{
            "trackId": self.complete.rid,
            "status": "Pre Survey Termination",
            "termReason": "Off hours",
        }]
        self.complete.save(update_fields=["status", "upstream_transaction_data", "updated_at"])
        response = self.api.get(reverse("survey-attempt-list"), {"search": self.complete.rid})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"][0]["termination_reason"], "Off hours")

    def test_traffic_provider_reason_is_independently_permission_scoped(self):
        self.complete.status = SurveyAttempt.Status.TERMINATED
        self.complete.upstream_transaction_data = {
            "status": "Early termination",
            "termReason": "Provider-only reason",
        }
        self.complete.status_source = "browser_callback"
        self.complete.save(update_fields=["status", "status_source", "upstream_transaction_data", "updated_at"])
        for code in ("attempts.view", "studies.column.status"):
            UserFunctionOverride.objects.create(
                user=self.kanik,
                function=AccessFunction.objects.get(code=code),
                effect=UserFunctionOverride.Effect.ALLOW,
            )
        api = APIClient()
        api.force_authenticate(self.kanik)

        hidden = api.get(reverse("survey-attempt-list"))

        self.assertEqual(hidden.status_code, 200)
        self.assertNotIn("termination_reason", hidden.data["results"][0])
        self.assertNotIn("termination_category", hidden.data["results"][0])
        self.assertNotIn("status_source", hidden.data["results"][0])
        UserFunctionOverride.objects.create(
            user=self.kanik,
            function=AccessFunction.objects.get(code="studies.field.provider_status"),
            effect=UserFunctionOverride.Effect.ALLOW,
        )

        visible = api.get(reverse("survey-attempt-list"))

        self.assertEqual(visible.status_code, 200)
        self.assertEqual(visible.data["results"][0]["termination_reason"], "Provider-only reason")
        self.assertNotIn("status_source", visible.data["results"][0])

        UserFunctionOverride.objects.create(
            user=self.kanik,
            function=AccessFunction.objects.get(code="studies.field.status_source"),
            effect=UserFunctionOverride.Effect.ALLOW,
        )
        source_visible = api.get(reverse("survey-attempt-list"))
        self.assertEqual(source_visible.data["results"][0]["status_source"], "browser_callback")

    def test_traffic_list_omits_identity_fields_denied_to_the_user(self):
        for code in ("attempts.view", "studies.column.status"):
            UserFunctionOverride.objects.create(
                user=self.kanik,
                function=AccessFunction.objects.get(code=code),
                effect=UserFunctionOverride.Effect.ALLOW,
            )
        for code in ("studies.column.respondent_id", "studies.column.pid"):
            UserFunctionOverride.objects.create(
                user=self.kanik,
                function=AccessFunction.objects.get(code=code),
                effect=UserFunctionOverride.Effect.DENY,
            )
        api = APIClient()
        api.force_authenticate(self.kanik)

        response = api.get(reverse("survey-attempt-list"), {"include_summary": "false"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        row = response.data["results"][0]
        self.assertIn("status", row)
        self.assertIn("status_label", row)
        self.assertNotIn("rid", row)
        self.assertNotIn("pid", row)
        self.assertNotIn("prescreener_uid", row)

    def test_attempt_retrieve_requires_explicit_sensitive_audit_permission(self):
        for code in (
            "attempts.view",
            "studies.column.status",
            "studies.column.respondent_id",
            "studies.column.pid",
            "studies.column.device",
        ):
            UserFunctionOverride.objects.create(
                user=self.kanik,
                function=AccessFunction.objects.get(code=code),
                effect=UserFunctionOverride.Effect.ALLOW,
            )
        self.complete.answers = {"question": {"values": ["private answer"]}}
        self.complete.upstream_transaction_data = {"private": "provider audit"}
        self.complete.entry_client_data = {"fingerprint": "private browser data"}
        self.complete.outbound_url = "https://provider.example/private-entry"
        self.complete.save(update_fields=[
            "answers", "upstream_transaction_data", "entry_client_data", "outbound_url", "updated_at",
        ])
        api = APIClient()
        api.force_authenticate(self.kanik)

        restricted = api.get(reverse("survey-attempt-detail", args=[self.complete.rid]))

        self.assertEqual(restricted.status_code, 200)
        self.assertEqual(restricted.data["pid"], self.complete.pid)
        self.assertNotIn("rid", restricted.data)
        for field_name in (
            "answers", "upstream_transaction_data", "entry_client_data", "outbound_url",
            "entry_user_agent", "supplier_code", "callback_count", "is_verified",
        ):
            self.assertNotIn(field_name, restricted.data)

        UserFunctionOverride.objects.create(
            user=self.kanik,
            function=AccessFunction.objects.get(code="studies.detail.sensitive_audit"),
            effect=UserFunctionOverride.Effect.ALLOW,
        )
        allowed = api.get(reverse("survey-attempt-detail", args=[self.complete.rid]))

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.data["answers"], self.complete.answers)
        self.assertEqual(allowed.data["upstream_transaction_data"], self.complete.upstream_transaction_data)
        self.assertEqual(allowed.data["entry_client_data"], self.complete.entry_client_data)
        self.assertEqual(allowed.data["outbound_url"], self.complete.outbound_url)

    def test_traffic_list_is_slim_but_retrieve_keeps_full_audit_contract(self):
        response = self.api.get(reverse("survey-attempt-list"), {"search": self.complete.rid})

        self.assertEqual(response.status_code, 200)
        result = response.data["results"][0]
        self.assertNotIn("answers", result)
        self.assertNotIn("upstream_transaction_data", result)
        self.assertNotIn("entry_client_data", result)
        self.assertNotIn("outbound_url", result)
        self.assertIn("entry_device", result)
        self.assertIn("termination_reason", result)

        detail = self.api.get(reverse("survey-attempt-detail", args=[self.complete.rid]))
        self.assertEqual(detail.status_code, 200)
        self.assertIn("answers", detail.data)
        self.assertIn("upstream_transaction_data", detail.data)
        self.assertIn("entry_client_data", detail.data)
        self.assertIn("outbound_url", detail.data)

    def test_traffic_rows_and_summary_can_be_loaded_in_parallel(self):
        rows = self.api.get(
            reverse("survey-attempt-list"),
            {"search": self.complete.rid, "include_summary": "false"},
        )
        self.assertEqual(rows.status_code, 200)
        self.assertNotIn("summary", rows.data)
        self.assertEqual(rows.data["count"], 1)

        summary = self.api.get(
            reverse("survey-attempt-summary"),
            {"search": self.complete.rid},
        )
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(summary.data["count"], 1)
        self.assertEqual(summary.data["summary"]["total"], 1)

    def test_combined_traffic_keeps_a_live_pagination_count(self):
        caches["reports"].clear()

        with CaptureQueriesContext(connection) as queries:
            response = self.api.get(
                reverse("survey-attempt-list"),
                {"search": self.complete.rid},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["summary"]["total"], 1)
        count_queries = [
            item["sql"] for item in queries.captured_queries
            if "COUNT(" in item["sql"].upper()
        ]
        self.assertEqual(len(count_queries), 2)
        self.assertTrue(any('AS "__count"' in sql for sql in count_queries))

    def test_cached_traffic_summary_cannot_hide_a_new_page(self):
        caches["reports"].clear()
        first = self.api.get(
            reverse("survey-attempt-list"),
            {"page_size": 1},
        )
        self.assertEqual(first.status_code, 200)
        original_count = first.data["count"]

        SurveyAttempt.objects.create(
            rid="Pg1Na2Ti3N",
            survey=self.survey,
            platform_user=self.kanik,
            user_id=str(self.kanik.pk),
            status=SurveyAttempt.Status.INITIATED,
        )
        next_page = self.api.get(
            reverse("survey-attempt-list"),
            {"page_size": 1, "page": original_count + 1},
        )

        self.assertEqual(next_page.status_code, 200)
        self.assertEqual(next_page.data["count"], original_count + 1)
        self.assertEqual(len(next_page.data["results"]), 1)
        # KPI cards may remain briefly cached, but row pagination never does.
        self.assertEqual(next_page.data["summary"]["total"], original_count)

    def test_exact_tracking_search_uses_authoritative_match_and_partial_search_falls_back(self):
        SurveyAttempt.objects.create(
            rid="Zz1Yy2Xx3W",
            survey=self.survey,
            platform_user=self.kanik,
            user_id=f"prefix-{self.complete.rid}-suffix",
            status=SurveyAttempt.Status.INITIATED,
        )

        exact = self.api.get(reverse("survey-attempt-list"), {"search": self.complete.rid})
        self.assertEqual(exact.status_code, 200)
        self.assertEqual(exact.data["count"], 1)
        self.assertEqual(exact.data["results"][0]["pid"], self.complete.pid)

        partial = self.api.get(reverse("survey-attempt-list"), {"search": "Kanik"})
        self.assertEqual(partial.status_code, 200)
        self.assertGreaterEqual(partial.data["count"], 2)

    def test_terminal_list_normalizes_provider_outcome_once_per_row(self):
        self.complete.status = SurveyAttempt.Status.TERMINATED
        self.complete.save(update_fields=["status", "updated_at"])

        with patch("surveys.serializers.provider_outcome") as normalized:
            normalized.return_value = {"reason": "Off hours", "category": "Timing"}
            response = self.api.get(reverse("survey-attempt-list"), {"search": self.complete.rid})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"][0]["termination_reason"], "Off hours")
        self.assertEqual(response.data["results"][0]["termination_category"], "Timing")
        normalized.assert_called_once()

    def test_terminal_traffic_query_count_does_not_scale_with_page_rows(self):
        self.complete.status = SurveyAttempt.Status.TERMINATED
        self.complete.status_source = "browser_callback"
        self.complete.upstream_transaction_data = {
            "status": "Terminated",
            "termReason": "Profile mismatch",
        }
        self.complete.is_verified = True
        self.complete.exit_client_data = {
            "innovatemr_callback": {"termReason": "Profile mismatch"},
        }
        self.complete.save(update_fields=[
            "status",
            "status_source",
            "upstream_transaction_data",
            "is_verified",
            "exit_client_data",
            "updated_at",
        ])
        SurveyAttempt.objects.bulk_create([
            SurveyAttempt(
                rid=f"T{index:09d}",
                survey=self.survey,
                platform_user=self.kanik,
                user_id=str(self.kanik.pk),
                status=SurveyAttempt.Status.TERMINATED,
                status_source="browser_callback",
                upstream_transaction_data={
                    "status": "Terminated",
                    "termReason": "Profile mismatch",
                },
                is_verified=True,
                exit_client_data={
                    "innovatemr_callback": {"termReason": "Profile mismatch"},
                },
            )
            for index in range(12)
        ])

        url = reverse("survey-attempt-list")
        base_params = {
            "include_summary": "false",
            "status": SurveyAttempt.Status.TERMINATED,
        }
        warm_response = self.api.get(url, {**base_params, "page_size": 20})
        self.assertEqual(warm_response.status_code, 200)

        def captured_response(page_size):
            with CaptureQueriesContext(connection) as captured:
                response = self.api.get(url, {**base_params, "page_size": page_size})
            self.assertEqual(response.status_code, 200)
            return response, list(captured.captured_queries)

        single_row, single_row_queries = captured_response(1)
        multiple_rows, multiple_row_queries = captured_response(20)

        self.assertEqual(len(single_row.data["results"]), 1)
        self.assertGreater(len(multiple_rows.data["results"]), 1)
        self.assertIn("status_source", multiple_rows.data["results"][0])
        self.assertIn("termination_reason", multiple_rows.data["results"][0])
        self.assertEqual(len(single_row_queries), len(multiple_row_queries))

    def test_country_filter_and_hit_time_cpi_snapshot_are_stable(self):
        created = create_attempt(self.survey, self.kanik, "10.10.10.10")
        self.assertEqual(str(created.source_cpi_snapshot), "2.50")
        self.assertEqual(created.cpi_currency_snapshot, "USD")

        self.survey.cpi = "9.99"
        self.survey.save(update_fields=["cpi"])
        created.refresh_from_db()
        self.assertEqual(str(created.source_cpi_snapshot), "2.50")

        canada = Survey.objects.create(
            source_id=555124, name="Canada study", company_name="InnovateMR",
            country="Canada", country_code="CA", cpi="4.00",
        )
        SurveyAttempt.objects.create(
            rid="Cc1Aa2Nn3D", survey=canada, platform_user=self.kanik,
            user_id=str(self.kanik.pk), status=SurveyAttempt.Status.INITIATED,
            source_cpi_snapshot="4.00", cpi_currency_snapshot="USD",
        )
        response = self.api.get(reverse("survey-attempt-list"), {"country": "CA"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["country_code"], "CA")

    def test_team_lead_cpi_percentage_masks_study_snapshot_and_revenue(self):
        role = Role.objects.get(slug="team-lead")
        role.cpi_visibility_percent = "70.00"
        role.save(update_fields=["cpi_visibility_percent"])
        EmployeeProfile.objects.filter(user=self.kanik).update(role=role)
        UserFunctionOverride.objects.create(
            user=self.kanik,
            function=AccessFunction.objects.get(code="studies.card.revenue"),
            effect=UserFunctionOverride.Effect.ALLOW,
        )
        self.kanik = get_user_model().objects.get(pk=self.kanik.pk)
        scoped_api = APIClient()
        scoped_api.force_authenticate(self.kanik)

        response = scoped_api.get(reverse("survey-attempt-list"), {"status": SurveyAttempt.Status.COMPLETED})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(str(response.data["results"][0]["source_cpi_snapshot"]), "1.75")
        self.assertEqual(str(response.data["summary"]["total_revenue"]), "1.75")

    def test_summary_tracks_all_outcomes_and_completed_device_types(self):
        SurveyAttempt.objects.create(
            rid="Mm1Oo2Bb3L", survey=self.survey, platform_user=self.kanik, user_id=str(self.kanik.pk),
            status=SurveyAttempt.Status.COMPLETED, entry_device="Mobile phone",
        )
        SurveyAttempt.objects.create(
            rid="Tt1Aa2Bb3C", survey=self.survey, platform_user=self.kanik, user_id=str(self.kanik.pk),
            status=SurveyAttempt.Status.COMPLETED, entry_device="Tablet",
        )
        SurveyAttempt.objects.create(
            rid="Ii1Nn2Ii3T", survey=self.survey, platform_user=self.kanik, user_id=str(self.kanik.pk),
            status=SurveyAttempt.Status.REDIRECTED, entry_device="Desktop",
        )
        SurveyAttempt.objects.create(
            rid="Qq1Uu2Oo3T", survey=self.survey, platform_user=self.kanik, user_id=str(self.kanik.pk),
            status=SurveyAttempt.Status.OVER_QUOTA, entry_device="Desktop",
        )
        SurveyAttempt.objects.create(
            rid="Ss1Ee2Cc3U", survey=self.survey, platform_user=self.kanik, user_id=str(self.kanik.pk),
            status=SurveyAttempt.Status.QUALITY_TERMINATED, entry_device="Desktop",
        )
        response = self.api.get(reverse("survey-attempt-list"), {"user": self.kanik.pk})
        self.assertEqual(response.status_code, 200)
        summary = response.data["summary"]
        self.assertEqual(summary["total"], 6)
        self.assertEqual(summary["initiated"], 1)
        self.assertEqual(summary["completed"], 3)
        self.assertEqual(summary["over_quota"], 1)
        self.assertEqual(summary["security_terminated"], 1)
        self.assertEqual(summary["conversion_rate"], 50.0)
        self.assertEqual(summary["incidence_rate"], 100.0)
        self.assertEqual(summary["completed_devices"], {"desktop": 1, "mobile": 1, "tablet": 1, "unclassified": 0})

    def test_filtered_excel_uses_exact_operational_columns(self):
        self.complete.prescreener_uid = "TRAFFIC-UID-01"
        self.complete.save(update_fields=["prescreener_uid"])
        response = self.api.get(reverse("survey-attempt-export"), {
            "user": self.kanik.pk,
            "status": SurveyAttempt.Status.COMPLETED,
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("traffic-reports-", response["Content-Disposition"])
        self.assertIn(".xlsx", response["Content-Disposition"])
        rows = xlsx_rows(response)
        self.assertEqual(rows[0], [
            "Project id", "UID", "RID", "PID", "Status", "Final status", "Invoice month",
            "Client name", "Cleint survey id", "Cleint CPI", "Country",
            "Device", "OS", "Browser", "User agent", "Entry IP", "Exit IP",
            "Actual LOI (minutes)",
            "Inisitate at", "Presecreent at", "Redirect at", "entry date time",
            "Exit date time", "Vendor name", "User name", "Vendor CPI",
        ])
        self.assertIn("Kanik Sharma", rows[1])
        self.assertIn(self.complete.pid, rows[1])
        self.assertIn(self.complete.rid, rows[1])
        self.assertIn(self.complete.prescreener_uid, rows[1])
        self.assertNotIn("Pre-screener answers", rows[0])
        self.assertNotIn("Outbound supplier URL", rows[0])
        self.assertNotIn("Ee4Ff5Gg6H", str(rows))
        self.assertEqual(
            rows[1][rows[0].index("Actual LOI (minutes)")],
            "1.37",
        )

    def test_traffic_export_omits_columns_denied_to_the_user(self):
        UserFunctionOverride.objects.create(
            user=self.kanik,
            function=AccessFunction.objects.get(code="attempts.export"),
            effect=UserFunctionOverride.Effect.ALLOW,
        )
        UserFunctionOverride.objects.create(
            user=self.kanik,
            function=AccessFunction.objects.get(code="attempts.view"),
            effect=UserFunctionOverride.Effect.ALLOW,
        )
        UserFunctionOverride.objects.create(
            user=self.kanik,
            function=AccessFunction.objects.get(code="studies.column.ip"),
            effect=UserFunctionOverride.Effect.DENY,
        )
        UserFunctionOverride.objects.create(
            user=self.kanik,
            function=AccessFunction.objects.get(code="studies.column.respondent_id"),
            effect=UserFunctionOverride.Effect.ALLOW,
        )
        UserFunctionOverride.objects.create(
            user=self.kanik,
            function=AccessFunction.objects.get(code="studies.column.pid"),
            effect=UserFunctionOverride.Effect.DENY,
        )
        UserFunctionOverride.objects.create(
            user=self.kanik,
            function=AccessFunction.objects.get(code="studies.column.client_name"),
            effect=UserFunctionOverride.Effect.DENY,
        )
        scoped_api = APIClient()
        scoped_api.force_authenticate(self.kanik)

        rows = xlsx_rows(scoped_api.get(reverse("survey-attempt-export")))

        self.assertNotIn("Entry IP", rows[0])
        self.assertNotIn("Exit IP", rows[0])
        self.assertNotIn("Client name", rows[0])
        self.assertIn("RID", rows[0])
        self.assertIn("UID", rows[0])

        attempt_list = scoped_api.get(reverse("survey-attempt-list"))
        self.assertEqual(attempt_list.status_code, 200)
        self.assertTrue(attempt_list.data["results"])
        for attempt in attempt_list.data["results"]:
            self.assertNotIn("client_name", attempt)
            self.assertNotIn("company_name", attempt)

        self.client.force_login(self.kanik)
        page = self.client.get(reverse("traffic-reports"))
        self.assertEqual(page.status_code, 200)
        self.assertFalse(page.context["can_view_study_client_name"])

    def test_traffic_export_separates_admin_commercials_from_team_lead_cpi(self):
        role = Role.objects.get(slug="team-lead")
        role.cpi_visibility_percent = "70.00"
        role.save(update_fields=["cpi_visibility_percent"])
        branch = OrganizationUnit.objects.create(
            workspace_owner=self.owner,
            unit_type=OrganizationUnit.UnitType.BRANCH,
            name="Delhi",
            code="traffic-export-delhi",
            created_by=self.owner,
        )
        sub_branch = OrganizationUnit.objects.create(
            workspace_owner=self.owner,
            parent=branch,
            unit_type=OrganizationUnit.UnitType.SUB_BRANCH,
            name="Operations",
            code="traffic-export-operations",
            created_by=self.owner,
        )
        shift = OrganizationUnit.objects.create(
            workspace_owner=self.owner,
            parent=sub_branch,
            unit_type=OrganizationUnit.UnitType.SHIFT,
            name="Morning",
            code="traffic-export-morning",
            created_by=self.owner,
        )
        EmployeeProfile.objects.filter(user=self.kanik).update(
            role=role,
            organization_unit=shift,
        )
        UserFunctionOverride.objects.create(
            user=self.kanik,
            function=AccessFunction.objects.get(code="attempts.export"),
            effect=UserFunctionOverride.Effect.ALLOW,
        )
        self.kanik = get_user_model().objects.get(pk=self.kanik.pk)

        scoped_api = APIClient()
        scoped_api.force_authenticate(self.kanik)
        scoped_rows = xlsx_rows(scoped_api.get(reverse("survey-attempt-export")))
        self.assertNotIn("Vendor CPI", scoped_rows[0])
        self.assertNotIn("Vendor name", scoped_rows[0])
        self.assertEqual(
            scoped_rows[1][scoped_rows[0].index("Cleint CPI")],
            "1.75",
        )

        admin_rows = xlsx_rows(self.api.get(reverse("survey-attempt-export"), {
            "search": self.complete.rid,
        }))
        self.assertEqual(
            admin_rows[1][admin_rows[0].index("Cleint CPI")],
            "2.50",
        )
        self.assertEqual(admin_rows[1][admin_rows[0].index("Vendor CPI")], "1.75")
        self.assertEqual(admin_rows[1][admin_rows[0].index("Vendor name")], "Operations")

    def test_external_supplier_export_hides_admin_commercial_columns(self):
        external = get_user_model().objects.create_user(
            username="external-supplier",
            first_name="External",
            last_name="Supply",
        )
        EmployeeProfile.objects.filter(user=external).update(
            account_type=EmployeeProfile.AccountType.EXTERNAL_VENDOR,
            role=Role.objects.get(slug="external-vendor"),
            created_by=self.owner,
        )
        UserFunctionOverride.objects.create(
            user=external,
            function=AccessFunction.objects.get(code="attempts.export"),
            effect=UserFunctionOverride.Effect.ALLOW,
        )
        UserFunctionOverride.objects.create(
            user=external,
            function=AccessFunction.objects.get(code="studies.column.cpi"),
            effect=UserFunctionOverride.Effect.ALLOW,
        )
        external_attempt = SurveyAttempt.objects.create(
            rid="Xx1Ee2Vv3D",
            survey=self.survey,
            platform_user=external,
            vendor=external,
            user_id=str(external.pk),
            status=SurveyAttempt.Status.COMPLETED,
            source_cpi_snapshot="2.50",
            cpi_cut_percent_snapshot="30.00",
            payable_cpi_snapshot="1.75",
            cpi_currency_snapshot="USD",
        )

        external_api = APIClient()
        external_api.force_authenticate(external)
        external_rows = xlsx_rows(external_api.get(reverse("survey-attempt-export")))
        self.assertNotIn("Vendor CPI", external_rows[0])
        self.assertNotIn("Vendor name", external_rows[0])
        self.assertEqual(
            external_rows[1][external_rows[0].index("Cleint CPI")],
            "1.75",
        )

        admin_rows = xlsx_rows(self.api.get(reverse("survey-attempt-export"), {
            "search": external_attempt.rid,
        }))
        self.assertEqual(
            admin_rows[1][admin_rows[0].index("Cleint CPI")],
            "2.50",
        )
        self.assertEqual(admin_rows[1][admin_rows[0].index("Vendor CPI")], "1.75")
        self.assertEqual(
            admin_rows[1][admin_rows[0].index("Vendor name")],
            "External Supply",
        )

    def test_view_permission_is_scoped_and_does_not_grant_csv_export(self):
        viewer = get_user_model().objects.create_user(username="viewer", first_name="Scoped")
        UserFunctionOverride.objects.create(
            user=viewer,
            function=AccessFunction.objects.get(code="attempts.view"),
            effect=UserFunctionOverride.Effect.ALLOW,
        )
        for code in ("studies.card.total", "studies.column.respondent_id"):
            UserFunctionOverride.objects.create(
                user=viewer,
                function=AccessFunction.objects.get(code=code),
                effect=UserFunctionOverride.Effect.ALLOW,
            )
        own_attempt = SurveyAttempt.objects.create(
            rid="Ii7Jj8Kk9L", survey=self.survey, platform_user=viewer, user_id=str(viewer.pk),
            status=SurveyAttempt.Status.INITIATED,
        )
        scoped_api = APIClient()
        scoped_api.force_authenticate(viewer)
        listing = scoped_api.get(reverse("survey-attempt-list"))
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.data["count"], 1)
        self.assertEqual(listing.data["results"][0]["pid"], own_attempt.pid)
        self.assertNotIn("rid", listing.data["results"][0])
        self.assertEqual(scoped_api.get(reverse("survey-attempt-export")).status_code, 403)

        self.client.force_login(viewer)
        page = self.client.get(reverse("studies"))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'id="exportStudies"')
        self.assertNotContains(page, 'id="studySearch"')
        self.assertNotContains(page, "Status</th>")
        self.assertContains(page, 'id="studyMetricTotal"')
        self.assertNotContains(page, 'id="studyMetricCompleted"')
        self.assertEqual(scoped_api.get(reverse("survey-attempt-list"), {"status": "1"}).status_code, 403)
        self.assertEqual(scoped_api.get(reverse("survey-attempt-list"), {"country": "US"}).status_code, 403)

    def test_team_lead_sees_lower_rank_employee_activity_in_own_shift_only(self):
        team_lead = get_user_model().objects.create_user(
            username="tracking-lead", first_name="Tracking", last_name="Lead"
        )
        second_team_lead = get_user_model().objects.create_user(
            username="tracking-lead-two", first_name="Second", last_name="Lead"
        )
        employee = get_user_model().objects.create_user(
            username="tracking-employee", first_name="Branch", last_name="Employee"
        )
        other_branch_employee = get_user_model().objects.create_user(
            username="other-branch-employee", first_name="Other", last_name="Branch"
        )
        manager = get_user_model().objects.create_user(
            username="tracking-manager", first_name="Branch", last_name="Manager"
        )
        other_shift_employee = get_user_model().objects.create_user(
            username="tracking-evening-employee", first_name="Evening", last_name="Employee"
        )
        delhi = OrganizationUnit.objects.create(
            workspace_owner=self.owner, unit_type=OrganizationUnit.UnitType.BRANCH,
            name="Delhi", code="delhi", created_by=self.owner,
        )
        delhi_ops = OrganizationUnit.objects.create(
            workspace_owner=self.owner, parent=delhi, unit_type=OrganizationUnit.UnitType.SUB_BRANCH,
            name="Operations", code="operations", created_by=self.owner,
        )
        delhi_morning = OrganizationUnit.objects.create(
            workspace_owner=self.owner, parent=delhi_ops, unit_type=OrganizationUnit.UnitType.SHIFT,
            name="Morning", code="morning", created_by=self.owner,
        )
        delhi_support = OrganizationUnit.objects.create(
            workspace_owner=self.owner, parent=delhi, unit_type=OrganizationUnit.UnitType.SUB_BRANCH,
            name="Support", code="support", created_by=self.owner,
        )
        delhi_evening = OrganizationUnit.objects.create(
            workspace_owner=self.owner, parent=delhi_support, unit_type=OrganizationUnit.UnitType.SHIFT,
            name="Evening", code="evening", created_by=self.owner,
        )
        mumbai = OrganizationUnit.objects.create(
            workspace_owner=self.owner, unit_type=OrganizationUnit.UnitType.BRANCH,
            name="Mumbai", code="mumbai", created_by=self.owner,
        )
        mumbai_ops = OrganizationUnit.objects.create(
            workspace_owner=self.owner, parent=mumbai, unit_type=OrganizationUnit.UnitType.SUB_BRANCH,
            name="Operations", code="operations", created_by=self.owner,
        )
        mumbai_morning = OrganizationUnit.objects.create(
            workspace_owner=self.owner, parent=mumbai_ops, unit_type=OrganizationUnit.UnitType.SHIFT,
            name="Morning", code="morning", created_by=self.owner,
        )
        profiles = [
            (team_lead, "team-lead", delhi_morning),
            (second_team_lead, "team-lead", delhi_morning),
            (employee, "employee", delhi_morning),
            (other_shift_employee, "employee", delhi_evening),
            (other_branch_employee, "employee", mumbai_morning),
            (manager, "manager", delhi_morning),
        ]
        for platform_user, role_slug, organization_unit in profiles:
            EmployeeProfile.objects.filter(user=platform_user).update(
                role=Role.objects.get(slug=role_slug),
                created_by=self.owner,
                organization_unit=organization_unit,
            )

        visible_attempt = SurveyAttempt.objects.create(
            rid="Tl1Ee2Aa3D", survey=self.survey, platform_user=employee, user_id=str(employee.pk),
            status=SurveyAttempt.Status.COMPLETED, entry_device="Desktop",
        )
        other_shift_attempt = SurveyAttempt.objects.create(
            rid="Tl0Ee1Dd2E", survey=self.survey, platform_user=other_shift_employee,
            user_id=str(other_shift_employee.pk), status=SurveyAttempt.Status.TERMINATED, entry_device="Mobile",
        )
        SurveyAttempt.objects.create(
            rid="Tl4Oo5Bb6M", survey=self.survey, platform_user=other_branch_employee,
            user_id=str(other_branch_employee.pk), status=SurveyAttempt.Status.COMPLETED, entry_device="Mobile",
        )
        SurveyAttempt.objects.create(
            rid="Tl7Mm8Cc9R", survey=self.survey, platform_user=manager, user_id=str(manager.pk),
            status=SurveyAttempt.Status.COMPLETED, entry_device="Tablet",
        )

        lead_api = APIClient()
        lead_api.force_authenticate(team_lead)
        studies = lead_api.get(reverse("survey-attempt-list"))
        self.assertEqual(studies.status_code, 200)
        self.assertEqual(studies.data["count"], 1)
        self.assertEqual({row["pid"] for row in studies.data["results"]}, {visible_attempt.pid})
        branch_studies = lead_api.get(reverse("survey-attempt-list"), {"branch": str(delhi.pk)})
        self.assertEqual(branch_studies.status_code, 200)
        self.assertEqual(branch_studies.data["count"], 1)
        sub_branch_studies = lead_api.get(reverse("survey-attempt-list"), {"sub_branch": str(delhi_support.pk)})
        self.assertEqual(sub_branch_studies.status_code, 200)
        self.assertEqual(sub_branch_studies.data["count"], 0)
        shift_studies = lead_api.get(reverse("survey-attempt-list"), {"shift": str(delhi_morning.pk)})
        self.assertEqual(shift_studies.status_code, 200)
        self.assertEqual({row["pid"] for row in shift_studies.data["results"]}, {visible_attempt.pid})

        hits = lead_api.get(reverse("user-hits-api"))
        self.assertEqual(hits.status_code, 200)
        self.assertEqual(hits.data["count"], 1)
        self.assertEqual({row["user_id"] for row in hits.data["results"]}, {employee.pk})
        morning_hit = next(row for row in hits.data["results"] if row["user_id"] == employee.pk)
        self.assertEqual(morning_hit["branch"], "Delhi")
        self.assertEqual(morning_hit["sub_branch"], "Operations")
        self.assertEqual(morning_hit["shift"], "Morning")
        branch_hits = lead_api.get(reverse("user-hits-api"), {"branch": str(delhi.pk)})
        self.assertEqual(branch_hits.status_code, 200)
        self.assertEqual(branch_hits.data["count"], 1)
        shift_hits = lead_api.get(reverse("user-hits-api"), {"shift": str(delhi_morning.pk)})
        self.assertEqual(shift_hits.status_code, 200)
        self.assertEqual({row["user_id"] for row in shift_hits.data["results"]}, {employee.pk})

        second_lead_api = APIClient()
        second_lead_api.force_authenticate(second_team_lead)
        second_lead_studies = second_lead_api.get(reverse("survey-attempt-list"))
        self.assertEqual(second_lead_studies.status_code, 200)
        self.assertEqual(second_lead_studies.data["count"], 1)
        self.assertEqual({row["pid"] for row in second_lead_studies.data["results"]}, {visible_attempt.pid})

        for code in ("attempts.view", "user_hits.view", "user_hits.column.user"):
            UserFunctionOverride.objects.update_or_create(
                user=employee, function=AccessFunction.objects.get(code=code),
                defaults={"effect": UserFunctionOverride.Effect.ALLOW},
            )
        employee_api = APIClient()
        employee_api.force_authenticate(employee)
        employee_studies = employee_api.get(reverse("survey-attempt-list"))
        self.assertEqual(employee_studies.status_code, 200)
        self.assertEqual(employee_studies.data["count"], 1)
        self.assertEqual(employee_studies.data["results"][0]["pid"], visible_attempt.pid)
        employee_hits = employee_api.get(reverse("user-hits-api"))
        self.assertEqual(employee_hits.status_code, 200)
        self.assertEqual(employee_hits.data["count"], 1)
        self.assertEqual(employee_hits.data["results"][0]["user_id"], employee.pk)

        self.client.force_login(team_lead)
        page = self.client.get(reverse("studies"))
        self.assertContains(page, "Branch Employee")
        self.assertNotContains(page, "Evening Employee")
        self.assertNotContains(page, "Other Branch")
        self.assertNotContains(page, "Branch Manager")

    def test_upstream_transaction_reconciles_legacy_redirect_status_ip_and_loi(self):
        initiated_at = timezone.now() - timedelta(minutes=63)
        redirected_at = timezone.now() - timedelta(minutes=3)
        attempt = SurveyAttempt.objects.create(
            rid="Mm1Nn2Oo3P", survey=self.survey, platform_user=self.kanik, user_id=str(self.kanik.pk),
            status=SurveyAttempt.Status.REDIRECTED,
            initiated_at=initiated_at,
            redirected_at=redirected_at,
            initiation_ip="127.0.0.1",
        )
        upstream_time = timezone.now()
        client = Mock()
        client.get_survey_transactions_by_pid.return_value = [{
            "PID": attempt.rid,
            "trackId": attempt.rid,
            "status": "Completed",
            "ip": "8.8.4.4",
            "completeDateTime": upstream_time.isoformat(),
            "verifyToken": "Valid",
        }]
        self.assertTrue(reconcile_attempt_status(client, attempt))
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.COMPLETED)
        self.assertEqual(attempt.status_source, "innovatemr_transaction")
        self.assertEqual(attempt.initiation_ip, "8.8.4.4")
        self.assertEqual(attempt.callback_ip, "8.8.4.4")
        self.assertGreaterEqual(attempt.loi_seconds, 3779)
        self.assertLess(attempt.loi_seconds, 3790)
        self.assertTrue(attempt.is_verified)
        self.assertEqual(attempt.upstream_transaction_data["trackId"], attempt.rid)

    def test_upstream_pre_survey_statuses_collapse_into_five_ui_outcomes(self):
        cases = [("Pre-Survey Termination", "2"), ("Pre-Survey Over Quota", "3"), ("Pre-Survey Quality Term", "4")]
        for index, (upstream_status, expected) in enumerate(cases):
            attempt = SurveyAttempt.objects.create(
                rid=f"Qq{index}Rr{index}Ss{index}T", survey=self.survey, platform_user=self.kanik,
                user_id=str(self.kanik.pk), status=SurveyAttempt.Status.REDIRECTED,
            )
            client = Mock()
            client.get_survey_transactions_by_pid.return_value = [{
                "PID": attempt.rid, "status": upstream_status, "ip": "9.9.9.9",
            }]
            reconcile_attempt_status(client, attempt)
            attempt.refresh_from_db()
            self.assertEqual(attempt.status, expected)


class TerminationReasonPageTests(TestCase):
    def setUp(self):
        caches["reports"].clear()
        self.owner = get_user_model().objects.create_superuser(
            username="reason-owner", email="reason-owner@example.test", password="test-password"
        )
        self.respondent = get_user_model().objects.create_user(
            username="reason-respondent", first_name="Reason", last_name="Tester"
        )
        client = Client.objects.create(
            code="reason-innovate", name="InnovateMR", provider_code="innovatemr"
        )
        integration = ClientIntegration.objects.create(
            client=client,
            name="InnovateMR reason test",
            provider_code="innovatemr",
            base_url="https://supplier.innovatemr.net/api/v2",
        )
        self.survey = Survey.objects.create(
            source_id=16003381,
            name="Reason lookup survey",
            client=client,
            integration=integration,
            entry_link="https://edgeapi.innovatemr.net/startSurvey?PID=[%%pid%%]",
        )
        self.attempt = SurveyAttempt.objects.create(
            rid="EqY33Hq0jH",
            survey=self.survey,
            platform_user=self.respondent,
            user_id=str(self.respondent.pk),
            status=SurveyAttempt.Status.TERMINATED,
            status_source="browser_callback",
            callback_at=timezone.now(),
            callback_count=1,
            initiation_ip="172.56.27.197",
        )

    @patch("surveys.views.InnovateMRClient.get_survey_transactions_by_pid")
    def test_search_fetches_caches_and_displays_exact_provider_reason(self, transaction_lookup):
        transaction_lookup.return_value = [{
            "status": "Pre Survey Termination",
            "termReason": "Off hours",
            "trackId": self.attempt.rid,
            "ip": "172.56.27.197",
        }]
        self.client.force_login(self.owner)

        response = self.client.get(reverse("termination-reasons"), {"rid": self.attempt.rid})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pre Survey Termination")
        self.assertContains(response, "Off hours")
        self.assertContains(response, "Term Reports")
        self.assertEqual(response.context["detail_outcome"], {
            "status": "Pre Survey Termination",
            "reason": "Off hours",
            "category": "",
        })
        transaction_lookup.assert_called_once_with(self.survey.source_id, self.attempt.rid)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.upstream_transaction_data["termReason"], "Off hours")
        self.assertIsNotNone(self.attempt.upstream_checked_at)

    def test_cached_innovate_transaction_renders_clean_fields_not_raw_json(self):
        raw_transaction = {
            "id": "TqU3aQwdQTeKvf3U5r2DPSE",
            "ip": "49.145.217.139",
            "CPI": "2.55",
            "status": "Pre Survey Quality Termination",
            "trackId": self.attempt.rid,
            "termReason": "Selected threat potential score at joblevel does not allow the survey",
            "verifyToken": "Pending",
        }
        self.attempt.status = SurveyAttempt.Status.QUALITY_TERMINATED
        self.attempt.upstream_transaction_data = raw_transaction
        self.attempt.save(update_fields=["status", "upstream_transaction_data"])
        self.client.force_login(self.owner)

        with patch("surveys.views.InnovateMRClient.get_survey_transactions_by_pid") as lookup:
            response = self.client.get(
                reverse("termination-reasons"), {"detail": self.attempt.rid}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["detail_outcome"], {
            "status": "Pre Survey Quality Termination",
            "reason": "Selected threat potential score at joblevel does not allow the survey",
            "category": "",
        })
        self.assertContains(response, "Pre Survey Quality Termination")
        self.assertContains(
            response, "Selected threat potential score at joblevel does not allow the survey"
        )
        self.assertNotContains(response, "TqU3aQwdQTeKvf3U5r2DPSE")
        self.assertNotContains(response, "Platform status")
        lookup.assert_not_called()

    def test_admin_role_has_page_by_default_and_employee_is_forbidden(self):
        admin_user = get_user_model().objects.create_user(username="reason-admin")
        EmployeeProfile.objects.filter(user=admin_user).update(role=Role.objects.get(slug="admin"))
        self.client.force_login(admin_user)
        self.assertEqual(self.client.get(reverse("termination-reasons")).status_code, 200)

        employee = get_user_model().objects.create_user(username="reason-employee")
        EmployeeProfile.objects.filter(user=employee).update(role=Role.objects.get(slug="employee"))
        self.client.force_login(employee)
        self.assertEqual(self.client.get(reverse("termination-reasons")).status_code, 403)

    def test_page_lists_every_unsuccessful_status_before_rid_search(self):
        SurveyAttempt.objects.create(
            rid="Quota1Ab2C",
            survey=self.survey,
            platform_user=self.respondent,
            status=SurveyAttempt.Status.OVER_QUOTA,
            callback_at=timezone.now(),
        )
        SurveyAttempt.objects.create(
            rid="Quali1Ab2C",
            survey=self.survey,
            platform_user=self.respondent,
            status=SurveyAttempt.Status.QUALITY_TERMINATED,
            callback_at=timezone.now(),
        )
        self.client.force_login(self.owner)

        response = self.client.get(reverse("termination-reasons"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["summary"]["total"], 3)
        self.assertEqual(response.context["summary"]["terminated"], 1)
        self.assertEqual(response.context["summary"]["quota"], 1)
        self.assertEqual(response.context["summary"]["quality"], 1)
        self.assertContains(response, self.attempt.rid)
        self.assertContains(response, "Quota1Ab2C")
        self.assertContains(response, "Quali1Ab2C")
        self.assertContains(response, "Details", count=3)

    def test_term_page_query_count_does_not_scale_with_page_rows(self):
        SurveyAttempt.objects.bulk_create([
            SurveyAttempt(
                rid=f"R{index:09d}",
                survey=self.survey,
                platform_user=self.respondent,
                user_id=str(self.respondent.pk),
                status=SurveyAttempt.Status.TERMINATED,
                status_source="browser_callback",
                callback_at=timezone.now(),
                upstream_transaction_data={
                    "status": "Terminated",
                    "termReason": "Profile mismatch",
                },
            )
            for index in range(24)
        ])
        self.client.force_login(self.owner)
        url = reverse("termination-reasons")

        self.client.get(url)
        self.client.get(url, {"search": self.attempt.rid})

        with CaptureQueriesContext(connection) as single_row_queries:
            single_row = self.client.get(url, {"search": self.attempt.rid})
        with CaptureQueriesContext(connection) as multiple_row_queries:
            multiple_rows = self.client.get(url)

        self.assertEqual(single_row.status_code, 200)
        self.assertEqual(multiple_rows.status_code, 200)
        self.assertEqual(len(single_row.context["page_obj"].object_list), 1)
        self.assertEqual(len(multiple_rows.context["page_obj"].object_list), 20)
        self.assertEqual(len(single_row_queries), len(multiple_row_queries))

    def test_term_page_skips_provider_outcome_when_details_are_not_permitted(self):
        viewer = get_user_model().objects.create_user(username="reason-list-only")
        for code in (
            "termination_reasons.view",
            "termination_reasons.column.rid",
        ):
            UserFunctionOverride.objects.create(
                user=viewer,
                function=AccessFunction.objects.get(code=code),
                effect=UserFunctionOverride.Effect.ALLOW,
            )
        self.attempt.platform_user = viewer
        self.attempt.user_id = str(viewer.pk)
        self.attempt.save(update_fields=["platform_user", "user_id", "updated_at"])
        self.client.force_login(viewer)

        with patch("surveys.views.provider_outcome") as normalized:
            response = self.client.get(reverse("termination-reasons"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.attempt.rid)
        normalized.assert_not_called()

    def test_traffic_style_filters_support_multiple_statuses_country_and_search(self):
        self.survey.country_code = "US"
        self.survey.country = "United States"
        self.survey.buyer_id = "buyer-a"
        self.survey.save(update_fields=["country_code", "country", "buyer_id"])
        quota = SurveyAttempt.objects.create(
            rid="Quota2Ab3D",
            survey=self.survey,
            platform_user=self.respondent,
            status=SurveyAttempt.Status.OVER_QUOTA,
            callback_at=timezone.now(),
        )
        SurveyAttempt.objects.create(
            rid="Quali2Ab3D",
            survey=self.survey,
            platform_user=self.respondent,
            status=SurveyAttempt.Status.QUALITY_TERMINATED,
            callback_at=timezone.now(),
        )
        self.client.force_login(self.owner)

        response = self.client.get(reverse("termination-reasons"), {
            "search": self.survey.local_id,
            "status": [SurveyAttempt.Status.TERMINATED, SurveyAttempt.Status.OVER_QUOTA],
            "country": ["US"],
            "buyer_id": ["buyer-a"],
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["summary"]["total"], 2)
        self.assertContains(response, self.attempt.rid)
        self.assertContains(response, quota.rid)
        self.assertNotContains(response, "Quali2Ab3D")
        self.assertContains(response, "Sub-client / Buyer ID")
        self.assertContains(response, "From date &amp; time")

    def test_filtered_excel_contains_platform_and_provider_statuses(self):
        self.attempt.upstream_transaction_data = {
            "status": "Pre Survey Termination",
            "termReason": "Off hours",
        }
        self.attempt.prescreener_uid = "TERM-UID-0001"
        self.attempt.save(update_fields=["upstream_transaction_data", "prescreener_uid"])
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("termination-reasons-export"),
            {"status": SurveyAttempt.Status.TERMINATED, "search": self.attempt.rid},
        )
        rows = xlsx_rows(response)

        self.assertEqual(response.status_code, 200)
        self.assertIn("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", response["Content-Type"])
        self.assertIn("Platform status", rows[0])
        self.assertIn("Provider status", rows[0])
        self.assertEqual(
            [rows[1][rows[0].index(column)] for column in ("RID", "PID", "UID")],
            [self.attempt.rid, self.attempt.pid, self.attempt.prescreener_uid],
        )
        self.assertIn("Terminated", rows[1])
        self.assertIn("Pre Survey Termination", rows[1])
        self.assertIn("Off hours", rows[1])

    def test_scoped_viewer_only_sees_own_rows_and_exported_columns(self):
        viewer = get_user_model().objects.create_user(
            username="reason-scoped", first_name="Scoped", last_name="Viewer"
        )
        own_attempt = SurveyAttempt.objects.create(
            rid="OwnRe1Ab2C",
            survey=self.survey,
            platform_user=viewer,
            user_id=str(viewer.pk),
            status=SurveyAttempt.Status.TERMINATED,
            callback_at=timezone.now(),
            upstream_transaction_data={"status": "Early termination", "termReason": "Own reason"},
        )
        for code in (
            "termination_reasons.view",
            "termination_reasons.export",
            "termination_reasons.column.rid",
        ):
            UserFunctionOverride.objects.create(
                user=viewer,
                function=AccessFunction.objects.get(code=code),
                effect=UserFunctionOverride.Effect.ALLOW,
            )
        self.client.force_login(viewer)

        page = self.client.get(reverse("termination-reasons"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, own_attempt.rid)
        self.assertNotContains(page, self.attempt.rid)
        self.assertNotContains(page, self.survey.local_id)
        self.assertNotContains(page, "Own reason")

        export = self.client.get(reverse("termination-reasons-export"))
        rows = xlsx_rows(export)
        self.assertEqual(export.status_code, 200)
        self.assertEqual(rows[0], ["RID", "PID", "UID"])
        self.assertEqual(rows[1], [own_attempt.rid, own_attempt.pid, ""])

    def test_supplier_filter_limits_unsuccessful_outcomes(self):
        supplier = get_user_model().objects.create_user(
            username="reason-filter-supplier", first_name="Reason", last_name="Supplier"
        )
        self.attempt.vendor = supplier
        self.attempt.save(update_fields=["vendor"])
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("termination-reasons"), {"supplier": str(supplier.pk)}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["summary"]["total"], 1)
        self.assertContains(response, self.attempt.rid)
        self.assertContains(response, "Supplier")

    def test_external_supplier_can_be_granted_scoped_term_reports(self):
        supplier = get_user_model().objects.create_user(
            username="external-reason-supplier",
            first_name="External",
            last_name="Supplier",
            password="test-password",
        )
        EmployeeProfile.objects.update_or_create(
            user=supplier,
            defaults={
                "account_type": EmployeeProfile.AccountType.EXTERNAL_VENDOR,
                "role": Role.objects.get(slug="external-vendor"),
            },
        )
        VendorCommercialProfile.objects.create(
            vendor=supplier,
            delivery_mode=VendorCommercialProfile.DeliveryMode.PANEL,
            is_active=True,
        )
        own_attempt = SurveyAttempt.objects.create(
            rid="ExtRe1Ab2C",
            survey=self.survey,
            platform_user=supplier,
            user_id=str(supplier.pk),
            status=SurveyAttempt.Status.TERMINATED,
            callback_at=timezone.now(),
            upstream_transaction_data={
                "status": "Early termination",
                "termReason": "Supplier scoped reason",
            },
        )
        for code in (
            "termination_reasons.view",
            "termination_reasons.column.rid",
            "termination_reasons.column.status",
            "termination_reasons.table.provider_status",
            "termination_reasons.table.reason",
        ):
            UserFunctionOverride.objects.create(
                user=supplier,
                function=AccessFunction.objects.get(code=code),
                effect=UserFunctionOverride.Effect.ALLOW,
            )
        self.assertTrue(self.client.login(
            username="external-reason-supplier",
            password="test-password",
        ))

        response = self.client.get(reverse("termination-reasons"))

        self.assertEqual(response.status_code, 200, response.headers.get("Location"))
        self.assertContains(response, "Term Reports")
        self.assertContains(response, own_attempt.rid)
        self.assertContains(response, "Supplier scoped reason")
        self.assertNotContains(response, self.attempt.rid)

    def test_rfg_detail_uses_stored_provider_callback_reason(self):
        client = Client.objects.create(code="reason-rfg", name="Research For Good", provider_code="rfg")
        integration = ClientIntegration.objects.create(
            client=client,
            name="RFG reason test",
            provider_code="rfg",
            base_url="https://api.researchforgood.com/API",
        )
        survey = Survey.objects.create(
            source_key="RFG-REASON-1",
            name="RFG reason survey",
            client=client,
            integration=integration,
        )
        attempt = SurveyAttempt.objects.create(
            rid="RfgSec1Ab2",
            survey=survey,
            platform_user=self.respondent,
            status=SurveyAttempt.Status.QUALITY_TERMINATED,
            callback_at=timezone.now(),
            upstream_transaction_data={"rfg_callback": {"result": "10", "liveS": "2"}},
        )
        self.client.force_login(self.owner)

        response = self.client.get(reverse("termination-reasons"), {"detail": attempt.rid})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Security termination")
        self.assertContains(response, "Suspicious or proxy IP address")

    def test_custom_provider_can_map_nested_outcome_fields(self):
        client = Client.objects.create(code="reason-custom", name="Future Client", provider_code="custom")
        integration = ClientIntegration.objects.create(
            client=client,
            name="Custom reason test",
            provider_code="custom",
            base_url="https://provider.example.test/api",
            field_mapping={
                "outcome_status": "provider.state",
                "outcome_reason": "provider.explanation",
                "outcome_category": "provider.group",
            },
        )
        survey = Survey.objects.create(
            source_key="CUSTOM-REASON-1",
            name="Custom reason survey",
            client=client,
            integration=integration,
        )
        attempt = SurveyAttempt.objects.create(
            rid="CstmRe1Ab2",
            survey=survey,
            platform_user=self.respondent,
            status=SurveyAttempt.Status.TERMINATED,
            callback_at=timezone.now(),
            upstream_transaction_data={
                "provider": {
                    "state": "Rejected",
                    "explanation": "Outside audience",
                    "group": "Targeting",
                }
            },
        )
        self.client.force_login(self.owner)

        response = self.client.get(reverse("termination-reasons"), {"detail": attempt.rid})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Rejected")
        self.assertContains(response, "Outside audience")
        self.assertContains(response, "Category: Targeting")

    def test_non_terminal_attempt_does_not_call_provider(self):
        self.attempt.status = SurveyAttempt.Status.REDIRECTED
        self.attempt.save(update_fields=["status"])
        self.client.force_login(self.owner)
        with patch("surveys.views.InnovateMRClient.get_survey_transactions_by_pid") as lookup:
            response = self.client.get(reverse("termination-reasons"), {"rid": self.attempt.rid})
        self.assertContains(response, "currently redirected to survey")
        lookup.assert_not_called()


class UserHitsTests(TestCase):
    def setUp(self):
        caches["reports"].clear()
        self.owner = get_user_model().objects.create_superuser(
            username="hits-owner", email="hits-owner@example.test", password="test-password"
        )
        self.kanik = get_user_model().objects.create_user(
            username="kanik-hits", first_name="Kanik", last_name="Gupta", email="kanik-hits@example.test"
        )
        self.other = get_user_model().objects.create_user(
            username="other-hits", first_name="Other", last_name="User", email="other-hits@example.test"
        )
        EmployeeProfile.objects.filter(user=self.kanik).update(
            company_name="Gurgaon", department="Operations", created_by=self.owner
        )
        EmployeeProfile.objects.filter(user=self.other).update(
            company_name="Mumbai", department="Research", created_by=self.owner
        )
        self.survey = Survey.objects.create(source_id=909090, name="User hit metrics")
        today = timezone.localdate()
        yesterday = today - timedelta(days=1)
        self.today = today
        today_at_ten = timezone.make_aware(datetime.combine(today, time(10, 0)))
        yesterday_at_ten = timezone.make_aware(datetime.combine(yesterday, time(10, 0)))

        SurveyAttempt.objects.create(
            rid="Dh1Aa2Bb3C", survey=self.survey, platform_user=self.kanik, user_id=str(self.kanik.pk),
            status=SurveyAttempt.Status.COMPLETED, entry_device="Desktop", initiated_at=today_at_ten,
        )
        SurveyAttempt.objects.create(
            rid="Mh2Cc3Dd4E", survey=self.survey, platform_user=self.kanik, user_id=str(self.kanik.pk),
            status=SurveyAttempt.Status.TERMINATED, entry_device="Mobile", initiated_at=today_at_ten,
        )
        SurveyAttempt.objects.create(
            rid="Th3Ee4Ff5G", survey=self.survey, platform_user=self.kanik, user_id=str(self.kanik.pk),
            status=SurveyAttempt.Status.COMPLETED, entry_device="Tablet", initiated_at=yesterday_at_ten,
        )
        SurveyAttempt.objects.create(
            rid="Dh4Gg5Hh6I", survey=self.survey, platform_user=self.other, user_id=str(self.other.pk),
            status=SurveyAttempt.Status.COMPLETED, entry_device="Desktop", initiated_at=today_at_ten,
        )
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def test_page_and_api_aggregate_user_day_device_counts(self):
        idle_user = get_user_model().objects.create_user(
            username="idle-hits", first_name="Idle", last_name="Employee", email="idle@example.test"
        )
        self.client.force_login(self.owner)
        page = self.client.get(reverse("user-hits"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "User activity")
        self.assertContains(page, "Gurgaon")
        self.assertContains(page, "Operations")
        self.assertContains(page, 'id="hitFromDateTime"')
        self.assertContains(page, 'id="hitToDateTime"')
        self.assertNotContains(page, 'id="hitFromTime"')
        self.assertContains(page, "Idle Employee")
        self.assertContains(page, 'aria-label="Search users"')
        self.assertContains(page, 'aria-label="Search branches"')
        self.assertContains(page, 'id="hitIncidenceRate"')
        self.assertContains(page, 'id="hitCompleteDesktop"')

        response = self.api.get(reverse("user-hits-api"), {
            "user": self.kanik.pk,
            "from_date": self.today.isoformat(),
            "to_date": self.today.isoformat(),
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        result = response.data["results"][0]
        self.assertEqual(result["branch"], "Gurgaon")
        self.assertEqual(result["sub_branch"], "Operations")
        self.assertEqual(result["hits"], {
            "total": 2, "desktop": 1, "mobile": 1, "tablet": 0, "unclassified": 0,
        })
        self.assertEqual(result["completes"], {
            "total": 1, "desktop": 1, "mobile": 0, "tablet": 0, "unclassified": 0,
        })
        self.assertEqual(response.data["summary"]["conversion_rate"], 50.0)
        self.assertEqual(response.data["summary"]["incidence_rate"], 50.0)

    def test_time_filters_narrow_ist_date_boundaries(self):
        response = self.api.get(reverse("user-hits-api"), {
            "from_date": self.today.isoformat(),
            "from_time": "10:01",
            "to_date": self.today.isoformat(),
            "to_time": "23:59",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)

        invalid = self.api.get(reverse("user-hits-api"), {"from_time": "10:00"})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.data["detail"], "from_time requires from_date.")

    def test_branch_filter_and_all_date_rows(self):
        response = self.api.get(reverse("user-hits-api"), {"branch": "Gurgaon"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)
        self.assertTrue(all(row["user_id"] == self.kanik.pk for row in response.data["results"]))
        self.assertEqual(response.data["summary"]["hits"]["tablet"], 1)

    def test_supplier_filter_limits_user_hit_aggregates(self):
        supplier = get_user_model().objects.create_user(
            username="hits-filter-supplier", first_name="Hit", last_name="Supplier"
        )
        SurveyAttempt.objects.filter(
            platform_user=self.kanik, initiated_at__date=self.today,
        ).update(vendor=supplier)

        response = self.api.get(reverse("user-hits-api"), {
            "supplier": str(supplier.pk),
            "from_date": self.today.isoformat(),
            "to_date": self.today.isoformat(),
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["user_id"], self.kanik.pk)
        self.assertEqual(response.data["results"][0]["hits"]["total"], 2)

    def test_super_admin_user_filter_uses_narrow_attempt_index_scope(self):
        from .user_hits import aggregate_user_hit_payload

        caches["reports"].clear()
        with CaptureQueriesContext(connection) as captured:
            payload = aggregate_user_hit_payload(
                self.owner,
                {"user": str(self.kanik.pk)},
            )

        self.assertTrue(payload["rows"])
        attempt_sql = [
            query["sql"] for query in captured.captured_queries
            if "surveys_surveyattempt" in query["sql"].lower()
        ]
        self.assertTrue(attempt_sql)
        expected_scope = (
            '"surveys_surveyattempt"."platform_user_id" IN '
            f'({self.kanik.pk})'
        )
        self.assertTrue(any(expected_scope in sql for sql in attempt_sql))

    def test_compact_aggregate_is_paged_before_row_expansion_and_projection(self):
        from .user_hits import expand_user_hit_rows as real_expand_user_hit_rows

        caches["reports"].clear()
        with (
            patch(
                "surveys.views.expand_user_hit_rows",
                wraps=real_expand_user_hit_rows,
            ) as expand_rows,
            CaptureQueriesContext(connection) as captured,
        ):
            first_page = self.api.get(
                reverse("user-hits-api"),
                {"page": 1, "page_size": 2},
            )

        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(first_page.data["count"], 3)
        self.assertEqual(len(first_page.data["results"]), 2)
        self.assertEqual(
            [(row["date"], row["user_name"]) for row in first_page.data["results"]],
            [
                (self.today.isoformat(), "Kanik Gupta"),
                (self.today.isoformat(), "Other User"),
            ],
        )
        self.assertEqual(len(expand_rows.call_args.args[0]), 2)
        attempt_queries = [
            query["sql"] for query in captured.captured_queries
            if "surveys_surveyattempt" in query["sql"].lower()
        ]
        self.assertEqual(len(attempt_queries), 1)
        self.assertIn("GROUP BY", attempt_queries[0].upper())

        with CaptureQueriesContext(connection) as cached_queries:
            second_page = self.api.get(
                reverse("user-hits-api"),
                {"page": 2, "page_size": 2},
            )

        self.assertEqual(second_page.status_code, 200)
        self.assertEqual(second_page.data["count"], 3)
        self.assertEqual(
            [(row["date"], row["user_name"]) for row in second_page.data["results"]],
            [((self.today - timedelta(days=1)).isoformat(), "Kanik Gupta")],
        )
        self.assertEqual(first_page.data["summary"], second_page.data["summary"])
        self.assertFalse([
            query for query in cached_queries.captured_queries
            if "surveys_surveyattempt" in query["sql"].lower()
        ])

    def test_mysql_ist_grouping_uses_numeric_offsets_without_timezone_tables(self):
        from .user_hits import _local_date_expression

        queryset = SurveyAttempt.objects.all()
        with patch.object(connection, "vendor", "mysql"):
            expression = _local_date_expression(queryset)
            sql = str(queryset.annotate(local_date=expression).values("local_date").query)

        self.assertIn("CONVERT_TZ", sql)
        self.assertIn("'+00:00'", sql)
        self.assertIn("'+05:30'", sql)
        self.assertNotIn("'UTC'", sql)

    def test_legacy_numeric_user_snapshot_is_included_without_platform_fk(self):
        SurveyAttempt.objects.create(
            rid="Lg1Aa2Bb3C",
            survey=self.survey,
            platform_user=None,
            user_id=str(self.kanik.pk),
            status=SurveyAttempt.Status.COMPLETED,
            entry_device="Desktop",
            initiated_at=timezone.make_aware(datetime.combine(self.today, time(11, 0))),
        )

        response = self.api.get(reverse("user-hits-api"), {
            "user": self.kanik.pk,
            "from_date": self.today.isoformat(),
            "to_date": self.today.isoformat(),
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["hits"]["total"], 3)
        self.assertEqual(response.data["results"][0]["completes"]["total"], 2)

    def test_legacy_employee_id_snapshot_is_included_without_platform_fk(self):
        EmployeeProfile.objects.filter(user=self.kanik).update(employee_id="87821")
        SurveyAttempt.objects.create(
            rid="Le8Ga7Cy6I",
            survey=self.survey,
            platform_user=None,
            user_id="87821",
            status=SurveyAttempt.Status.COMPLETED,
            entry_device="Mobile",
            initiated_at=timezone.make_aware(datetime.combine(self.today, time(11, 30))),
        )

        response = self.api.get(reverse("user-hits-api"), {
            "user": self.kanik.pk,
            "from_date": self.today.isoformat(),
            "to_date": self.today.isoformat(),
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["hits"]["total"], 3)
        self.assertEqual(response.data["results"][0]["hits"]["mobile"], 2)

    def test_role_based_super_admin_sees_all_user_activity(self):
        role_owner = get_user_model().objects.create_user(
            username="hits-role-owner", first_name="Role", last_name="Owner"
        )
        EmployeeProfile.objects.filter(user=role_owner).update(
            role=Role.objects.get(slug="super-admin")
        )
        for code in ("user_hits.view", "user_hits.column.user"):
            UserFunctionOverride.objects.create(
                user=role_owner,
                function=AccessFunction.objects.get(code=code),
                effect=UserFunctionOverride.Effect.ALLOW,
            )
        role_owner_api = APIClient()
        role_owner_api.force_authenticate(role_owner)

        response = role_owner_api.get(reverse("user-hits-api"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {row["user_id"] for row in response.data["results"]},
            {self.kanik.pk, self.other.pk},
        )

    def test_permission_and_visibility_are_scoped_to_user_hierarchy(self):
        viewer = get_user_model().objects.create_user(
            username="hits-viewer", first_name="Scoped", email="hits-viewer@example.test"
        )
        for code in ("user_hits.view", "user_hits.column.user"):
            UserFunctionOverride.objects.create(
                user=viewer,
                function=AccessFunction.objects.get(code=code),
                effect=UserFunctionOverride.Effect.ALLOW,
            )
        SurveyAttempt.objects.create(
            rid="Vh5Ii6Jj7K", survey=self.survey, platform_user=viewer, user_id=str(viewer.pk),
            status=SurveyAttempt.Status.INITIATED, entry_device="Mobile",
        )
        scoped_api = APIClient()
        scoped_api.force_authenticate(viewer)
        response = scoped_api.get(reverse("user-hits-api"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["user_id"], viewer.pk)
        self.assertNotIn("branch", response.data["results"][0])
        self.assertNotIn("hits", response.data["results"][0])
        self.assertEqual(scoped_api.get(reverse("user-hits-api"), {"branch": "Gurgaon"}).status_code, 403)
        self.client.force_login(viewer)
        viewer_page = self.client.get(reverse("user-hits"))
        self.assertNotContains(viewer_page, 'id="hitBranchLabel"')
        self.assertNotContains(viewer_page, "<th>Hits</th>", html=True)

        no_access = get_user_model().objects.create_user(username="hits-no-access")
        denied_api = APIClient()
        denied_api.force_authenticate(no_access)
        self.assertEqual(denied_api.get(reverse("user-hits-api")).status_code, 403)
        self.client.force_login(no_access)
        self.assertEqual(self.client.get(reverse("user-hits")).status_code, 403)


class DashboardAnalyticsTests(TestCase):
    def setUp(self):
        caches["reports"].clear()
        self.owner = get_user_model().objects.create_superuser(
            username="dashboard-owner", email="dashboard-owner@example.test", password="test-password"
        )
        self.employee = get_user_model().objects.create_user(
            username="dashboard-employee", first_name="Dash", last_name="Employee",
            email="dashboard-employee@example.test",
        )
        self.other = get_user_model().objects.create_user(
            username="dashboard-other", first_name="Other", last_name="User",
            email="dashboard-other@example.test",
        )
        self.client_a = Client.objects.create(code="dashboard-a", name="Client Alpha")
        self.client_b = Client.objects.create(code="dashboard-b", name="Client Beta")
        self.survey_a = Survey.objects.create(
            client=self.client_a, source_id=880001, company_name="Client Alpha", name="Alpha survey",
            country="United States", country_code="US", cpi="4.00",
        )
        self.survey_b = Survey.objects.create(
            client=self.client_b, source_id=880002, company_name="Client Beta", name="Beta survey",
            country="Canada", country_code="CA", cpi="2.00",
        )
        self.complete = SurveyAttempt.objects.create(
            rid="Da1Sh2Co3M", survey=self.survey_a, platform_user=self.employee,
            user_id=str(self.employee.pk), status=SurveyAttempt.Status.COMPLETED,
            source_cpi_snapshot="4.00", cpi_currency_snapshot="USD", loi_seconds=120,
            entry_device="Desktop", callback_at=timezone.now(),
        )
        SurveyAttempt.objects.create(
            rid="Da4Sh5Te6R", survey=self.survey_b, platform_user=self.other,
            user_id=str(self.other.pk), status=SurveyAttempt.Status.TERMINATED,
            source_cpi_snapshot="2.00", cpi_currency_snapshot="USD", loi_seconds=60,
            entry_device="Mobile", callback_at=timezone.now(),
        )
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def test_dashboard_page_has_animated_widgets_without_filters_or_activity_feed(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("dashboard"))

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Performance intelligence")
        self.assertContains(page, 'id="volumeChart"')
        self.assertContains(page, 'id="financeChart"')
        self.assertContains(page, 'id="trafficGraphClient"')
        self.assertContains(page, 'id="financeGraphClient"')
        self.assertNotContains(page, 'aria-label="Traffic graph time range"')
        self.assertNotContains(page, 'aria-label="Finance graph time range"')
        self.assertContains(page, 'id="clientShareChart"')
        self.assertContains(page, 'data-dashboard-range="24h"')
        self.assertContains(page, 'data-dashboard-range="48h"')
        self.assertContains(page, 'data-dashboard-range="7d"')
        self.assertContains(page, 'data-dashboard-range="month"')
        self.assertContains(page, 'data-dashboard-range="3m"')
        self.assertContains(page, 'data-dashboard-range="6m"')
        self.assertContains(page, 'id="dashboardFinancialYear"')
        self.assertNotContains(page, 'data-dashboard-range="1y"')
        self.assertNotContains(page, 'data-dashboard-filter="branch"')
        self.assertNotContains(page, "Recent activity")
        self.assertContains(page, 'id="dashboardIR"')
        self.assertNotContains(page, 'id="dashboardActiveUsers"')
        self.assertContains(page, "<small>RPC</small>", html=True)
        self.assertNotContains(page, "Historical hit-time CPI")
        self.assertNotContains(page, 'id="trafficChartInsights"')
        self.assertNotContains(page, 'id="financeChartInsights"')
        self.assertNotContains(page, "Portfolio leader")
        self.assertNotContains(page, "Efficiency signal")

    def test_dashboard_api_returns_overall_kpis_client_share_and_time_series(self):
        response = self.api.get(reverse("dashboard-api"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["range"]["key"], "24h")
        self.assertEqual(response.data["range"]["bucket_label"], "2-hour intervals")
        self.assertEqual(response.data["summary"]["hits"], 2)
        self.assertEqual(response.data["summary"]["completes"], 1)
        self.assertEqual(response.data["summary"]["last_hour_completes"], 1)
        self.assertEqual(response.data["summary"]["conversion_rate"], 50.0)
        self.assertEqual(response.data["summary"]["incidence_rate"], 50.0)
        self.assertEqual(response.data["summary"]["active_users"], 2)
        self.assertEqual(response.data["summary"]["average_loi_seconds"], 90)
        self.assertEqual(str(response.data["summary"]["revenue"]), "4.00")
        self.assertEqual(str(response.data["summary"]["average_cpi"]), "4.00")
        self.assertEqual(str(response.data["summary"]["rpc"]), "2.00")
        self.assertEqual(response.data["status_breakdown"]["terminated"], 1)
        self.assertEqual(response.data["device_breakdown"]["desktop"], 1)
        self.assertEqual(response.data["device_performance"]["desktop"]["hits"], 1)
        self.assertEqual(response.data["device_performance"]["desktop"]["conversion_rate"], 100.0)
        self.assertEqual(response.data["client_distribution"][0]["name"], "Client Alpha")
        self.assertEqual(response.data["client_distribution"][0]["share_percent"], 100.0)
        self.assertEqual(response.data["client_distribution"][0]["hits"], 1)
        self.assertEqual(response.data["client_distribution"][0]["conversion_rate"], 100.0)
        self.assertEqual(len(response.data["traffic_chart"]["points"]), 12)
        self.assertEqual(sum(point["hits"] for point in response.data["traffic_chart"]["points"]), 2)
        self.assertEqual(len(response.data["finance_chart"]["points"]), 12)
        self.assertEqual(
            {item["name"] for item in response.data["graph_clients"]},
            {"Client Alpha", "Client Beta"},
        )
        self.assertEqual(response.data["top_suppliers"][0]["name"], "Direct traffic")
        self.assertEqual(response.data["top_suppliers"][0]["branch_name"], "Unassigned branch")
        self.assertEqual(response.data["top_suppliers"][0]["contribution_percent"], 100.0)
        self.assertIsNotNone(response.data["comparison"])
        self.assertTrue(response.data["financial_years"])
        self.assertNotIn("recent_activity", response.data)

    def test_dashboard_supports_every_global_analytics_range(self):
        local_now = timezone.localtime(timezone.now())
        financial_year = local_now.year if local_now.month >= 4 else local_now.year - 1
        financial_year_months = local_now.month - 3 if local_now.month >= 4 else local_now.month + 9
        expected = {
            "24h": (12, "2-hour intervals"),
            "48h": (12, "4-hour intervals"),
            "7d": (7, "Daily intervals"),
            "month": (local_now.day, "Daily intervals"),
            "3m": (13, "Weekly intervals"),
            "6m": (6, "Monthly intervals"),
            "fy": (financial_year_months, "Monthly intervals"),
        }
        for range_key, (point_count, bucket_label) in expected.items():
            with self.subTest(range_key=range_key):
                params = {"range": range_key}
                if range_key == "fy":
                    params["financial_year"] = financial_year
                response = self.api.get(reverse("dashboard-api"), params)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["range"]["key"], range_key)
                self.assertEqual(response.data["range"]["bucket_label"], bucket_label)
                self.assertEqual(len(response.data["traffic_chart"]["points"]), point_count)
                self.assertEqual(
                    response.data["traffic_chart"]["range"]["bucket_label"], bucket_label
                )
                self.assertEqual(
                    sum(point["hits"] for point in response.data["traffic_chart"]["points"]), 2
                )
                if range_key == "fy":
                    self.assertEqual(response.data["range"]["financial_year"], financial_year)

        invalid = self.api.get(reverse("dashboard-api"), {"range": "forever"})
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("Range must be one of", invalid.data["detail"])

    def test_dashboard_range_excludes_activity_outside_selected_window(self):
        SurveyAttempt.objects.create(
            rid="OldDa5h7Yr", survey=self.survey_a, platform_user=self.employee,
            user_id=str(self.employee.pk), status=SurveyAttempt.Status.COMPLETED,
            source_cpi_snapshot="10.00", cpi_currency_snapshot="USD",
            initiated_at=timezone.now() - timedelta(days=40),
        )

        recent = self.api.get(reverse("dashboard-api"), {"range": "24h"})
        quarterly = self.api.get(reverse("dashboard-api"), {"range": "3m"})

        self.assertEqual(recent.data["summary"]["hits"], 2)
        self.assertEqual(quarterly.data["summary"]["hits"], 3)
        self.assertEqual(quarterly.data["summary"]["completes"], 2)
        self.assertEqual(str(quarterly.data["summary"]["revenue"]), "14.00")

    def test_graph_client_filters_use_the_global_range_without_changing_cards(self):
        response = self.api.get(reverse("dashboard-api"), {
            "range": "24h",
            "traffic_range": "48h",
            "traffic_client": self.client_b.pk,
            "finance_range": "6m",
            "finance_client": self.client_a.pk,
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["range"]["key"], "24h")
        self.assertEqual(response.data["summary"]["hits"], 2)
        self.assertEqual(response.data["traffic_chart"]["range"]["key"], "24h")
        self.assertEqual(response.data["traffic_chart"]["client_id"], self.client_b.pk)
        self.assertEqual(
            sum(point["hits"] for point in response.data["traffic_chart"]["points"]), 1
        )
        self.assertEqual(response.data["finance_chart"]["range"]["key"], "24h")
        self.assertEqual(response.data["finance_chart"]["client_id"], self.client_a.pk)
        self.assertEqual(
            sum(point["completes"] for point in response.data["finance_chart"]["points"]), 1
        )

    def test_current_month_uses_previous_month_to_date_comparison(self):
        response = self.api.get(reverse("dashboard-api"), {"range": "month"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["comparison"]["label"], "Previous month to date")

        fixed_now = datetime(2026, 9, 5, 13, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
        comparison = dashboard_comparison_window(
            dashboard_range_window("month", now=fixed_now)
        )
        self.assertEqual(timezone.localtime(comparison["start"]).date().isoformat(), "2026-08-01")
        self.assertEqual(timezone.localtime(comparison["end"]).date().isoformat(), "2026-08-05")

    def test_top_supplier_uses_supplier_company_and_employee_branch(self):
        branch = OrganizationUnit.objects.create(
            workspace_owner=self.owner,
            unit_type=OrganizationUnit.UnitType.BRANCH,
            name="Noida",
            code="dashboard-noida",
        )
        employee_profile = self.employee.employee_profile
        employee_profile.organization_unit = branch
        employee_profile.save(update_fields=["organization_unit"])
        supplier = get_user_model().objects.create_user(username="dashboard-supplier")
        supplier_profile = supplier.employee_profile
        supplier_profile.account_type = EmployeeProfile.AccountType.EXTERNAL_VENDOR
        supplier_profile.company_name = "Supplier One"
        supplier_profile.save(update_fields=["account_type", "company_name"])
        self.complete.vendor = supplier
        self.complete.save(update_fields=["vendor"])

        response = self.api.get(reverse("dashboard-api"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["top_suppliers"][0]["name"], "Supplier One")
        self.assertEqual(response.data["top_suppliers"][0]["branch_name"], "Noida")

    def test_dashboard_is_unfiltered_for_owner_and_rejects_employee(self):
        filtered = self.api.get(reverse("dashboard-api"), {"client": self.client_b.pk})
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual(filtered.data["summary"]["hits"], 2)
        self.assertEqual(filtered.data["summary"]["completes"], 1)

        scoped = APIClient()
        scoped.force_authenticate(self.employee)
        own = scoped.get(reverse("dashboard-api"))
        self.assertEqual(own.status_code, 403)
        self.assertEqual(scoped.get(reverse("dashboard-api"), {"branch": "1"}).status_code, 403)
        self.assertEqual(scoped.get(reverse("dashboard-api"), {"traffic_range": "48h"}).status_code, 403)
        self.assertEqual(scoped.get(reverse("dashboard-api"), {"finance_range": "48h"}).status_code, 403)

    def test_employee_card_override_cannot_bypass_dashboard_restriction(self):
        UserFunctionOverride.objects.create(
            user=self.employee,
            function=AccessFunction.objects.get(code="dashboard.card.hits"),
            effect=UserFunctionOverride.Effect.DENY,
        )
        scoped = APIClient()
        scoped.force_authenticate(self.employee)

        response = scoped.get(reverse("dashboard-api"))

        self.assertEqual(response.status_code, 403)

    def test_local_prescreener_termination_is_excluded_from_ir(self):
        SurveyAttempt.objects.create(
            rid="LocalPr3Sc", survey=self.survey_a, platform_user=self.employee,
            user_id=str(self.employee.pk), status=SurveyAttempt.Status.TERMINATED,
            status_source="local_prescreener",
        )
        response = self.api.get(reverse("dashboard-api"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["summary"]["incidence_rate"], 50.0)
