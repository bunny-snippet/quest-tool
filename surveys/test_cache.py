from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache, caches
from django.db import connection
from django.db.models import Value
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from config.cache_utils import (
    jittered_ttl,
    safe_cache_get_or_set,
    stable_cache_key,
)
from surveys.models import Survey
from surveys.project_cache import (
    invalidate_project_cache,
    project_filter_metadata,
    project_filtered_count,
)
from surveys.report_cache import cached_report_payload


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

    def test_filter_values_get_independent_cache_entries(self):
        calls = []

        def load():
            calls.append(True)
            return len(calls)

        us = cached_report_payload("test-summary", self.request("country=US"), load)
        ca = cached_report_payload("test-summary", self.request("country=CA"), load)

        self.assertEqual((us, ca), (1, 2))
