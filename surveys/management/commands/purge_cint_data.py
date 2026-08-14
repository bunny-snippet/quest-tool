"""Permanently remove Cint inventory and respondent history from both databases."""

from itertools import islice

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from prescreener_vault.cache import invalidate_vault_cache
from prescreener_vault.constants import DATABASE_ALIAS
from prescreener_vault.models import (
    CintRespondentEmail,
    CintRespondentEmailUse,
    PrescreenerSubmission,
)
from surveys.models import CintWebhookDelivery, Survey, SurveyAttempt, SyncLease, SyncRun
from surveys.project_cache import invalidate_project_cache
from vendors.models import (
    AllocationReservation,
    ClientIntegration,
    VendorClientAllocation,
    VendorSurveyAllocation,
)


CONFIRMATION = "DELETE-CINT-DATA"
CHUNK_SIZE = 1000


def _chunks(values, size=CHUNK_SIZE):
    iterator = iter(values)
    while batch := list(islice(iterator, size)):
        yield batch


class Command(BaseCommand):
    help = (
        "Permanently purge Cint surveys, attempts, allocations, sync history and "
        "linked prescreener-vault data while retaining paused integration credentials."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm",
            help=f"Required destructive confirmation: {CONFIRMATION}",
        )

    def _scope(self):
        integrations = ClientIntegration.objects.filter(provider_code="cint")
        integration_ids = list(integrations.values_list("pk", flat=True))
        surveys = Survey.objects.filter(
            Q(integration_id__in=integration_ids)
            | Q(client__provider_code="cint")
        ).distinct()
        return integrations, integration_ids, surveys

    @staticmethod
    def _attempts(surveys):
        # The explicit client fallback also catches early/legacy Cint attempts
        # whose survey relation was not normalized correctly.
        return SurveyAttempt.objects.filter(
            Q(survey__in=surveys) | Q(client__provider_code="cint")
        ).distinct()

    def _counts(self, integrations, surveys):
        attempts = self._attempts(surveys)
        rids = list(attempts.values_list("rid", flat=True))
        uids = list(
            attempts.exclude(prescreener_uid__isnull=True)
            .exclude(prescreener_uid="")
            .values_list("prescreener_uid", flat=True)
        )
        vault_submissions = 0
        email_uses = 0
        identities = 0
        for batch in _chunks(rids):
            vault_submissions += PrescreenerSubmission.objects.using(
                DATABASE_ALIAS
            ).filter(rid__in=batch).count()
            email_uses += CintRespondentEmailUse.objects.using(
                DATABASE_ALIAS
            ).filter(rid__in=batch).count()
        for batch in _chunks(uids):
            identities += CintRespondentEmail.objects.using(DATABASE_ALIAS).filter(
                assigned_uid__in=batch
            ).count()
        return {
            "integrations": integrations.count(),
            "surveys": surveys.count(),
            "attempts": attempts.count(),
            "survey_allocations": VendorSurveyAllocation.objects.filter(
                survey__in=surveys
            ).count(),
            "reservations": AllocationReservation.objects.filter(
                attempt__in=attempts
            ).count(),
            "sync_runs": SyncRun.objects.filter(
                integration__in=integrations
            ).count(),
            "webhook_deliveries": CintWebhookDelivery.objects.filter(
                integration__in=integrations
            ).count(),
            "vault_submissions": vault_submissions,
            "email_uses": email_uses,
            "email_identities_to_release": identities,
            "rids": rids,
            "uids": uids,
        }

    def handle(self, *args, **options):
        integrations, integration_ids, surveys = self._scope()
        counts = self._counts(integrations, surveys)
        self.stdout.write("Cint purge scope:")
        for key in (
            "integrations", "surveys", "attempts", "survey_allocations",
            "reservations", "sync_runs", "webhook_deliveries", "vault_submissions", "email_uses",
            "email_identities_to_release",
        ):
            self.stdout.write(f"  {key}={counts[key]}")

        if options.get("confirm") != CONFIRMATION:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run only. Re-run with --confirm {CONFIRMATION} to permanently delete."
                )
            )
            return

        # Pause first. Deployment instructions stop beat/worker as well, while
        # this database state prevents a restarted scheduler from refilling old data.
        integrations.update(
            is_active=False,
            scheduled_sync_enabled=False,
            last_test_status="",
            last_test_error="",
            last_sync_status="",
            last_sync_error="",
            last_sync_summary={},
            last_sync_started_at=None,
            last_sync_finished_at=None,
        )

        # Vault and operational databases cannot share one transaction. Purge
        # the linked vault rows first; the command remains safely repeatable if
        # the operational deletion is interrupted. Small transactions avoid a
        # long-lived MySQL lock when the production history is large.
        identity_ids = set()
        for batch in _chunks(counts["uids"]):
            with transaction.atomic(using=DATABASE_ALIAS):
                identity_ids.update(
                    CintRespondentEmail.objects.using(DATABASE_ALIAS)
                    .filter(assigned_uid__in=batch)
                    .values_list("pk", flat=True)
                )
        for batch in _chunks(counts["rids"]):
            with transaction.atomic(using=DATABASE_ALIAS):
                CintRespondentEmailUse.objects.using(DATABASE_ALIAS).filter(
                    rid__in=batch
                ).delete()
                PrescreenerSubmission.objects.using(DATABASE_ALIAS).filter(
                    rid__in=batch
                ).delete()
        for batch in _chunks(sorted(identity_ids)):
            with transaction.atomic(using=DATABASE_ALIAS):
                CintRespondentEmailUse.objects.using(DATABASE_ALIAS).filter(
                    identity_id__in=batch
                ).delete()
                CintRespondentEmail.objects.using(DATABASE_ALIAS).filter(
                    pk__in=batch
                ).update(
                    assigned_uid=None,
                    status=CintRespondentEmail.Status.AVAILABLE,
                    use_count=0,
                    assigned_at=None,
                    first_used_at=None,
                    last_used_at=None,
                )

        current_surveys = Survey.objects.filter(
            Q(integration_id__in=integration_ids)
            | Q(client__provider_code="cint")
        ).distinct()
        attempts = self._attempts(current_surveys)
        survey_ids = list(current_surveys.values_list("pk", flat=True))
        attempt_ids = list(attempts.values_list("pk", flat=True))
        client_ids = set(
            integrations.values_list("client_id", flat=True)
        )
        client_ids.update(
            current_surveys.exclude(client_id__isnull=True)
            .values_list("client_id", flat=True)
        )
        client_ids.update(
            attempts.exclude(client_id__isnull=True)
            .values_list("client_id", flat=True)
        )

        for batch in _chunks(attempt_ids):
            with transaction.atomic():
                AllocationReservation.objects.filter(attempt_id__in=batch).delete()
                SurveyAttempt.objects.filter(pk__in=batch).delete()
        for batch in _chunks(survey_ids):
            with transaction.atomic():
                VendorSurveyAllocation.objects.filter(survey_id__in=batch).delete()
                Survey.objects.filter(pk__in=batch).delete()
        for batch in _chunks(integration_ids):
            with transaction.atomic():
                SyncRun.objects.filter(integration_id__in=batch).delete()
                CintWebhookDelivery.objects.filter(integration_id__in=batch).delete()

        with transaction.atomic():
            VendorClientAllocation.objects.filter(client_id__in=client_ids).update(
                consumed_quantity=0,
                reserved_quantity=0,
            )
            SyncLease.objects.filter(
                Q(name__in=[f"integration-{pk}-sync" for pk in integration_ids])
                | Q(name__in=[f"cint-redirects-{pk}" for pk in integration_ids])
            ).delete()

        invalidate_vault_cache()
        invalidate_project_cache()
        self.stdout.write(self.style.SUCCESS(
            "Cint data permanently purged. Integrations and credentials were retained but paused."
        ))
