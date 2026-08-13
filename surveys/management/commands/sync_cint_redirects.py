"""Queue or synchronously run the one-time/current Cint redirect backfill."""

from django.core.management.base import BaseCommand, CommandError

from surveys.provider_services import sync_cint_redirect_contracts
from surveys.tasks import sync_cint_redirects_task
from vendors.models import ClientIntegration


class Command(BaseCommand):
    help = "Configure current callback redirects for pending Cint survey codes."

    def add_arguments(self, parser):
        parser.add_argument("--integration-id", type=int)
        parser.add_argument("--batch-size", type=int, default=25)
        parser.add_argument(
            "--wait",
            action="store_true",
            help="Run batches in this process instead of queueing Celery work.",
        )

    def handle(self, *args, **options):
        integrations = ClientIntegration.objects.filter(
            provider_code="cint",
            is_active=True,
        ).order_by("pk")
        if options["integration_id"]:
            integrations = integrations.filter(pk=options["integration_id"])
        integrations = list(integrations)
        if not integrations:
            raise CommandError("No active Cint integration was found.")
        batch_size = max(1, min(int(options["batch_size"]), 100))
        for integration in integrations:
            if not options["wait"]:
                sync_cint_redirects_task.delay(integration.pk, batch_size=batch_size)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Queued Cint redirect backfill for integration {integration.pk}."
                    )
                )
                continue
            totals = {"updated": 0, "failures": 0}
            while True:
                result = sync_cint_redirect_contracts(
                    integration,
                    batch_size=batch_size,
                )
                totals["updated"] += result["updated"]
                totals["failures"] += result["failures"]
                self.stdout.write(
                    f"integration={integration.pk} updated={result['updated']} "
                    f"failures={result['failures']} remaining={result['remaining']}"
                )
                if not result["remaining"] or result["failures"] or not result["processed"]:
                    break
            self.stdout.write(self.style.SUCCESS(
                f"Integration {integration.pk}: updated={totals['updated']} "
                f"failures={totals['failures']}."
            ))
