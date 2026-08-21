from decimal import Decimal
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from surveys.models import Survey, SurveyAttempt

from .models import Client, SecurityPolicyMode, VendorClientAllocation
from .verisoul import authenticate_verisoul_session, effective_verisoul_policy


@override_settings(
    VERISOUL_ENV="sandbox",
    VERISOUL_PROJECT_ID="project-test",
    VERISOUL_API_KEY="private-test-key",
    VERISOUL_ACCOUNT_SCORE_THRESHOLD=Decimal("0.70"),
)
class VerisoulPolicyTests(TestCase):
    def setUp(self):
        self.client_record = Client.objects.create(
            code="secure-client", name="Secure Client", verisoul_enabled=True,
        )
        self.survey = Survey.objects.create(
            client=self.client_record, source_id=90001, name="Secure survey", country_code="US",
        )
        self.user = get_user_model().objects.create_user("secure-user")
        self.attempt = SurveyAttempt.objects.create(
            rid="Ab12Cd34Ef", survey=self.survey, platform_user=self.user,
            client=self.client_record, user_id=str(self.user.pk), initiation_ip="127.0.0.1",
        )

    def test_client_default_is_inherited_and_supplier_can_bypass(self):
        policy = effective_verisoul_policy(self.attempt)
        self.assertTrue(policy.enabled)
        self.assertEqual(policy.scope, "client")

        supplier = get_user_model().objects.create_user("external-supplier")
        allocation = VendorClientAllocation.objects.create(
            vendor=supplier, client=self.client_record,
            verisoul_mode=SecurityPolicyMode.DISABLED,
        )
        self.attempt.client_allocation = allocation
        self.assertFalse(effective_verisoul_policy(self.attempt).enabled)

    @patch("vendors.verisoul.requests.post")
    def test_only_real_below_threshold_passes(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {
            "decision": "Real", "account_score": 0.69,
            "request_id": "request-1", "project_id": "project-test", "session": {},
        }
        post.return_value = response

        result = authenticate_verisoul_session(session_id="session-1", attempt=self.attempt)

        self.assertTrue(result.passed)
        self.assertEqual(post.call_args.kwargs["headers"]["x-api-key"], "private-test-key")
        self.assertNotIn("private-test-key", str(post.call_args.kwargs["json"]))

    @patch("vendors.verisoul.requests.post")
    def test_threshold_boundary_fails_closed(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {
            "decision": "Real", "account_score": 0.70,
            "request_id": "request-2", "project_id": "project-test", "session": {},
        }
        post.return_value = response

        result = authenticate_verisoul_session(session_id="session-2", attempt=self.attempt)

        self.assertFalse(result.passed)

    @patch("vendors.verisoul.requests.post")
    def test_public_gate_passes_only_after_backend_authentication(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {
            "decision": "Real", "account_score": 0.2,
            "request_id": "request-3", "project_id": "project-test", "session": {},
        }
        post.return_value = response

        gate = self.client.get(reverse("survey-start"), {"rid": self.attempt.rid})
        self.assertContains(gate, 'class="silent-loader"')
        self.assertNotContains(gate, "Checking your browser")
        self.assertNotContains(gate, f"RID {self.attempt.rid}")

        verified = self.client.post(
            reverse("survey-security-check"),
            data='{"rid":"Ab12Cd34Ef","session_id":"session-3"}',
            content_type="application/json",
        )
        self.assertEqual(verified.status_code, 200)
        self.assertEqual(verified.json()["status"], "passed")
        self.assertEqual(self.attempt.verisoul_assessment.status, "passed")
