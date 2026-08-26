"""Regression tests for immutable provider result callbacks."""

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from vendors.models import Client, ClientIntegration

from .models import Survey, SurveyAttempt


class TerminalCallbackSecurityTests(TestCase):
    def setUp(self):
        client = Client.objects.create(
            code="callback-security-custom",
            name="Callback security custom provider",
            provider_code="custom",
        )
        self.integration = ClientIntegration.objects.create(
            client=client,
            name="Callback security custom integration",
            provider_code="custom",
            base_url="https://provider.example/api",
        )
        self.survey = Survey.objects.create(
            source_id=991001,
            source_key="991001",
            client=client,
            integration=self.integration,
            company_name=client.name,
            status=Survey.Status.LIVE,
        )

    def make_attempt(self, rid, *, attempt_status=SurveyAttempt.Status.REDIRECTED, final=False):
        now = timezone.now()
        return SurveyAttempt.objects.create(
            rid=rid,
            survey=self.survey,
            user_id="security-test-user",
            status=attempt_status,
            callback_at=now if final else None,
            last_callback_at=now if final else None,
            callback_count=1 if final else 0,
            status_source="provider_callback" if final else "",
        )

    def test_clean_pid_url_is_display_only_for_pending_attempt(self):
        attempt = self.make_attempt("Pe1Nd2In3G")

        response = self.client.get(reverse("survey-status"), {
            "status": "1",
            "pid": attempt.pid,
        })

        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "Invalid survey callback", status_code=409)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.REDIRECTED)
        self.assertIsNone(attempt.callback_at)
        self.assertEqual(attempt.callback_count, 0)

    @patch("surveys.views._external_supplier_result_url")
    def test_s4_cannot_be_replaced_or_forwarded_as_s1(self, supplier_result_url):
        attempt = self.make_attempt(
            "Fi4Na5Ls6T",
            attempt_status=SurveyAttempt.Status.QUALITY_TERMINATED,
            final=True,
        )

        response = self.client.get(reverse("survey-status"), {
            "status": "1",
            "rid": attempt.rid,
        })

        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "Invalid survey callback", status_code=409)
        self.assertNotContains(response, "Thank you for participating", status_code=409)
        supplier_result_url.assert_not_called()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.QUALITY_TERMINATED)
        self.assertEqual(attempt.callback_count, 1)

        clean_forgery = self.client.get(reverse("survey-status"), {
            "status": "1",
            "pid": attempt.pid,
        })
        self.assertEqual(clean_forgery.status_code, 409)
        supplier_result_url.assert_not_called()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.QUALITY_TERMINATED)

    @patch("surveys.views._external_supplier_result_url", return_value="")
    def test_first_callback_transitions_then_raw_replay_is_rejected(self, supplier_result_url):
        attempt = self.make_attempt("Le1Gi2Ti3M")

        accepted = self.client.get(reverse("survey-status"), {
            "status": "3",
            "rid": attempt.rid,
        })

        self.assertEqual(accepted.status_code, 302)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.OVER_QUOTA)
        self.assertEqual(attempt.callback_count, 1)
        supplier_result_url.assert_called_once()
        self.assertEqual(supplier_result_url.call_args.args[1], SurveyAttempt.Status.OVER_QUOTA)

        supplier_result_url.reset_mock()
        replay = self.client.get(reverse("survey-status"), {
            "status": "3",
            "rid": attempt.rid,
        })
        self.assertEqual(replay.status_code, 409)
        supplier_result_url.assert_not_called()
        attempt.refresh_from_db()
        self.assertEqual(attempt.callback_count, 1)

        display = self.client.get(reverse("survey-status"), {
            "status": "3",
            "pid": attempt.pid,
        })
        self.assertEqual(display.status_code, 200)
        self.assertContains(display, "Quota already filled")
        self.assertContains(display, attempt.pid)
        self.assertNotContains(display, attempt.rid)

        duplicate_pid = self.client.get(
            f"{reverse('survey-status')}?status=3&pid={attempt.pid}&pid={attempt.pid}"
        )
        self.assertEqual(duplicate_pid.status_code, 409)


