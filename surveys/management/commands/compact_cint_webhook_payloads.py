"""Report retained Cint webhook bodies without modifying source data."""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from surveys.models import CintWebhookDelivery


class Command(BaseCommand):
    help = (
        "Read-only report of retained successfully processed Cint webhook bodies. "
        "Payload deletion/compaction is intentionally disabled."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Disabled safety flag retained only for backward compatibility.",
        )
        parser.add_argument(
            "--older-than-hours",
            type=int,
            default=24,
            help="Report processed deliveries at least this many hours old (default: 24).",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=5,
            help="Deprecated compatibility option; no rows are modified.",
        )
        parser.add_argument(
            "--pause-ms",
            type=int,
            default=250,
            help="Deprecated compatibility option; no rows are modified.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Optional maximum number of deliveries included in the report.",
        )

    def handle(self, *args, **options):
        older_than_hours = max(0, int(options["older_than_hours"]))
        limit = max(0, int(options["limit"]))
        cutoff = timezone.now() - timedelta(hours=older_than_hours)
        queryset = (
            CintWebhookDelivery.objects.filter(
                status=CintWebhookDelivery.Status.PROCESSED,
                processed_at__lte=cutoff,
            )
            .exclude(payload=[])
            .order_by("pk")
        )
        pending = queryset.count()
        selected = min(pending, limit) if limit else pending
        self.stdout.write(
            f"processed_payloads={pending} selected={selected} "
            f"cutoff={cutoff.isoformat()}"
        )
        if options["apply"]:
            raise CommandError(
                "Cint payload compaction is disabled: source webhook data must not "
                "be deleted or replaced. No rows were modified."
            )
        self.stdout.write(
            self.style.SUCCESS(
                "Read-only report complete. Every retained payload remains unchanged."
            )
        )
