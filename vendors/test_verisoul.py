from decimal import Decimal
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import EmployeeProfile
from surveys.models import Survey, SurveyAttempt

from .models import (
    Client,
    OrganizationClientAccess,
    OrganizationUnit,
    SecurityPolicyMode,
    VendorClientAllocation,
)
from .verisoul import (
    VerisoulError,
    authenticate_verisoul_session,
    effective_verisoul_policy,
    verisoul_group_identifier,
)


@override_settings(
    VERISOUL_ENV="sandbox",
    VERISOUL_PROJECT_ID="project-test",
    VERISOUL_API_KEY="private-test-key",
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

        self.client_record.verisoul_enabled = False
        self.client_record.save(update_fields=["verisoul_enabled", "updated_at"])
        allocation.verisoul_mode = SecurityPolicyMode.ENABLED
        allocation.save(update_fields=["verisoul_mode", "updated_at"])
        self.assertTrue(effective_verisoul_policy(self.attempt).enabled)

    def _organization_tree(self, prefix="security"):
        owner = get_user_model().objects.create_superuser(
            username=f"{prefix}-owner", email=f"{prefix}@example.test", password="test-pass",
        )
        branch = OrganizationUnit.objects.create(
            workspace_owner=owner, unit_type=OrganizationUnit.UnitType.BRANCH,
            name=f"{prefix} Branch", code=f"{prefix}-branch", created_by=owner,
        )
        sub_branch = OrganizationUnit.objects.create(
            workspace_owner=owner, parent=branch,
            unit_type=OrganizationUnit.UnitType.SUB_BRANCH,
            name="Operations", code="operations", created_by=owner,
        )
        shift = OrganizationUnit.objects.create(
            workspace_owner=owner, parent=sub_branch,
            unit_type=OrganizationUnit.UnitType.SHIFT,
            name="Morning", code="morning", created_by=owner,
        )
        EmployeeProfile.objects.update_or_create(
            user=self.user, defaults={"organization_unit": shift, "created_by": owner},
        )
        self.attempt.platform_user = self.user
        return branch, sub_branch, shift

    def test_client_master_toggle_is_the_default_for_the_entire_tree(self):
        branch, _, _ = self._organization_tree("master")
        OrganizationClientAccess.objects.create(
            organization_unit=branch, client=self.client_record,
            verisoul_mode=SecurityPolicyMode.INHERIT,
        )

        self.assertTrue(effective_verisoul_policy(self.attempt).enabled)

        self.client_record.verisoul_enabled = False
        self.client_record.save(update_fields=["verisoul_enabled", "updated_at"])
        self.attempt.client = self.client_record
        self.attempt.survey.client = self.client_record

        policy = effective_verisoul_policy(self.attempt)
        self.assertFalse(policy.enabled)
        self.assertEqual(policy.scope, "client")

    def test_nearest_organization_override_wins_for_only_its_subtree(self):
        branch, sub_branch, shift = self._organization_tree("nearest")
        sibling_branch = OrganizationUnit.objects.create(
            workspace_owner=branch.workspace_owner,
            unit_type=OrganizationUnit.UnitType.BRANCH,
            name="Sibling Branch", code="sibling-branch", created_by=branch.workspace_owner,
        )
        sibling_sub_branch = OrganizationUnit.objects.create(
            workspace_owner=branch.workspace_owner, parent=sibling_branch,
            unit_type=OrganizationUnit.UnitType.SUB_BRANCH,
            name="Sibling Operations", code="sibling-operations", created_by=branch.workspace_owner,
        )
        sibling_shift = OrganizationUnit.objects.create(
            workspace_owner=branch.workspace_owner, parent=sibling_sub_branch,
            unit_type=OrganizationUnit.UnitType.SHIFT,
            name="Sibling Morning", code="sibling-morning", created_by=branch.workspace_owner,
        )
        self.client_record.verisoul_enabled = False
        self.client_record.save(update_fields=["verisoul_enabled", "updated_at"])
        OrganizationClientAccess.objects.bulk_create([
            OrganizationClientAccess(
                organization_unit=branch, client=self.client_record,
                verisoul_mode=SecurityPolicyMode.ENABLED,
            ),
            OrganizationClientAccess(
                organization_unit=sub_branch, client=self.client_record,
                verisoul_mode=SecurityPolicyMode.INHERIT,
            ),
            OrganizationClientAccess(
                organization_unit=shift, client=self.client_record,
                verisoul_mode=SecurityPolicyMode.DISABLED,
            ),
            OrganizationClientAccess(
                organization_unit=sibling_branch, client=self.client_record,
                verisoul_mode=SecurityPolicyMode.INHERIT,
            ),
        ])

        # The closest Shift override disables this single path.
        self.assertFalse(effective_verisoul_policy(self.attempt).enabled)

        OrganizationClientAccess.objects.filter(
            organization_unit=shift, client=self.client_record,
        ).update(verisoul_mode=SecurityPolicyMode.INHERIT)
        self.user = get_user_model().objects.get(pk=self.user.pk)
        self.attempt.platform_user = self.user
        inherited = effective_verisoul_policy(self.attempt)
        self.assertTrue(inherited.enabled)
        self.assertEqual(inherited.scope_id, branch.pk)

        # A sibling without an explicit ON remains on the client master OFF.
        EmployeeProfile.objects.filter(user=self.user).update(organization_unit=sibling_shift)
        self.user = get_user_model().objects.get(pk=self.user.pk)
        self.attempt.platform_user = self.user
        sibling = effective_verisoul_policy(self.attempt)
        self.assertFalse(sibling.enabled)
        self.assertEqual(sibling.scope, "client")

    @patch("vendors.verisoul.requests.post")
    def test_real_decision_passes_and_preserves_score(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {
            "decision": "Real", "account_score": 0.99,
            "request_id": "request-1", "project_id": "project-test", "session": {},
        }
        post.return_value = response

        result = authenticate_verisoul_session(session_id="session-1", attempt=self.attempt)

        self.assertTrue(result.passed)
        self.assertEqual(result.account_score, Decimal("0.99"))
        self.assertEqual(result.reason, "Verisoul classified the session as real.")
        self.assertEqual(post.call_args.kwargs["headers"]["x-api-key"], "private-test-key")
        self.assertNotIn("private-test-key", str(post.call_args.kwargs["json"]))
        self.assertEqual(
            post.call_args.kwargs["json"]["account"]["group"],
            f"survey_{self.survey.local_id}",
        )
        self.assertNotIn("metadata", post.call_args.kwargs["json"]["account"])

    def test_group_identifier_is_stable_per_survey(self):
        other_attempt = SurveyAttempt(
            rid="Zy98Xw76Vu", survey=self.survey, platform_user=self.user,
        )
        self.assertEqual(
            verisoul_group_identifier(self.attempt),
            verisoul_group_identifier(other_attempt),
        )

    @patch("vendors.verisoul.requests.post")
    def test_non_real_decision_fails_even_with_a_low_score(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {
            "decision": "Fake", "account_score": 0.01,
            "request_id": "request-2", "project_id": "project-test", "session": {},
        }
        post.return_value = response

        result = authenticate_verisoul_session(session_id="session-2", attempt=self.attempt)

        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "Verisoul classified the session as Fake.")

    @patch("vendors.verisoul.requests.post")
    def test_provider_error_keeps_safe_diagnostic_message(self, post):
        response = Mock(status_code=400)
        response.json.return_value = {"message": "Session ID not found"}
        post.return_value = response

        with self.assertRaisesMessage(
            VerisoulError,
            "Verisoul verification failed (HTTP 400): Session ID not found",
        ):
            authenticate_verisoul_session(session_id="session-mismatch", attempt=self.attempt)

    @override_settings(ALLOW_LEGACY_UNSIGNED_ENTRY_LINKS=True)
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
        self.assertContains(gate, 'id="verisoul-sdk"')
        self.assertContains(gate, "window.Verisoul = new Proxy")
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
