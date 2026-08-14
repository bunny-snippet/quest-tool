"""Audit and safely repair gaps between survey attempts and Panelist Data."""

import json
from collections import Counter, defaultdict

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from prescreener_vault.constants import DATABASE_ALIAS
from prescreener_vault.models import PrescreenerSubmission
from prescreener_vault.services import capture_prescreener_submission
from surveys.models import SurveyAttempt


class Command(BaseCommand):
    help = (
        "Compare every operational survey attempt with the isolated Panelist Data vault, "
        "and optionally repair records whose original prescreener answers still exist."
    )

    def add_arguments(self, parser):
        parser.add_argument("--repair", action="store_true")
        parser.add_argument("--batch-size", type=int, default=500)
        parser.add_argument("--after-id", type=int, default=0)
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--show-missing", type=int, default=25)

    def handle(self, *args, **options):
        if not settings.PRESCREENER_VAULT_ENABLED:
            raise CommandError("Set PRESCREENER_VAULT_ENABLED=true before reconciling Panelist Data.")

        batch_size = max(1, options["batch_size"])
        sample_limit = max(0, options["show_missing"])
        totals = Counter()
        samples = defaultdict(list)
        status_counts = Counter()

        def remember(category, attempt, detail=""):
            if len(samples[category]) >= sample_limit:
                return
            samples[category].append({
                "id": attempt.pk,
                "rid": attempt.rid,
                "uid": attempt.prescreener_uid or "",
                "status": attempt.status,
                "detail": detail,
            })

        # Keyset iteration avoids slow OFFSET queries when this reaches millions of attempts.
        last_id = options["after_id"]
        remaining = options["limit"] or None
        while True:
            size = min(batch_size, remaining) if remaining is not None else batch_size
            batch = list(
                SurveyAttempt.objects.select_related("survey")
                .filter(id__gt=last_id)
                .order_by("id")[:size]
            )
            if not batch:
                break
            last_id = batch[-1].pk
            if remaining is not None:
                remaining -= len(batch)

            rids = [attempt.rid for attempt in batch]
            uids = [attempt.prescreener_uid for attempt in batch if attempt.prescreener_uid]
            lookup = Q(rid__in=rids)
            if uids:
                lookup |= Q(uid__in=uids)
            vault_rows = list(
                PrescreenerSubmission.objects.using(DATABASE_ALIAS)
                .filter(lookup)
                .values("uid", "rid")
            )
            by_rid = {row["rid"]: row for row in vault_rows}
            by_uid = {row["uid"]: row for row in vault_rows}

            for attempt in batch:
                totals["scanned"] += 1
                vault_row = by_rid.get(attempt.rid)
                if vault_row:
                    if attempt.prescreener_uid and attempt.prescreener_uid != vault_row["uid"]:
                        totals["identity_conflict"] += 1
                        remember("identity_conflict", attempt, f"vault_uid={vault_row['uid']}")
                        continue
                    if not attempt.prescreener_uid:
                        totals["main_uid_missing"] += 1
                        if options["repair"]:
                            updated = SurveyAttempt.objects.filter(
                                pk=attempt.pk,
                            ).filter(
                                Q(prescreener_uid__isnull=True) | Q(prescreener_uid="")
                            ).update(prescreener_uid=vault_row["uid"])
                            if updated:
                                totals["main_uid_repaired"] += 1
                    totals["linked"] += 1
                    continue

                uid_owner = by_uid.get(attempt.prescreener_uid) if attempt.prescreener_uid else None
                if uid_owner:
                    totals["identity_conflict"] += 1
                    remember("identity_conflict", attempt, f"vault_rid={uid_owner['rid']}")
                    continue

                if attempt.answers:
                    totals["repairable"] += 1
                    remember("repairable", attempt)
                    if options["repair"]:
                        try:
                            capture_prescreener_submission(
                                attempt,
                                attempt.answers,
                                submitted_at=attempt.submitted_at or attempt.initiated_at,
                            )
                            totals["repaired"] += 1
                        except Exception as exc:  # Keep the full audit running after one bad row.
                            totals["repair_failed"] += 1
                            remember("repair_failed", attempt, str(exc))
                    continue

                progressed = bool(
                    attempt.submitted_at
                    or attempt.redirected_at
                    or attempt.callback_at
                    or attempt.status != SurveyAttempt.Status.INITIATED
                )
                if progressed:
                    totals["submitted_payload_missing"] += 1
                    remember("submitted_payload_missing", attempt)
                else:
                    totals["not_submitted"] += 1
                    status_counts[attempt.status] += 1
                    remember("not_submitted", attempt)

            if remaining is not None and remaining <= 0:
                break

        totals["vault_records"] = PrescreenerSubmission.objects.using(DATABASE_ALIAS).count()
        totals["linked_after_repair"] = totals["linked"] + totals["repaired"]
        totals["unlinked_after_repair"] = totals["scanned"] - totals["linked_after_repair"]

        self.stdout.write("Panelist Data reconciliation summary")
        self.stdout.write(json.dumps(dict(totals), indent=2, sort_keys=True))
        if status_counts:
            self.stdout.write("Not-submitted status breakdown")
            self.stdout.write(json.dumps(dict(status_counts), indent=2, sort_keys=True))
        for category, rows in samples.items():
            if rows:
                self.stdout.write(f"{category} samples")
                self.stdout.write(json.dumps(rows, indent=2))

        if totals["submitted_payload_missing"]:
            self.stdout.write(self.style.WARNING(
                "Some progressed attempts have no vault row and no recoverable operational answers; "
                "their questionnaire payload cannot be reconstructed from Traffic metadata alone."
            ))
        if totals["identity_conflict"] or totals["repair_failed"]:
            raise CommandError("Reconciliation found identity conflicts or failed repairs; review the samples above.")
        self.stdout.write(self.style.SUCCESS("Panelist Data reconciliation completed."))