class RFGTerminalCallbackSecurityTests(TestCase):
    trusted_ip = "15.222.163.99"

    def setUp(self):
        client = Client.objects.create(
            code="callback-security-rfg",
            name="RFG",
            provider_code="rfg",
        )
        integration = ClientIntegration.objects.create(
            client=client,
            name="RFG callback security",
            provider_code="rfg",
            base_url="https://api.researchforgood.com/API/",
            config={
                "callback_security_mode": "ip",
                "callback_ip_allowlist": [self.trusted_ip],
            },
        )
        self.survey = Survey.objects.create(
            source_key="RFG-CALLBACK-SECURITY",
            client=client,
            integration=integration,
            company_name="RFG",
            status=Survey.Status.LIVE,
        )

    def test_rfg_first_terminal_result_is_accepted_but_conflict_cannot_overwrite_it(self):
        attempt = SurveyAttempt.objects.create(
            rid="Rf1Gc2Al3L",
            survey=self.survey,
            user_id="rfg-security-test",
            status=SurveyAttempt.Status.REDIRECTED,
        )

        accepted = self.client.get(reverse("rfg-callback"), {
            "tid": attempt.rid,
            "result": "10",
        }, REMOTE_ADDR=self.trusted_ip)
        self.assertEqual(accepted.status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.QUALITY_TERMINATED)
        self.assertTrue(attempt.is_verified)
        self.assertEqual(attempt.callback_count, 1)
        original_callback_at = attempt.callback_at
        original_audit = attempt.upstream_transaction_data

        conflict = self.client.get(reverse("rfg-callback"), {
            "tid": attempt.rid,
            "result": "1",
        }, REMOTE_ADDR=self.trusted_ip)
        self.assertEqual(conflict.status_code, 409)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.QUALITY_TERMINATED)
        self.assertEqual(attempt.callback_count, 1)
        self.assertEqual(attempt.callback_at, original_callback_at)
        self.assertEqual(attempt.upstream_transaction_data, original_audit)

        replay = self.client.get(reverse("rfg-callback"), {
            "tid": attempt.rid,
            "result": "10",
        }, REMOTE_ADDR=self.trusted_ip)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["idempotent"])
        attempt.refresh_from_db()
        self.assertEqual(attempt.callback_count, 1)
        self.assertEqual(attempt.upstream_transaction_data, original_audit)

    @override_settings(TRUST_X_FORWARDED_FOR=True)
    def test_rfg_allowlist_cannot_be_forged_with_x_forwarded_for(self):
        attempt = SurveyAttempt.objects.create(
            rid="Sp1Oo2Fe3D",
            survey=self.survey,
            user_id="rfg-spoof-test",
            status=SurveyAttempt.Status.REDIRECTED,
        )

        response = self.client.get(reverse("rfg-callback"), {
            "tid": attempt.rid,
            "result": "1",
        }, REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR=self.trusted_ip,
           HTTP_X_REAL_IP="203.0.113.20")

        self.assertEqual(response.status_code, 403)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.REDIRECTED)
        self.assertFalse(attempt.is_verified)
        self.assertEqual(attempt.callback_count, 0)

    @patch("surveys.views._external_supplier_result_url")
    def test_generic_status_endpoint_cannot_finalize_rfg_attempt(self, supplier_result_url):
        attempt = SurveyAttempt.objects.create(
            rid="Br1Ow2Se3R",
            survey=self.survey,
            user_id="rfg-browser-forgery-test",
            status=SurveyAttempt.Status.REDIRECTED,
        )

        response = self.client.get(reverse("survey-status"), {
            "status": "1",
            "rid": attempt.rid,
        })

        self.assertEqual(response.status_code, 403)
        supplier_result_url.assert_not_called()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.REDIRECTED)
        self.assertIsNone(attempt.callback_at)
        self.assertFalse(attempt.is_verified)
        self.assertEqual(attempt.callback_count, 0)

    def test_rfg_callback_redacts_session_key_and_rejects_duplicate_result(self):
        attempt = SurveyAttempt.objects.create(
            rid="Re1Da2Ct3D",
            survey=self.survey,
            user_id="rfg-redaction-test",
            status=SurveyAttempt.Status.REDIRECTED,
        )

        accepted = self.client.get(reverse("rfg-callback"), {
            "tid": attempt.rid,
            "result": "1",
            "sesskey": "provider-bearer-secret",
        }, REMOTE_ADDR=self.trusted_ip)

        self.assertEqual(accepted.status_code, 200)
        attempt.refresh_from_db()
        stored = attempt.upstream_transaction_data["rfg_callback"]
        self.assertEqual(stored["sesskey"], "[redacted]")
        self.assertNotIn("provider-bearer-secret", str(attempt.upstream_transaction_data))

        other = SurveyAttempt.objects.create(
            rid="Du1Pl2Ic3T",
            survey=self.survey,
            user_id="rfg-duplicate-test",
            status=SurveyAttempt.Status.REDIRECTED,
        )
        duplicate = self.client.get(
            f"{reverse('rfg-callback')}?tid={other.rid}&result=1&result=10",
            REMOTE_ADDR=self.trusted_ip,
        )
        self.assertEqual(duplicate.status_code, 400)
        other.refresh_from_db()
        self.assertEqual(other.status, SurveyAttempt.Status.REDIRECTED)
        self.assertEqual(other.callback_count, 0)

    def test_rfg_browser_result_audit_is_redacted_and_immutable(self):
        attempt = SurveyAttempt.objects.create(
            rid="Im1Mu2Ta3B",
            survey=self.survey,
            user_id="rfg-browser-audit-test",
            status=SurveyAttempt.Status.REDIRECTED,
        )

        first = self.client.get(reverse("rfg-result"), {
            "pid": attempt.pid,
            "result": "2",
            "sesskey": "browser-session-secret",
        })
        self.assertEqual(first.status_code, 200)
        attempt.refresh_from_db()
        original_audit = attempt.upstream_transaction_data
        self.assertEqual(
            original_audit["rfg_browser_return"]["sesskey"], "[redacted]"
        )

        changed = self.client.get(reverse("rfg-result"), {
            "pid": attempt.pid,
            "result": "1",
            "sesskey": "replacement-secret",
        })
        self.assertEqual(changed.status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.upstream_transaction_data, original_audit)
        self.assertNotIn("replacement-secret", str(attempt.upstream_transaction_data))

        duplicate = self.client.get(
            f"{reverse('rfg-result')}?pid={attempt.pid}&result=1&result=2"
        )
        self.assertEqual(duplicate.status_code, 400)
