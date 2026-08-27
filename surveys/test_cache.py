from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache, caches
from django.db import connection
from django.db.models import Value
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from accounts.access import (
    activity_visibility_cache_generation,
    activity_visible_user_ids,
)
from config.cache_utils import (
    jittered_ttl,
    safe_cache_compare_delete,
    safe_cache_generation,
    safe_cache_get_or_set,
    stable_cache_key,
)
from surveys.models import Survey, SurveyAttempt
from surveys.project_cache import (
    invalidate_project_cache,
    project_filter_metadata,
    project_filtered_count,
)
from surveys.report_cache import (
    cached_report_payload,
    report_metadata_generation,
    report_viewer_scope,
)


@override_settings(CACHE_DEFAULT_TTL_SECONDS=100, CACHE_TTL_JITTER_SECONDS=20)
class CacheUtilityTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_jittered_ttl_stays_inside_configured_range(self):
        values = {jittered_ttl() for _ in range(100)}
        self.assertTrue(all(80 <= value <= 120 for value in values))
        self.assertGreater(len(values), 1)

    def test_stable_key_hides_filter_values_and_ignores_dict_order(self):
        first = stable_cache_key("vault", {"country": "US", "gender": "male"})
        second = stable_cache_key("vault", {"gender": "male", "country": "US"})
        self.assertEqual(first, second)
        self.assertNotIn("male", first)
        self.assertNotIn("US", first)

    def test_get_or_set_loads_once(self):
        calls = []

        def factory():
            calls.append(True)
            return {"value": 1}

        first = safe_cache_get_or_set("test:get-or-set", factory)
        second = safe_cache_get_or_set("test:get-or-set", factory)
        self.assertEqual(first, {"value": 1})
        self.assertEqual(second, {"value": 1})
        self.assertEqual(len(calls), 1)

    def test_generation_key_eviction_never_reuses_literal_namespace(self):
        key = "test:authorization-generation"
        first = safe_cache_generation(key)
        cache.delete(key)
        second = safe_cache_generation(key)

        self.assertNotEqual(first, second)
        self.assertNotEqual(first, 1)
        self.assertNotEqual(second, 1)

    def test_compare_delete_never_removes_a_replacement_lock(self):
        cache.set("test:lock", 222, timeout=30)
        self.assertFalse(safe_cache_compare_delete("test:lock", 111))
        self.assertEqual(cache.get("test:lock"), 222)
        self.assertTrue(safe_cache_compare_delete("test:lock", 222))
        self.assertIsNone(cache.get("test:lock"))

    @patch("config.cache_utils._cache")
    def test_cache_outage_falls_back_to_factory(self, cache_lookup):
        backend = cache_lookup.return_value
        backend.get.side_effect = ConnectionError("redis unavailable")
        backend.set.side_effect = ConnectionError("redis unavailable")
        self.assertEqual(
            safe_cache_get_or_set("test:outage", lambda: {"from": "database"}),
            {"from": "database"},
        )
        backend.get.assert_called_once()
        backend.set.assert_called_once()


