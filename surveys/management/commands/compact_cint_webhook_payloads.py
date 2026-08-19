"""Compact successfully processed Cint webhook bodies in bounded batches."""

import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from surveys.models import CintWebhookDelivery


class Command(BaseCommand):
    help = (
        "Replace retained payload JSON with [] for successfully processed Cint "
        "deliveries while preserving hashes, signatures, counters and timestamps."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply compaction. Without this flag the command is a dry run.",
        )
        parser.add_argument(
            "--older-than-hours",
            type=int,
            default=24,
            help="Compact processed deliveries at least this many hours old (default: 24).",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=5,
            help="Large JSON rows updated per transaction (default: 5, maximum: 50).",
        )
        parser.add_argument(
            "--pause-ms",
            type=int,
            default=250,
            help="Pause between batches to limit MySQL I/O pressure (default: 250).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Optional maximum number of deliveries to compact in this run.",
        )

    def handle(self, *args, **options):
        older_than_hours = max(0, int(options["older_than_hours"]))
        batch_size = max(1, min(int(options["batch_size"]), 50))
        pause_seconds = max(0, int(options["pause_ms"])) / 1000
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
            f"cutoff={cutoff.isoformat()} batch_size={batch_size}"
        )
        if not options["apply"]:
            self.stdout.write(
                self.style.WARNING("Dry run only; add --apply to compact payloads.")
            )
            return

        compacted = 0
        while compacted < selected:
            take = min(batch_size, selected - compacted)
            ids = list(queryset.values_list("pk", flat=True)[:take])
            if not ids:
                break
            updated = CintWebhookDelivery.objects.filter(
                pk__in=ids,
                status=CintWebhookDelivery.Status.PROCESSED,
            ).exclude(payload=[]).update(payload=[])
            compacted += updated
            self.stdout.write(f"compacted={compacted}/{selected}")
            if pause_seconds and compacted < selected:
                time.sleep(pause_seconds)
        self.stdout.write(self.style.SUCCESS(f"Compacted {compacted} payloads."))
