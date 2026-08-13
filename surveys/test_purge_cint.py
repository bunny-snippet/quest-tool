from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from prescreener_vault.constants import DATABASE_ALIAS
from prescreener_vault.models import (
    CintRespondentEmail,
    CintRespondentEmailUse,
    PrescreenerSubmission,
)
from vendors.models import (
    AllocationReservation,
    Client,
    ClientIntegration,
    VendorClientAllocation,
    VendorSurveyAllocation,
)

from .models import Survey, SurveyAttempt, SurveyQuota, SyncLease, SyncRun, TargetingQuestion


@override_settings(PRESCREENER_VAULT_ENABLED=True)
class PurgeCintDataCommandTests(TestCase):
    databases = {"default", DATABASE_ALIAS}

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="cint-purge-user")
        self.cint_client = Client.objects.create(
            code="cint-purge", name="Cint Exchange", provider_code="cint"
        )
        self.rfg_client = Client.objects.create(
            code="rfg-purge", name="Research For Good", provider_code="rfg"
        )
        self.cint_integration = ClientIntegration.objects.create(
            client=self.cint_client,
            name="Cint production",
            provider_code="cint",
            base_url="https://api.samplicio.us",
            scheduled_sync_enabled=True,
            last_test_status="success",
            last_sync_status="success",
        )
        self.rfg_integration = ClientIntegration.objects.create(
            client=self.rfg_client,
            name="RFG production",
            provider_code="rfg",
            base_url="https://api.researchforgood.com",
        )
        self.cint_survey = Survey.objects.create(
            client=self.cint_client,
            integration=self.cint_integration,
            source_key="82198755",
            company_name="Cint Exchange",
        )
        self.rfg_survey = Survey.objects.create(
            client=self.rfg_client,
            integration=self.rfg_integration,
            source_key="RFG23001",
            company_name="RFG",
        )
        SurveyQuota.objects.create(
            survey=self.cint_survey, source_key="cint-quota", name="Cint quota"
        )
        TargetingQuestion.objects.create(
            survey=self.cint_survey,
            question_id=43,
            text="Cint age",
        )
        self.client_allocation = VendorClientAllocation.objects.create(
            vendor=self.user,
            client=self.cint_client,
            quantity_limit=10,
            reserved_quantity=1,
            consumed_quantity=2,
        )
        self.survey_allocation = VendorSurveyAllocation.objects.create(
            client_allocation=self.client_allocation,
            survey=self.cint_survey,
            quantity_limit=10,
            reserved_quantity=1,
            consumed_quantity=2,
        )
        self.cint_attempt = SurveyAttempt.objects.create(
            rid="CintRid001",
            prescreener_uid="CINT-UID0-0000-0001",
            survey=self.cint_survey,
            client=self.cint_client,
            client_allocation=self.client_allocation,
            survey_allocation=self.survey_allocation,
            user_id=str(self.user.pk),
        )
        self.rfg_attempt = SurveyAttempt.objects.create(
            rid="RfgRid0001",
            prescreener_uid="RFG0-UID0-0000-0001",
            survey=self.rfg_survey,
            client=self.rfg_client,
            user_id=str(self.user.pk),
        )
        AllocationReservation.objects.create(
            attempt=self.cint_attempt,
            client_allocation=self.client_allocation,
            survey_allocation=self.survey_allocation,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        SyncRun.objects.create(integration=self.cint_integration)
        SyncRun.objects.create(integration=self.rfg_integration)
        SyncLease.objects.create(name=f"integration-{self.cint_integration.pk}-sync")
        SyncLease.objects.create(name=f"cint-redirects-{self.cint_integration.pk}")

        for attempt in (self.cint_attempt, self.rfg_attempt):
            PrescreenerSubmission.objects.using(DATABASE_ALIAS).create(
                uid=attempt.prescreener_uid,
                rid=attempt.rid,
                country_code="US",
                language_code="ENG",
                submitted_at=timezone.now(),
            )
        self.email_identity = CintRespondentEmail.objects.using(DATABASE_ALIAS).create(
            encrypted_email="encrypted-real-email",
            email_hash="a" * 64,
            assigned_uid=self.cint_attempt.prescreener_uid,
            status=CintRespondentEmail.Status.ASSIGNED,
            use_count=1,
            assigned_at=timezone.now(),
            first_used_at=timezone.now(),
            last_used_at=timezone.now(),
        )
        CintRespondentEmailUse.objects.using(DATABASE_ALIAS).create(
            identity=self.email_identity,
            rid=self.cint_attempt.rid,
        )

    def test_dry_run_does_not_mutate_any_data(self):
        output = StringIO()
        call_command("purge_cint_data", stdout=output)

        self.assertIn("Dry run only", output.getvalue())
        self.assertTrue(Survey.objects.filter(pk=self.cint_survey.pk).exists())
        self.assertTrue(SurveyAttempt.objects.filter(pk=self.cint_attempt.pk).exists())
        self.cint_integration.refresh_from_db()
        self.assertTrue(self.cint_integration.is_active)
        self.assertTrue(self.cint_integration.scheduled_sync_enabled)

    def test_confirmed_purge_removes_only_cint_data_and_pauses_integration(self):
        call_command("purge_cint_data", confirm="DELETE-CINT-DATA")

        self.assertFalse(Survey.objects.filter(pk=self.cint_survey.pk).exists())
        self.assertFalse(SurveyAttempt.objects.filter(pk=self.cint_attempt.pk).exists())
        self.assertFalse(SurveyQuota.objects.filter(survey_id=self.cint_survey.pk).exists())
        self.assertFalse(TargetingQuestion.objects.filter(survey_id=self.cint_survey.pk).exists())
        self.assertFalse(VendorSurveyAllocation.objects.filter(pk=self.survey_allocation.pk).exists())
        self.assertFalse(AllocationReservation.objects.filter(attempt_id=self.cint_attempt.pk).exists())
        self.assertFalse(SyncRun.objects.filter(integration=self.cint_integration).exists())
        self.assertFalse(SyncLease.objects.filter(name__contains=str(self.cint_integration.pk)).exists())

        self.assertTrue(Survey.objects.filter(pk=self.rfg_survey.pk).exists())
        self.assertTrue(SurveyAttempt.objects.filter(pk=self.rfg_attempt.pk).exists())
        self.assertTrue(SyncRun.objects.filter(integration=self.rfg_integration).exists())
        self.assertTrue(
            PrescreenerSubmission.objects.using(DATABASE_ALIAS)
            .filter(rid=self.rfg_attempt.rid)
            .exists()
        )

        self.assertFalse(
            PrescreenerSubmission.objects.using(DATABASE_ALIAS)
            .filter(rid=self.cint_attempt.rid)
            .exists()
        )
        self.assertFalse(
            CintRespondentEmailUse.objects.using(DATABASE_ALIAS)
            .filter(rid=self.cint_attempt.rid)
            .exists()
        )
        self.email_identity.refresh_from_db(using=DATABASE_ALIAS)
        self.assertEqual(self.email_identity.status, CintRespondentEmail.Status.AVAILABLE)
        self.assertIsNone(self.email_identity.assigned_uid)
        self.assertEqual(self.email_identity.use_count, 0)

        self.client_allocation.refresh_from_db()
        self.assertEqual(self.client_allocation.reserved_quantity, 0)
        self.assertEqual(self.client_allocation.consumed_quantity, 0)
        self.cint_integration.refresh_from_db()
        self.assertFalse(self.cint_integration.is_active)
        self.assertFalse(self.cint_integration.scheduled_sync_enabled)
        self.assertEqual(self.cint_integration.last_sync_status, "")

        # The command is deliberately idempotent for safe production retries.
        call_command("purge_cint_data", confirm="DELETE-CINT-DATA")
