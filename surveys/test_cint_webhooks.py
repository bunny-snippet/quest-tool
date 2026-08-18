"""Contract tests for signed Cint Feed Opportunities ingestion."""

import base64
import json
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse

from vendors.models import Client, ClientIntegration

from .cint_webhooks import subscription_payload
from .models import CintWebhookDelivery, Survey


@override_settings(
    CINT_OPPORTUNITIES_WEBHOOK_ENABLED=True,
    CINT_OPPORTUNITIES_SUPPLIER_CODE="6528",
    CINT_OPPORTUNITIES_PUBLIC_KEY="",
    CINT_OPPORTUNITIES_KEY_ID="",
    CINT_OPPORTUNITIES_PROCESS_SYNCHRONOUS=True,
    CINT_OPPORTUNITIES_QUEUE_REDIRECTS=False,
    CINT_OPPORTUNITIES_SIGNATURE_TOLERANCE_SECONDS=300,
)
class CintOpportunitiesWebhookTests(TestCase):
    def test_django_request_body_limit_allows_configured_webhook_payload(self):
        self.assertGreater(
            settings.DATA_UPLOAD_MAX_MEMORY_SIZE,
            settings.CINT_OPPORTUNITIES_MAX_PAYLOAD_BYTES,
        )

    def setUp(self):
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        public_der = self.private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.key_id = "fa883b52-1eb5-47b8-94b8-35f478d6220e"
        self.client_record = Client.objects.create(
            code="cint-webhook",
            name="Cint Exchange",
            provider_code="cint",
        )
        self.integration = ClientIntegration.objects.create(
            client=self.client_record,
            name="Cint webhook",
            provider_code="cint",
            base_url="https://api.samplicio.us",
            supplier_code="6528",
            config={
                "opportunities_public_key": base64.b64encode(public_der).decode("ascii"),
                "opportunities_key_id": self.key_id,
            },
        )
        self.url = reverse("cint-opportunities-webhook")

    def opportunity(self, **overrides):
        row = {
            "survey_id": 82199770,
            "survey_name": "Signed webhook survey",
            "account_name": "Savanta",
            "buyer_id": 44,
            "country_language": "eng_us",
            "industry": "other",
            "study_type": "adhoc",
            "bid_length_of_interview": 12,
            "bid_incidence": 45,
            "collects_pii": False,
            "is_live": True,
            "revenue_per_interview": {"value": 2.25, "currency_code": "USD"},
            "total_remaining": 36,
            "overall_completes": 4,
            "relationship_type": "open",
            "respondent_pids": [],
            "message_reason": "new",
            "survey_quotas": [
                {
                    "survey_quota_id": 9001,
                    "survey_quota_type": "Total",
                    "conversion": 0.4,
                    "number_of_respondents": 36,
                    "questions": None,
                }
            ],
            "survey_qualifications": [
                {"question_id": 43, "logical_operator": "OR", "precodes": ["1"]}
            ],
        }
        row.update(overrides)
        return row

    def signed_post(self, payload, *, timestamp=None, mutate_after_sign=False):
        timestamp = int(time.time()) if timestamp is None else int(timestamp)
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        signature = self.private_key.sign(
            str(timestamp).encode("ascii") + b"." + raw,
            ec.ECDSA(hashes.SHA256()),
        )
        header = (
            f"t:{timestamp},v1:{self.key_id[:8]}:"
            + base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
        )
        if mutate_after_sign:
            raw += b" "
        return self.client.post(
            self.url,
            data=raw,
            content_type="application/json",
            HTTP_X_LUCID_SIGNATURE=header,
        )

    def test_subscription_payload_uses_current_api_field_names_and_filters(self):
        payload = subscription_payload("https://example.test/api/cint/webhook/surveys")

        self.assertIn("payload_max_size_mb", payload)
        self.assertIn("payload_max_survey_count", payload)
        self.assertNotIn("max_message_size_mb", payload)
        self.assertNotIn("public_key", payload)
        self.assertNotIn("key_id", payload)
        self.assertEqual(payload["payload_max_survey_count"], 1000)
        self.assertEqual(payload["send_interval_seconds"], 5)
        self.assertEqual(payload["opportunities"][1]["country_language"]["in"], ["eng_gb"])

    @override_settings(CINT_OPPORTUNITIES_WEBHOOK_ENABLED=False)
    def test_per_integration_switch_enables_signed_receiver(self):
        self.integration.config = {
            **self.integration.config,
            "opportunities_webhook_enabled": True,
            "opportunities_callback_url": "https://api.exchange-ip.com/api/cint/webhook/surveys",
        }
        self.integration.save(update_fields=["config", "updated_at"])

        response = self.signed_post(self.opportunity())

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["created"], 1)

    def test_signed_new_opportunity_creates_inventory_quota_and_targeting(self):
        response = self.signed_post(self.opportunity())

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["created"], 1)
        survey = Survey.objects.get(integration=self.integration, source_key="82199770")
        self.assertEqual(survey.country_code, "US")
        self.assertEqual(str(survey.cpi), "2.25")
        self.assertEqual(survey.loi, 12)
        self.assertEqual(str(survey.incidence_rate), "45.00")
        self.assertEqual(survey.remaining, 36)
        self.assertEqual(survey.quotas.count(), 1)
        self.assertEqual(survey.targeting_questions.count(), 1)
        self.assertEqual(
            survey.raw_data["_cint_inventory_source"],
            "opportunities_webhook",
        )
        self.assertEqual(CintWebhookDelivery.objects.get().status, "processed")

    def test_webhook_gender_precodes_render_as_labels(self):
        response = self.signed_post(self.opportunity(
            survey_qualifications=[
                {"question_id": 43, "logical_operator": "OR", "precodes": ["1", "2"]}
            ],
        ))

        self.assertEqual(response.status_code, 200, response.content)
        question = Survey.objects.get(
            integration=self.integration,
            source_key="82199770",
        ).targeting_questions.get(question_id=43)
        self.assertEqual(question.key, "GENDER")
        self.assertEqual(question.text, "What is your gender?")
        self.assertEqual(question.question_type, "Single")
        self.assertEqual(question.options, [
            {"OptionId": "1", "OptionText": "Male"},
            {"OptionId": "2", "OptionText": "Female"},
        ])

    def test_same_signed_delivery_is_idempotent(self):
        payload = self.opportunity()
        timestamp = int(time.time())
        first = self.signed_post(payload, timestamp=timestamp)
        second = self.signed_post(payload, timestamp=timestamp)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["status"], "duplicate")
        self.assertEqual(Survey.objects.filter(integration=self.integration).count(), 1)
        self.assertEqual(CintWebhookDelivery.objects.count(), 1)

    def test_update_replaces_metrics_and_deactivated_message_closes_survey(self):
        self.signed_post(self.opportunity())
        updated = self.opportunity(
            message_reason="updated",
            total_remaining=20,
            revenue_per_interview={"value": 3.5, "currency_code": "USD"},
        )
        response = self.signed_post(updated)
        self.assertEqual(response.status_code, 200)
        survey = Survey.objects.get(integration=self.integration, source_key="82199770")
        self.assertEqual(survey.remaining, 20)
        self.assertEqual(str(survey.cpi), "3.50")

        response = self.signed_post(self.opportunity(message_reason="deactivated", is_live=False))
        self.assertEqual(response.status_code, 200)
        survey.refresh_from_db()
        self.assertEqual(survey.status, Survey.Status.CLOSED)

    def test_out_of_band_rpi_is_not_saved(self):
        response = self.signed_post(self.opportunity(
            survey_id=82199771,
            country_language="eng_gb",
            revenue_per_interview={"value": 2.5, "currency_code": "USD"},
        ))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["skipped"], 1)
        self.assertFalse(Survey.objects.filter(source_key="82199771").exists())

    def test_tampered_or_expired_callback_is_rejected(self):
        tampered = self.signed_post(self.opportunity(), mutate_after_sign=True)
        expired = self.signed_post(self.opportunity(), timestamp=int(time.time()) - 301)

        self.assertEqual(tampered.status_code, 401)
        self.assertEqual(expired.status_code, 401)
        self.assertEqual(CintWebhookDelivery.objects.count(), 0)