@override_settings(
    PROJECT_CACHE_FILTERS_TTL_SECONDS=600,
    PROJECT_CACHE_COUNT_TTL_SECONDS=90,
    PROJECT_CACHE_TTL_JITTER_SECONDS=0,
)
class ProjectCacheTests(TestCase):
    def setUp(self):
        caches["projects"].clear()
        self.user = get_user_model().objects.create_user(
            username="project-cache-user",
            password="test-password",
        )
        Survey.objects.create(
            source_id=101,
            company_name="InnovateMR",
            country="United States",
            country_code="US",
            buyer_id="buyer-a",
            survey_type="B2C",
            cpi="2.50",
        )

    def test_filter_metadata_is_cached_then_version_invalidated(self):
        first = project_filter_metadata(
            Survey.objects.all(),
            user_id=self.user.pk,
            client_scoped=False,
            include_cpi=True,
        )
        self.assertEqual(first["countries"], [("US", "United States")])

        Survey.objects.create(
            source_id=102,
            company_name="Cint Exchange",
            country="Canada",
            country_code="CA",
            cpi="4.00",
        )
        cached = project_filter_metadata(
            Survey.objects.all(),
            user_id=self.user.pk,
            client_scoped=False,
            include_cpi=True,
        )
        self.assertEqual(cached["countries"], [("US", "United States")])

        invalidate_project_cache()
        refreshed = project_filter_metadata(
            Survey.objects.all(),
            user_id=self.user.pk,
            client_scoped=False,
            include_cpi=True,
        )
        self.assertEqual([row[0] for row in refreshed["countries"]], ["CA", "US"])
        self.assertEqual(str(refreshed["cpi_max"]), "4")

    def test_high_frequency_invalidations_are_throttled(self):
        self.assertTrue(invalidate_project_cache(throttle_seconds=30))
        filter_version = caches["projects"].get("projects:filters-version")
        count_version = caches["projects"].get("projects:count-version")
        self.assertFalse(invalidate_project_cache(throttle_seconds=30))
        self.assertEqual(
            caches["projects"].get("projects:filters-version"), filter_version
        )
        self.assertEqual(
            caches["projects"].get("projects:count-version"), count_version
        )

    def test_filter_and_count_versions_can_be_invalidated_independently(self):
        first_filters = project_filter_metadata(
            Survey.objects.all(),
            user_id=self.user.pk,
            client_scoped=False,
            include_cpi=True,
        )
        request = Request(APIRequestFactory().get("/api/v1/surveys/"))
        request.user = self.user
        self.assertEqual(project_filtered_count(request, Survey.objects.all()), 1)

        Survey.objects.create(
            source_id=104,
            company_name="RFG",
            country="India",
            country_code="IN",
        )
        invalidate_project_cache(filters=False, counts=True)

        cached_filters = project_filter_metadata(
            Survey.objects.all(),
            user_id=self.user.pk,
            client_scoped=False,
            include_cpi=True,
        )
        self.assertEqual(cached_filters, first_filters)
        self.assertEqual(project_filtered_count(request, Survey.objects.all()), 2)

    def test_count_cache_does_not_cache_project_rows(self):
        request = Request(APIRequestFactory().get("/api/v1/surveys/?country=US"))
        request.user = self.user
        queryset = Survey.objects.filter(country_code="US")
        self.assertEqual(project_filtered_count(request, queryset), 1)

        second = Survey.objects.create(
            source_id=103,
            company_name="InnovateMR",
            country="United States",
            country_code="US",
        )
        self.assertEqual(project_filtered_count(request, queryset), 1)
        self.assertTrue(Survey.objects.filter(pk=second.pk).exists())
        invalidate_project_cache()
        self.assertEqual(project_filtered_count(request, queryset), 2)

    def test_filtered_count_selects_only_distinct_project_ids(self):
        request = Request(APIRequestFactory().get("/api/v1/surveys/?country=US"))
        request.user = self.user
        queryset = (
            Survey.objects.filter(country_code="US")
            .annotate(expensive_list_annotation=Value("not-needed-for-count"))
            .order_by("-source_modified_at", "-created_at")
            .distinct()
        )

        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(project_filtered_count(request, queryset), 1)

        count_sql = captured.captured_queries[-1]["sql"]
        self.assertIn("COUNT", count_sql.upper())
        self.assertIn("surveys_survey", count_sql)
        self.assertNotIn("local_id", count_sql)
        self.assertNotIn("expensive_list_annotation", count_sql)


