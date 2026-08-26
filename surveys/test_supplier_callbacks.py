"""Regression coverage for durable external-supplier result callbacks."""

from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from vendors.models import Client, ClientIntegration, VendorAPIKey

from .models import Survey, SurveyAttempt
from .supplier_callbacks import (
    DELIVERY_AUDIT_KEY,
    SupplierCallbackRetryableError,
    _send_pinned_supplier_callback,
    deliver_supplier_result_callback,
    queue_supplier_result_callback,
)
from .tasks import dispatch_pending_supplier_callbacks_task
from .views import _finish_local_rfg_attempt


class SupplierCallbackDeliveryTests(TestCase):
    trusted_ip = "15.222.163.99"

    def setUp(self):
        self.owner = get_user_model().objects.create_user(
            username="callback-supplier",
            email="callback-supplier@example.com",
        )
        self.client_record = Client.objects.create(
            code="supplier-callback-rfg",
            name="Supplier Callback RFG",
            provider_code="rfg",
        )
        self.integration = ClientIntegration.objects.create(
            client=self.client_record,
            name="Supplier Callback RFG",
            provider_code="rfg",
            base_url="https://api.researchforgood.com/API/",
            config={
                "callback_security_mode": "ip",
                "callback_ip_allowlist": [self.trusted_ip],
            },
        )
        self.survey = Survey.objects.create(
            client=self.client_record,
            integration=self.integration,
            source_key="RFG-SUPPLIER-CALLBACK",
            local_id="20260899112233",
            company_name="RFG",
            status=Survey.Status.LIVE,
        )
        self.api_key = VendorAPIKey.objects.create(
            vendor=self.owner,
            name="Callback delivery key",
            prefix="qst_callback",
            last_four="test",
            key_hash="a" * 64,
            complete_callback_url="https://supplier.example/complete",
            terminate_callback_url="https://supplier.example/terminate",
            quota_callback_url="https://supplier.example/quota",
            quality_callback_url="https://supplier.example/quality",
        )

    def make_attempt(self, rid, status=SurveyAttempt.Status.REDIRECTED):
        return SurveyAttempt.objects.create(
            rid=rid,
            survey=self.survey,
            user_id="external-panelist",
            status=status,
            supplier_api_key_id=self.api_key.pk,
            supplier_delivery_config={"survey_id": self.survey.local_id},
            upstream_transaction_data={"existing_audit": {"safe": True}},
        )

    @staticmethod
    def public_dns_result(*_args, **_kwargs):
        return [(2, 1, 6, "", ("93.184.216.34", 443))]

    @patch("surveys.tasks.deliver_supplier_result_callback_task.delay")
    def test_verified_rfg_complete_queues_once_and_replay_does_not_duplicate(self, delay):
        attempt = self.make_attempt("Rf1Su2Pp3L")

        with self.captureOnCommitCallbacks(execute=True):
            accepted = self.client.get(reverse("rfg-callback"), {
                "tid": attempt.rid,
                "result": "1",
            }, REMOTE_ADDR=self.trusted_ip)
        self.assertEqual(accepted.status_code, 200)
        attempt.refresh_from_db()
        event_id = f"{attempt.pid}-1"
        self.assertEqual(
            attempt.upstream_transaction_data[DELIVERY_AUDIT_KEY]["event_id"],
            event_id,
        )
        self.assertEqual(
            attempt.upstream_transaction_data[DELIVERY_AUDIT_KEY]["state"],
            "queued",
        )
        self.assertTrue(attempt.upstream_transaction_data["existing_audit"]["safe"])
        delay.assert_called_once_with(attempt.pk, event_id)

        with self.captureOnCommitCallbacks(execute=True):
            replay = self.client.get(reverse("rfg-callback"), {
                "tid": attempt.rid,
                "result": "1",
            }, REMOTE_ADDR=self.trusted_ip)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["idempotent"])
        delay.assert_called_once()

        with self.captureOnCommitCallbacks(execute=True):
            conflict = self.client.get(reverse("rfg-callback"), {
                "tid": attempt.rid,
                "result": "10",
            }, REMOTE_ADDR=self.trusted_ip)
        self.assertEqual(conflict.status_code, 409)
        delay.assert_called_once()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.COMPLETED)
        self.assertEqual(attempt.callback_count, 1)

    @patch("surveys.tasks.deliver_supplier_result_callback_task.delay")
    def test_local_rfg_reject_queues_once_after_finalization(self, delay):
        attempt = self.make_attempt(
            "Lo1Ca2Lr3J",
            status=SurveyAttempt.Status.INITIATED,
        )
        request = RequestFactory().post("/survey/start", REMOTE_ADDR="203.0.113.9")

        with self.captureOnCommitCallbacks(execute=True):
            result = _finish_local_rfg_attempt(
                attempt,
                {"question": {"values": ["outside target"]}},
                request,
                result="7",
                reason="Outside provider qualification",
            )
        self.assertEqual(result.status, SurveyAttempt.Status.TERMINATED)
        result.refresh_from_db()
        event_id = f"{result.pid}-2"
        self.assertEqual(
            result.upstream_transaction_data[DELIVERY_AUDIT_KEY]["event_id"],
            event_id,
        )
        self.assertIn("rfg_local_outcome", result.upstream_transaction_data)
        delay.assert_called_once_with(result.pk, event_id)

        with self.captureOnCommitCallbacks(execute=True):
            _finish_local_rfg_attempt(
                result,
                {},
                request,
                result="7",
                reason="Replay",
            )
        delay.assert_called_once()

    @patch("surveys.supplier_callbacks._send_pinned_supplier_callback", return_value=204)
    @patch("surveys.tasks.deliver_supplier_result_callback_task.delay")
    def test_worker_delivers_pid_only_and_persists_success(
        self,
        delay,
        send,
    ):
        attempt = self.make_attempt(
            "De1Li2Ve3R",
            status=SurveyAttempt.Status.COMPLETED,
        )
        with self.captureOnCommitCallbacks(execute=True):
            self.assertTrue(queue_supplier_result_callback(attempt))
        event_id = f"{attempt.pid}-1"

        result = deliver_supplier_result_callback(attempt.pk, event_id)

        self.assertEqual(result, {"status": "delivered", "http_status": 204})
        send.assert_called_once()
        callback_url = send.call_args.args[0]
        params = parse_qs(urlsplit(callback_url).query)
        self.assertEqual(params["pid"], [attempt.pid])
        self.assertEqual(params["eventId"], [event_id])
        self.assertEqual(params["status"], ["1"])
        self.assertNotIn("rid", params)
        self.assertEqual(send.call_args.args[1], event_id)
        attempt.refresh_from_db()
        record = attempt.upstream_transaction_data[DELIVERY_AUDIT_KEY]
        self.assertEqual(record["state"], "delivered")
        self.assertEqual(record["http_status"], 204)
        self.assertEqual(record["attempt_count"], 1)
        self.assertTrue(record["delivered_at"])
        self.assertNotIn("supplier.example", str(record))
        self.assertTrue(attempt.upstream_transaction_data["existing_audit"]["safe"])

    @patch("surveys.supplier_callbacks._send_pinned_supplier_callback")
    @patch("surveys.tasks.deliver_supplier_result_callback_task.delay")
    def test_retryable_failure_can_run_again_and_succeed(
        self,
        delay,
        send,
    ):
        send.side_effect = [
            SupplierCallbackRetryableError("secret-bearing connection detail"),
            200,
        ]
        attempt = self.make_attempt(
            "Re1Tr2Ya3B",
            status=SurveyAttempt.Status.TERMINATED,
        )
        with self.captureOnCommitCallbacks(execute=True):
            queue_supplier_result_callback(attempt)
        event_id = f"{attempt.pid}-2"

        with self.assertRaises(SupplierCallbackRetryableError):
            deliver_supplier_result_callback(attempt.pk, event_id)
        attempt.refresh_from_db()
        failed = attempt.upstream_transaction_data[DELIVERY_AUDIT_KEY]
        self.assertEqual(failed["state"], "failed")
        self.assertNotIn("secret-bearing", failed["last_error"])

        delivered = deliver_supplier_result_callback(attempt.pk, event_id)
        self.assertEqual(delivered["status"], "delivered")
        attempt.refresh_from_db()
        record = attempt.upstream_transaction_data[DELIVERY_AUDIT_KEY]
        self.assertEqual(record["attempt_count"], 2)
        self.assertEqual(record["state"], "delivered")

    @patch("surveys.supplier_callbacks._PinnedHTTPSConnection")
    @patch("surveys.tasks.deliver_supplier_result_callback_task.delay")
    def test_private_destination_is_rejected_without_network_request(self, delay, connection):
        self.api_key.quality_callback_url = "https://127.0.0.1/private"
        self.api_key.save(update_fields=["quality_callback_url", "updated_at"])
        attempt = self.make_attempt(
            "Ss1Rf2Gu3D",
            status=SurveyAttempt.Status.QUALITY_TERMINATED,
        )
        with self.captureOnCommitCallbacks(execute=True):
            queue_supplier_result_callback(attempt)
        event_id = f"{attempt.pid}-4"

        result = deliver_supplier_result_callback(attempt.pk, event_id)

        self.assertEqual(result["reason"], "unsafe destination")
        connection.assert_not_called()
        attempt.refresh_from_db()
        record = attempt.upstream_transaction_data[DELIVERY_AUDIT_KEY]
        self.assertEqual(record["state"], "failed")
        self.assertNotIn("127.0.0.1", str(record))

    @patch("surveys.supplier_callbacks._send_pinned_supplier_callback", return_value=302)
    @patch("surveys.tasks.deliver_supplier_result_callback_task.delay")
    def test_redirect_is_not_followed_and_is_terminal_failure(
        self,
        delay,
        send,
    ):
        attempt = self.make_attempt(
            "No1Re2Di3R",
            status=SurveyAttempt.Status.OVER_QUOTA,
        )
        with self.captureOnCommitCallbacks(execute=True):
            queue_supplier_result_callback(attempt)
        event_id = f"{attempt.pid}-3"

        result = deliver_supplier_result_callback(attempt.pk, event_id)

        self.assertEqual(result, {"status": "failed", "http_status": 302})
        self.assertEqual(send.call_count, 1)
        attempt.refresh_from_db()
        self.assertEqual(
            attempt.upstream_transaction_data[DELIVERY_AUDIT_KEY]["state"],
            "failed",
        )

    @patch("surveys.supplier_callbacks._PinnedHTTPSConnection")
    @patch("surveys.supplier_callbacks.socket.getaddrinfo")
    def test_connection_is_pinned_to_the_validated_public_dns_answer(
        self,
        getaddrinfo,
        connection_class,
    ):
        getaddrinfo.side_effect = self.public_dns_result
        connection = connection_class.return_value
        connection.getresponse.return_value = Mock(status=202)
        callback_url = "https://supplier.example/result?eventId=public-event"

        status_code = _send_pinned_supplier_callback(
            callback_url,
            "public-event",
        )

        self.assertEqual(status_code, 202)
        getaddrinfo.assert_called_once()
        connection_class.assert_called_once_with(
            "supplier.example",
            "93.184.216.34",
            443,
            connect_timeout=3.0,
            read_timeout=7.0,
        )
        connection.request.assert_called_once()
        self.assertEqual(
            connection.request.call_args.args[:2],
            ("GET", "/result?eventId=public-event"),
        )
        connection.close.assert_called_once()

    @patch("surveys.supplier_callbacks._send_pinned_supplier_callback", return_value=200)
    @patch("surveys.tasks.deliver_supplier_result_callback_task.delay")
    def test_stale_worker_claim_is_reclaimed(self, delay, send):
        attempt = self.make_attempt(
            "St1Al2Ec3L",
            status=SurveyAttempt.Status.COMPLETED,
        )
        with self.captureOnCommitCallbacks(execute=True):
            queue_supplier_result_callback(attempt)
        attempt.refresh_from_db()
        record = attempt.upstream_transaction_data[DELIVERY_AUDIT_KEY]
        record.update({
            "state": "delivering",
            "last_attempt_at": (timezone.now() - timedelta(minutes=2)).isoformat(),
            "updated_at": (timezone.now() - timedelta(minutes=2)).isoformat(),
        })
        attempt.save(update_fields=["upstream_transaction_data", "updated_at"])
        event_id = record["event_id"]

        result = deliver_supplier_result_callback(attempt.pk, event_id)

        self.assertEqual(result["status"], "delivered")
        send.assert_called_once()
        attempt.refresh_from_db()
        self.assertEqual(
            attempt.upstream_transaction_data[DELIVERY_AUDIT_KEY]["state"],
            "delivered",
        )

    @patch("surveys.tasks.deliver_supplier_result_callback_task.delay")
    def test_local_queue_failure_recovers_on_idempotent_finalizer_replay(self, delay):
        delay.side_effect = ConnectionError("broker URL with credentials")
        attempt = self.make_attempt(
            "Br1Ok2Er3F",
            status=SurveyAttempt.Status.INITIATED,
        )
        request = RequestFactory().post("/survey/start", REMOTE_ADDR="203.0.113.9")
        with self.captureOnCommitCallbacks(execute=True):
            result = _finish_local_rfg_attempt(
                attempt,
                {},
                request,
                result="7",
                reason="Outside provider qualification",
            )
        result.refresh_from_db()
        self.assertEqual(
            result.upstream_transaction_data[DELIVERY_AUDIT_KEY]["state"],
            "queue_failed",
        )
        self.assertNotIn(
            "credentials",
            result.upstream_transaction_data[DELIVERY_AUDIT_KEY]["last_error"],
        )

        delay.side_effect = None
        with self.captureOnCommitCallbacks(execute=True):
            _finish_local_rfg_attempt(
                result,
                {},
                request,
                result="7",
                reason="Replay",
            )
        result.refresh_from_db()
        self.assertEqual(delay.call_count, 2)
        self.assertEqual(
            result.upstream_transaction_data[DELIVERY_AUDIT_KEY]["state"],
            "queued",
        )

    @patch("surveys.tasks.deliver_supplier_result_callback_task.delay")
    def test_periodic_dispatch_recovers_queue_failure_without_browser_reload(self, delay):
        delay.side_effect = ConnectionError("broker down")
        attempt = self.make_attempt(
            "Be1At2Rc3V",
            status=SurveyAttempt.Status.TERMINATED,
        )
        attempt.callback_at = timezone.now()
        attempt.last_callback_at = attempt.callback_at
        attempt.save(update_fields=["callback_at", "last_callback_at", "updated_at"])
        with self.captureOnCommitCallbacks(execute=True):
            queue_supplier_result_callback(attempt)
        attempt.refresh_from_db()
        self.assertEqual(
            attempt.upstream_transaction_data[DELIVERY_AUDIT_KEY]["state"],
            "queue_failed",
        )

        delay.side_effect = None
        recovered = dispatch_pending_supplier_callbacks_task()

        self.assertEqual(recovered["queued"], [attempt.pk])
        self.assertEqual(delay.call_count, 2)
