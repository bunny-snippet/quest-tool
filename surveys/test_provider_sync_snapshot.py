from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from vendors.models import Client, ClientIntegration

from .models import Survey, SurveyAttempt
from .provider_services import sync_client_integration
from .providers.base import NormalizedSurvey


class SnapshotInventoryProvider:
    close_missing_inventory_items = True

    def __init__(self, integration, source_keys=(), preparation_failures=()):
        self.integration = integration
        self.source_keys = tuple(source_keys)
        self.preparation_failures = set(preparation_failures)

    def inventory(self):
        return [{"source_key": source_key} for source_key in self.source_keys]

    def normalize_inventory_item(self, payload, seen_at):
        source_key = payload["source_key"]
        return NormalizedSurvey(
            source_key=source_key,
            numeric_source_id=None,
            modified_at=seen_at,
            values={
                "name": f"Survey {source_key}",
                "status": Survey.Status.LIVE,
                # The generic sync must replace an adapter-supplied value with
                # the authoritative marker for this inventory snapshot.
                "last_seen_at": seen_at - timedelta(days=30),
                "source_modified_at": seen_at,
                "raw_data": payload,
            },
            raw_data=payload,
        )

    def prepare_inventory_item(self, normalized, existing_survey=None):
        if normalized.source_key in self.preparation_failures:
            raise RuntimeError("preparation failed")
        return normalized


class NonClosingSnapshotInventoryProvider(SnapshotInventoryProvider):
    close_missing_inventory_items = False


class ProviderSnapshotCloseTests(TestCase):
    def setUp(self):
        self.client_record = Client.objects.create(
            code="snapshot-provider",
            name="Snapshot Provider",
            provider_code="custom",
        )
        self.integration = ClientIntegration.objects.create(
            client=self.client_record,
            name="Snapshot Inventory",
            provider_code="custom",
            base_url="https://provider.example.test/",
        )
        self.old_marker = timezone.now() - timedelta(days=1)
        self.snapshot_marker = timezone.now()

    def _survey(self, source_key):
        return Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_key=source_key,
            name=f"Survey {source_key}",
            status=Survey.Status.LIVE,
            last_seen_at=self.old_marker,
        )

    def _sync(self, provider):
        with patch(
            "surveys.provider_services.get_provider",
            return_value=provider,
        ), patch(
            "surveys.provider_services.timezone.now",
            return_value=self.snapshot_marker,
        ):
            return sync_client_integration(self.integration, refresh_details=False)

    def test_closes_missing_rows_with_snapshot_marker_not_source_key_not_in(self):
        current = self._survey("current")
        missing = self._survey("missing")
        provider = SnapshotInventoryProvider(self.integration, source_keys=("current",))

        with CaptureQueriesContext(connection) as queries:
            run = self._sync(provider)

        current.refresh_from_db()
        missing.refresh_from_db()
        self.assertEqual(run.closed, 1)
        self.assertEqual(current.status, Survey.Status.LIVE)
        self.assertEqual(current.last_seen_at, self.snapshot_marker)
        self.assertEqual(missing.status, Survey.Status.CLOSED)

        update_queries = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("UPDATE")
            and "surveys_survey" in query["sql"]
        ]
        marker_close_queries = [
            sql
            for sql in update_queries
            if "last_seen_at" in sql and "status" in sql and " < " in sql
        ]
        self.assertEqual(len(marker_close_queries), 1, update_queries)
        self.assertNotIn("source_key", marker_close_queries[0])
        self.assertFalse(
            any("source_key" in sql and "NOT IN" in sql.upper() for sql in update_queries),
            update_queries,
        )

    def test_preparation_failure_keeps_existing_missing_row_behavior(self):
        current = self._survey("current")
        failed = self._survey("failed")
        provider = SnapshotInventoryProvider(
            self.integration,
            source_keys=("current", "failed"),
            preparation_failures=("failed",),
        )

        run = self._sync(provider)

        current.refresh_from_db()
        failed.refresh_from_db()
        self.assertEqual(run.detail_failures, 1)
        self.assertEqual(run.closed, 1)
        self.assertEqual(run.status, "partial")
        self.assertEqual(current.status, Survey.Status.LIVE)
        self.assertEqual(current.last_seen_at, self.snapshot_marker)
        self.assertEqual(failed.status, Survey.Status.CLOSED)

    def test_provider_that_disables_missing_close_leaves_absent_row_live(self):
        absent = self._survey("absent")
        provider = NonClosingSnapshotInventoryProvider(self.integration, source_keys=())

        run = self._sync(provider)

        absent.refresh_from_db()
        self.assertEqual(run.closed, 0)
        self.assertEqual(absent.status, Survey.Status.LIVE)
        self.assertEqual(absent.last_seen_at, self.old_marker)

    def test_performance_indexes_match_query_shapes(self):
        survey_indexes = {index.name: index for index in Survey._meta.indexes}
        attempt_indexes = {index.name: index for index in SurveyAttempt._meta.indexes}

        self.assertEqual(
            survey_indexes["survey_int_status_seen_idx"].fields,
            ["integration", "status", "last_seen_at"],
        )
        self.assertEqual(
            attempt_indexes["attempt_term_order_idx"].fields,
            ["-callback_at", "-initiated_at", "status"],
        )