@override_settings(
    REPORT_CACHE_RESULT_TTL_SECONDS=30,
    REPORT_CACHE_TTL_JITTER_SECONDS=0,
)
class ReportCacheTests(TestCase):
    def setUp(self):
        caches["reports"].clear()
        self.user = get_user_model().objects.create_superuser(
            username="report-cache-owner",
            password="test-password",
        )
        self.factory = APIRequestFactory()

    def request(self, query):
        request = Request(self.factory.get(f"/api/v1/attempts/?{query}"))
        request.user = self.user
        return request

    def test_page_navigation_reuses_permission_scoped_aggregate(self):
        calls = []

        def load():
            calls.append(True)
            return {"total": 42}

        first = cached_report_payload(
            "test-summary", self.request("country=US&page=1"), load
        )
        second = cached_report_payload(
            "test-summary", self.request("country=US&page=8"), load
        )

        self.assertEqual(first, {"total": 42})
        self.assertEqual(second, first)
        self.assertEqual(len(calls), 1)

    @patch("surveys.report_cache.secrets.randbits", return_value=912345)
    @patch("surveys.report_cache.safe_cache_add", return_value=True)
    @patch("surveys.report_cache.safe_cache_compare_delete")
    def test_refresh_lock_is_released_only_with_its_owner_token(
        self,
        compare_delete,
        _cache_add,
        _randbits,
    ):
        result = cached_report_payload(
            "token-owned-lock",
            self.request("country=US"),
            lambda: {"total": 1},
        )

        self.assertEqual(result, {"total": 1})
        compare_delete.assert_called_once()
        _lock_key, lock_token = compare_delete.call_args.args
        self.assertEqual(lock_token, 912345)
        self.assertEqual(compare_delete.call_args.kwargs, {"alias": "reports"})

    def test_filter_values_get_independent_cache_entries(self):
        calls = []

        def load():
            calls.append(True)
            return len(calls)

        us = cached_report_payload("test-summary", self.request("country=US"), load)
        ca = cached_report_payload("test-summary", self.request("country=CA"), load)

        self.assertEqual((us, ca), (1, 2))

    def test_include_summary_does_not_split_identical_aggregate_keys(self):
        calls = []

        def load():
            calls.append(True)
            return {"total": 7}

        rows_request = self.request("country=US&include_summary=false")
        summary_request = self.request("country=US")

        self.assertEqual(
            cached_report_payload("same-traffic-summary", rows_request, load),
            {"total": 7},
        )
        self.assertEqual(
            cached_report_payload("same-traffic-summary", summary_request, load),
            {"total": 7},
        )
        self.assertEqual(len(calls), 1)

    def test_live_inventory_and_attempt_writes_do_not_rotate_filter_generation(self):
        generation = report_metadata_generation()
        survey = Survey.objects.create(
            source_id=880011,
            country="United States",
            country_code="US",
        )
        SurveyAttempt.objects.create(
            rid="Ca1Ch2Ur3N",
            survey=survey,
            platform_user=self.user,
            user_id=str(self.user.pk),
        )

        self.assertEqual(report_metadata_generation(), generation)

    def test_viewer_scope_fingerprint_changes_even_when_generation_is_unchanged(self):
        with patch(
            "surveys.report_cache.permission_cache_generation",
            return_value=7,
        ), patch(
            "surveys.report_cache.activity_visibility_cache_generation",
            return_value=9,
        ), patch(
            "surveys.report_cache.effective_permission_codes",
            side_effect=[{"attempts.view"}, {"attempts.view", "studies.card.revenue"}],
        ), patch(
            "surveys.report_cache.activity_visible_user_ids",
            return_value={self.user.pk},
        ):
            before = report_viewer_scope(self.user)
            after = report_viewer_scope(self.user)

        self.assertEqual(before["permission_generation"], after["permission_generation"])
        self.assertNotEqual(
            before["permission_fingerprint"],
            after["permission_fingerprint"],
        )


class ActivityVisibilityCacheTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create_superuser(
            username="visibility-cache-owner",
            password="test-password",
        )
        self.employee = get_user_model().objects.create_user(
            username="visibility-cache-employee",
        )

    def test_shared_snapshot_avoids_repeat_user_scan_and_invalidates_on_profile_change(self):
        with CaptureQueriesContext(connection) as cold_queries:
            first = activity_visible_user_ids(self.owner)
        with CaptureQueriesContext(connection) as warm_queries:
            second = activity_visible_user_ids(self.owner)

        self.assertEqual(first, second)
        self.assertIn(self.employee.pk, first)
        self.assertTrue(cold_queries.captured_queries)
        self.assertEqual(len(warm_queries), 0)

        added = get_user_model().objects.create_user(
            username="visibility-cache-added",
        )
        with CaptureQueriesContext(connection) as refreshed_queries:
            refreshed = activity_visible_user_ids(self.owner)

        self.assertIn(added.pk, refreshed)
        self.assertTrue(refreshed_queries.captured_queries)

    def test_visibility_generation_rotates_again_after_atomic_commit(self):
        profile = self.employee.employee_profile
        initial = activity_visibility_cache_generation()

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            profile.department = "New department"
            profile.save(update_fields=["department", "updated_at"])
            inside_transaction = activity_visibility_cache_generation()

        committed = activity_visibility_cache_generation()
        self.assertTrue(callbacks)
        self.assertGreater(inside_transaction, initial)
        self.assertGreater(committed, inside_transaction)
