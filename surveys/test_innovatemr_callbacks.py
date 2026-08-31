from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from vendors.models import Client, ClientIntegration

from .innovatemr_callbacks import sign_callback_url
from .models import Survey, SurveyAttempt
from .outcomes import provider_outcome


@override_settings(
    INNOVATEMR_CALLBACK_HASH_KEY="test-innovate-callback-secret",
    INNOVATEMR_CALLBACK_HASH_ALGORITHM="sha256",
    INNOVATEMR_CALLBACK_HASH_REQUIRED=True,
    PUBLIC_APP_BASE_URL="https://api.exchange-ip.com",
)
class InnovateMRCallbackTests(TestCase):
    secret = "test-innovate-callback-secret"

    def setUp(self):
        client = Client.objects.create(
            code="innovate-callback-test",
            name="InnovateMR",
            provider_code="innovatemr",
        )
        integration = ClientIntegration.objects.create(
            client=client,
            name="InnovateMR callback test",
            provider_code="innovatemr",
            base_url="https://supplier.innovatemr.net/api/v2",
        )
        self.survey = Survey.objects.create(
            source_id=987654,
            source_key="987654",
            client=client,
            integration=integration,
            company_name="InnovateMR",
            status=Survey.Status.LIVE,
        )
        self.attempt = SurveyAttempt.objects.create(
            rid="In1No2Va3T",
            survey=self.survey,
            user_id="respondent-1",
            status=SurveyAttempt.Status.REDIRECTED,
            upstream_transaction_data={
                "status": "Pre Survey Termination",
                "termReason": "Transaction API fallback",
            },
        )

    def signed_callback_path(self, **parameters):
        pairs = list(parameters.items()) + [("hash", "")]
        unsigned_query = urlencode(pairs)
        unsigned_url = f"https://api.exchange-ip.com{reverse('survey-status')}?{unsigned_query}"
        signature = sign_callback_url(unsigned_url, self.secret, "sha256")
        return f"{reverse('survey-status')}?{unsigned_query}{signature}"

    def test_valid_hash_records_status_and_signed_redirect_reason(self):
        response = self.client.get(self.signed_callback_path(
            status="2",
            rid=self.attempt.rid,
            surveyId=self.survey.source_id,
            termReason="Profile did not match the remaining quota",
            closeQuotaId="quota-7",
        ))

        self.assertEqual(response.status_code, 302)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, SurveyAttempt.Status.TERMINATED)
        self.assertEqual(self.attempt.status_source, "innovatemr_signed_redirect")
        self.assertTrue(self.attempt.is_verified)
        self.assertIsNotNone(self.attempt.callback_at)
        self.assertEqual(self.attempt.callback_count, 1)
        callback = self.attempt.exit_client_data["innovatemr_callback"]
        self.assertEqual(callback["termReason"], "Profile did not match the remaining quota")
        self.assertEqual(callback["closeQuotaId"], "quota-7")
        self.assertNotIn("hash", callback)
        self.assertEqual(
            self.attempt.upstream_transaction_data["innovatemr_browser_return"]["hash"],
            "[redacted]",
        )
        self.assertEqual(
            provider_outcome(self.attempt)["reason"],
            "Profile did not match the remaining quota",
        )

        owner = get_user_model().objects.create_superuser(
            username="innovate-term-report-owner",
            email="owner@example.test",
            password="test-password",
        )
        self.client.force_login(owner)
        report = self.client.get(reverse("termination-reasons"), {
            "search": self.attempt.rid,
        })
        self.assertEqual(report.status_code, 200)
        self.assertContains(report, "Profile did not match the remaining quota")

        # A later Survey Transactions refresh may replace its history payload;
        # the authenticated immediate reason remains independently available.
        self.attempt.upstream_transaction_data = {
            "status": "Pre Survey Termination",
            "termReason": "Broader API reason",
            "trackId": self.attempt.rid,
        }
        self.attempt.save(update_fields=["upstream_transaction_data", "updated_at"])
        self.assertEqual(
            provider_outcome(self.attempt)["reason"],
            "Profile did not match the remaining quota",
        )

    def test_invalid_hash_cannot_credit_a_complete_or_mutate_attempt(self):
        path = self.signed_callback_path(
            status="1",
            rid=self.attempt.rid,
            termReason="",
        )
        response = self.client.get(f"{path[:-64]}{'0' * 64}")

        self.assertEqual(response.status_code, 403)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, SurveyAttempt.Status.REDIRECTED)
        self.assertFalse(self.attempt.is_verified)
        self.assertIsNone(self.attempt.callback_at)
        self.assertEqual(self.attempt.callback_count, 0)
        self.assertNotIn("innovatemr_callback", self.attempt.exit_client_data)

    def test_invalid_browser_hash_displays_matching_verified_transaction_result(self):
        self.attempt.status = SurveyAttempt.Status.QUALITY_TERMINATED
        self.attempt.status_source = "innovatemr_transaction"
        self.attempt.is_verified = True
        self.attempt.callback_at = timezone.now()
        self.attempt.save(update_fields=[
            "status", "status_source", "is_verified", "callback_at", "updated_at",
        ])

        response = self.client.get(reverse("survey-status"), {
            "status": "4",
            "rid": self.attempt.rid,
            "hash": "incorrect-provider-hash",
        })

        self.assertRedirects(
            response,
            f"{reverse('survey-status')}?status=4&pid={self.attempt.pid}",
            fetch_redirect_response=False,
        )
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.callback_count, 0)
        self.assertEqual(self.attempt.status_source, "innovatemr_transaction")

    def test_invalid_hash_cannot_display_a_different_transaction_result(self):
        self.attempt.status = SurveyAttempt.Status.QUALITY_TERMINATED
        self.attempt.status_source = "innovatemr_transaction"
        self.attempt.is_verified = True
        self.attempt.save(update_fields=["status", "status_source", "is_verified", "updated_at"])

        response = self.client.get(reverse("survey-status"), {
            "status": "1",
            "rid": self.attempt.rid,
            "hash": "incorrect-provider-hash",
        })

        self.assertEqual(response.status_code, 403)

    def test_missing_hash_is_rejected_without_recording_result(self):
        response = self.client.get(reverse("survey-status"), {
            "status": "3",
            "rid": self.attempt.rid,
            "termReason": "Quota full",
        })

        self.assertEqual(response.status_code, 403)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, SurveyAttempt.Status.REDIRECTED)
        self.assertIsNone(self.attempt.callback_at)

    @override_settings(
        INNOVATEMR_CALLBACK_HASH_KEY="",
        INNOVATEMR_CALLBACK_HASH_REQUIRED=True,
    )
    def test_missing_server_secret_fails_closed_without_recording_result(self):
        response = self.client.get(reverse("survey-status"), {
            "status": "1",
            "rid": self.attempt.rid,
            "hash": "attacker-controlled",
        })

        self.assertEqual(response.status_code, 403)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, SurveyAttempt.Status.REDIRECTED)
        self.assertFalse(self.attempt.is_verified)
        self.assertIsNone(self.attempt.callback_at)
        self.assertEqual(self.attempt.callback_count, 0)

    def test_clean_internal_result_url_does_not_require_second_signature(self):
        callback = self.client.get(self.signed_callback_path(
            status="4",
            rid=self.attempt.rid,
            termReason="Quality validation failed",
        ))
        clean_result = self.client.get(callback["Location"])

        self.assertEqual(clean_result.status_code, 200)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.callback_count, 1)
