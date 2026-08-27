from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import caches
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from prescreener_vault.reuse import (
    _calendar_bounds,
    profile_reuse_month_status,
    profile_reuse_month_statuses,
)
from surveys.models import ProfileReuseMonthlyCounter, Survey, SurveyAttempt

from .models import Client, ClientIntegration


class ClientIntegrationListPerformanceTests(TestCase):
    def setUp(self):
        caches["reports"].clear()
        self.addCleanup(caches["reports"].clear)
        ClientIntegration.objects.all().delete()
        self.user = get_user_model().objects.create_superuser(
            username="integration-performance-owner",
            password="test-password",
        )
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.integrations = []
        self.surveys = []
        for index in range(12):
            client = Client.objects.create(
                code=f"integration-performance-{index}",
                name=f"Integration performance {index}",
                provider_code="custom",
            )
            integration = ClientIntegration.objects.create(
                client=client,
                name=f"Connection {index}",
                provider_code="custom",
                base_url="https://example.test",
                sync_interval_seconds=60,
            )
            survey = Survey.objects.create(
                integration=integration,
                client=client,
                source_id=970000 + index,
                country_code="US" if index % 2 else "GB",
                country="United States" if index % 2 else "United Kingdom",
            )
            self.integrations.append(integration)
            self.surveys.append(survey)

    def _captured_list(self, **parameters):
        caches["reports"].clear()
        with CaptureQueriesContext(connection) as captured:
            response = self.api.get(
                "/api/v1/vendors/integrations/",
                {"page_size": 200, **parameters},
            )
        self.assertEqual(response.status_code, 200)
        return response, list(captured.captured_queries)

    def test_card_metadata_query_count_is_constant_across_page_size(self):
        # Prime request-independent permission snapshots so both captures
        # measure only the integration endpoint's own query shape.
        self.api.get(
            "/api/v1/vendors/integrations/",
            {"page_size": 1},
        )
        single, single_queries = self._captured_list(page_size=1)
        multiple, multiple_queries = self._captured_list(page_size=200)

        self.assertEqual(len(single.data["results"]), 1)
        self.assertEqual(len(multiple.data["results"]), 12)
        self.assertEqual(len(single_queries), len(multiple_queries))
        self.assertLessEqual(len(multiple_queries), 7)
        self.assertEqual(sum(
            "surveys_surveyattempt" in query["sql"].lower()
            for query in multiple_queries
        ), 1)
        self.assertEqual(sum(
            "profilereusemonthlycounter" in query["sql"].lower()
            for query in multiple_queries
        ), 1)

    def test_nested_client_integrations_reuse_the_same_fixed_card_queries(self):
        # Warm only permission snapshots, then force the card payload cold.
        self.api.get(
            "/api/v1/vendors/clients/",
            {"page_size": 1, "provider_code": "custom"},
        )
        caches["reports"].clear()
        with CaptureQueriesContext(connection) as captured:
            response = self.api.get(
                "/api/v1/vendors/clients/",
                {"page_size": 200, "provider_code": "custom"},
            )

        self.assertEqual(response.status_code, 200)
        rows = response.data["results"]
        self.assertEqual(len(rows), 12)
        self.assertTrue(all(len(row["integrations"]) == 1 for row in rows))
        self.assertTrue(all(
            row["integrations"][0]["survey_count"] == 1 for row in rows
        ))
        self.assertLessEqual(len(captured), 8)
        self.assertEqual(sum(
            "surveys_surveyattempt" in query["sql"].lower()
            for query in captured.captured_queries
        ), 1)
        self.assertEqual(sum(
            "profilereusemonthlycounter" in query["sql"].lower()
            for query in captured.captured_queries
        ), 1)

    def test_set_based_reuse_status_matches_single_card_contract(self):
        reference = timezone.now()
        previous_start, current_start, period_start = _calendar_bounds(reference)
        previous_time = previous_start + timedelta(days=1)
        current_time = current_start + timedelta(days=1)
        SurveyAttempt.objects.bulk_create([
            SurveyAttempt(
                rid=f"P{index:09d}",
                survey=self.surveys[0],
                initiated_at=previous_time,
            )
            for index in range(2)
        ] + [
            SurveyAttempt(
                rid=f"C{index:09d}",
                survey=self.surveys[1],
                initiated_at=current_time,
            )
            for index in range(3)
        ])
        ProfileReuseMonthlyCounter.objects.create(
            integration=self.integrations[0],
            period_start=period_start,
            baseline_attempts=9,
            allocated_reuses=1,
            first_reuse_allocated=1,
        )
        ProfileReuseMonthlyCounter.objects.create(
            integration=self.integrations[1],
            period_start=period_start,
            baseline_attempts=5,
            allocated_reuses=2,
            first_reuse_allocated=1,
            repeat_reuse_allocated=1,
        )

        batched = profile_reuse_month_statuses(
            self.integrations, reference=reference
        )

        self.assertEqual(
            batched,
            {
                integration.pk: profile_reuse_month_status(
                    integration, reference=reference
                )
                for integration in self.integrations
            },
        )
