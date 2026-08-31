"""Celery task boundary for scheduled provider work and attempt reconciliation."""

import logging
import os
import shutil
from datetime import timedelta, timezone as dt_timezone
from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.test import RequestFactory
from rest_framework.test import APIRequestFactory, force_authenticate

from vendors.models import ClientIntegration
from vendors.credentials import resolve_integration_token
from vendors.services import finalize_attempt_capacity

from .integrations import InnovateMRAPIError, InnovateMRClient
from .models import CintWebhookDelivery, ExportJob, Survey, SurveyAttempt, SyncLease
from .services import reconcile_attempt_status, replace_survey_details, sync_surveys
from .provider_services import (
    refresh_client_integration_details,
    sync_cint_redirect_contracts,
    sync_client_integration,
)
from .providers import ProviderSurveyUnavailable, get_provider, has_provider
from .rfg_outcomes import RFG_STATUS_MAP, describe_rfg_outcome
from .supplier_callbacks import (
    DELIVERY_AUDIT_KEY,
    SupplierCallbackRetryableError,
    deliver_supplier_result_callback,
    queue_supplier_result_callback,
)


logger = logging.getLogger(__name__)


def _internal_request_host():
    """Use a configured host when rendering an export outside HTTP middleware."""

    return next(
        (host for host in settings.ALLOWED_HOSTS if host and host != "*"),
        "localhost",
    )


def _queued_export_response(job):
    """Run existing export builders with a synthetic authenticated request."""

    # Import lazily: views import tasks when a job is enqueued.
    from .views import (
        SurveyAttemptViewSet,
        SurveyViewSet,
        prescreener_data_export,
        termination_reasons_export,
    )

    query = {str(key): [str(item) for item in values] for key, values in (job.query or {}).items()}
    request_host = _internal_request_host()
    if job.kind == ExportJob.Kind.PANELIST:
        request = RequestFactory().get(
            "/prescreened-data/export/", data=query, HTTP_HOST=request_host,
        )
        request.user = job.requested_by
        return prescreener_data_export(request)
    if job.kind == ExportJob.Kind.TERMS:
        request = RequestFactory().get(
            "/termination-reasons/export/", data=query, HTTP_HOST=request_host,
        )
        request.user = job.requested_by
        return termination_reasons_export(request)
    factory = APIRequestFactory()
    if job.kind == ExportJob.Kind.PROJECTS:
        request = factory.get(
            "/api/v1/surveys/export/", data=query, HTTP_HOST=request_host,
        )
        force_authenticate(request, user=job.requested_by)
        return SurveyViewSet.as_view({"get": "export"})(request)
    if job.kind == ExportJob.Kind.TRAFFIC:
        request = factory.get(
            "/api/v1/survey-attempts/export/", data=query, HTTP_HOST=request_host,
        )
        force_authenticate(request, user=job.requested_by)
        return SurveyAttemptViewSet.as_view({"get": "export"})(request)
    raise ValueError(f"Unsupported export job kind: {job.kind}")


@shared_task(name="surveys.build_export_job", soft_time_limit=270, time_limit=300)
def build_export_job(public_id):
    """Persist a report to protected disk; the browser is no longer involved."""

    try:
        job = ExportJob.objects.select_related("requested_by").get(public_id=public_id)
    except ExportJob.DoesNotExist:
        return {"status": "missing"}
    if job.expires_at <= timezone.now():
        return {"status": "expired"}
    updated = ExportJob.objects.filter(
        pk=job.pk, status=ExportJob.Status.QUEUED,
    ).update(status=ExportJob.Status.RUNNING, started_at=timezone.now(), error="")
    if not updated:
        return {"status": "already-processed"}
    job.refresh_from_db()
    try:
        response = _queued_export_response(job)
        if getattr(response, "status_code", 200) != 200:
            raise RuntimeError(f"Export builder returned HTTP {response.status_code}.")
        workbook = getattr(response, "_export_workbook", None)
        if workbook is None:
            raise RuntimeError("Export builder did not return a workbook.")
        filename = response.get("Content-Disposition", "").split("filename=")[-1].strip('"') or "export.xlsx"
        storage_key = f"{job.public_id}.xlsx"
        directory = Path(settings.EXPORT_JOB_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / f".{storage_key}.tmp-{os.getpid()}"
        destination = directory / storage_key
        workbook.seek(0)
        with temporary.open("wb") as output:
            shutil.copyfileobj(workbook, output, length=1024 * 1024)
        temporary.replace(destination)
        workbook.close()
        ExportJob.objects.filter(pk=job.pk).update(
            status=ExportJob.Status.COMPLETED,
            filename=filename[:255], storage_key=storage_key,
            finished_at=timezone.now(), error="",
        )
        return {"status": "completed", "id": str(job.public_id)}
    except Exception as exc:
        logger.exception("Export job %s failed", public_id)
        ExportJob.objects.filter(pk=job.pk).update(
            status=ExportJob.Status.FAILED,
            error=str(exc)[:500], finished_at=timezone.now(),
        )
        return {"status": "failed", "id": str(job.public_id)}


@shared_task(name="surveys.cleanup_expired_export_jobs")
def cleanup_expired_export_jobs():
    """Remove expired protected workbooks without retaining PII on disk."""

    expired = ExportJob.objects.filter(expires_at__lte=timezone.now())
    keys = list(expired.exclude(storage_key="").values_list("storage_key", flat=True))
    expired.delete()
    directory = Path(settings.EXPORT_JOB_DIR)
    for key in keys:
        path = directory / key
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove expired export file %s", path)
    return {"deleted": len(keys)}


PROVIDER_MINIMUM_SYNC_INTERVAL_SECONDS = {
    "rfg": 600,
}


def effective_sync_interval_seconds(integration):
    """Return the slowest operator, environment and provider-safe interval."""

    configured_floor = {
        "innovatemr": settings.CLIENT_INTEGRATION_INNOVATEMR_SYNC_INTERVAL_SECONDS,
        "rfg": settings.CLIENT_INTEGRATION_RFG_SYNC_INTERVAL_SECONDS,
        "cint": settings.CLIENT_INTEGRATION_CINT_SYNC_INTERVAL_SECONDS,
        "purespectrum": settings.CLIENT_INTEGRATION_PURESPECTRUM_SYNC_INTERVAL_SECONDS,
    }.get(integration.provider_code, 60)
    return max(
        60,
        int(integration.sync_interval_seconds or 60),
        int(configured_floor or 60),
        PROVIDER_MINIMUM_SYNC_INTERVAL_SECONDS.get(integration.provider_code, 60),
    )


def _stale_surveys(integration, limit):
    return Survey.objects.filter(integration=integration, status=Survey.Status.LIVE).filter(
        Q(quota_synced_at__isnull=True)
        | Q(targeting_synced_at__isnull=True)
        | Q(source_modified_at__isnull=False, quota_synced_at__lt=F("source_modified_at"))
        | Q(source_modified_at__isnull=False, targeting_synced_at__lt=F("source_modified_at"))
    ).order_by("detail_synced_at", "-source_modified_at")[:limit]


@shared_task(name="surveys.dispatch_due_integrations")
def dispatch_due_integrations_task():
    now = timezone.now()
    queued = []
    integrations = ClientIntegration.objects.filter(is_active=True).filter(
        Q(client__is_active=True) | Q(provider_code__in=("biobrain", "voqall")),
    ).filter(
        Q(scheduled_sync_enabled=True)
        | Q(provider_code="innovatemr")
        | Q(provider_code="rfg", last_test_status="success")
        | Q(provider_code="cint", last_test_status="success")
        | Q(provider_code="purespectrum", last_test_status="success")
    ).only(
        "id", "provider_code", "sync_interval_seconds", "last_sync_started_at",
        "last_sync_status",
        "credential_env_key", "encrypted_api_token", "config",
    )
    for integration in integrations:
        if (
            integration.provider_code == "cint"
            and (integration.config or {}).get("opportunities_webhook_enabled") is True
        ):
            # Feed Opportunities replaces periodic inventory polling. Manual
            # Sync now remains available for diagnostics/backfills.
            continue
        if integration.provider_code in {"biobrain", "voqall"} and not resolve_integration_token(integration):
            continue
        lease_name = f"integration-{integration.pk}-sync"
        if SyncLease.objects.filter(name=lease_name, locked_until__gt=now).exists():
            continue
        interval_seconds = effective_sync_interval_seconds(integration)
        # A broker delay or slow worker must not make every Beat tick enqueue
        # another copy. Old queue/running markers recover automatically after
        # three effective intervals (and never sooner than the lease window).
        active_sync_cutoff = now - timedelta(seconds=max(300, interval_seconds * 3))
        if (
            integration.last_sync_status in {"queued", "running"}
            and integration.last_sync_started_at
            and integration.last_sync_started_at > active_sync_cutoff
        ):
            continue
        due_at = (integration.last_sync_started_at or (now - timedelta(days=1))) + timedelta(
            seconds=interval_seconds
        )
        if due_at <= now:
            ClientIntegration.objects.filter(pk=integration.pk).update(
                last_sync_started_at=now, last_sync_status="queued", last_sync_error="",
            )
            sync_client_integration_task.delay(integration.pk)
            queued.append(integration.pk)
    return {"queued": queued, "count": len(queued)}


@shared_task(
    name="surveys.sync_client_integration",
    soft_time_limit=240,
    time_limit=270,
)
def sync_client_integration_task(integration_id):
    integration = ClientIntegration.objects.select_related("client").get(pk=integration_id, is_active=True)
    lease_name = f"integration-{integration_id}-sync"
    if not SyncLease.acquire(
        lease_name, seconds=max(300, effective_sync_interval_seconds(integration))
    ):
        return {"status": "skipped", "reason": "previous integration sync is still running"}
    integration.last_sync_started_at = timezone.now()
    integration.last_sync_status = "running"
    integration.last_sync_error = ""
    integration.save(update_fields=["last_sync_started_at", "last_sync_status", "last_sync_error", "updated_at"])
    try:
        if has_provider(integration.provider_code):
            run = sync_client_integration(integration, refresh_details=False)
            details = refresh_client_integration_details(integration)
            summary = {
                "run_id": run.pk,
                "status": run.status,
                "created": run.created,
                "updated": run.updated,
                "unchanged": run.unchanged,
                "closed": run.closed,
                "details_refreshed": details["refreshed"],
                "detail_failures": details["failures"],
            }
            integration.last_sync_status = (
                "success" if run.status == "success" and not details["failures"] else "partial"
            )
            integration.last_sync_summary = summary
            if integration.provider_code == "cint":
                sync_cint_redirects_task.delay(integration.pk)
                summary["redirect_updates_queued"] = True
            return summary
        api = InnovateMRClient(integration=integration)
        summary = sync_surveys(api, integration=integration).__dict__
        inventory_count = sum(int(summary.get(key) or 0) for key in ("created", "updated", "unchanged"))
        if integration.provider_code in {"biobrain", "voqall"} and inventory_count > 0 and not integration.client.is_active:
            integration.client.is_active = True
            integration.client.save(update_fields=["is_active", "updated_at"])
        refreshed = failures = 0
        for survey in _stale_surveys(integration, integration.detail_refresh_batch):
            try:
                replace_survey_details(api, survey)
                refreshed += 1
            except Exception:
                failures += 1
        summary.update({"details_refreshed": refreshed, "detail_failures": failures})
        integration.last_sync_status = "success" if not failures else "partial"
        integration.last_sync_summary = summary
        return summary
    except Exception as exc:
        integration.last_sync_status = "failed"
        integration.last_sync_error = str(exc)[:10000]
        raise
    finally:
        integration.last_sync_finished_at = timezone.now()
        integration.save(update_fields=[
            "last_sync_finished_at", "last_sync_status", "last_sync_error", "last_sync_summary", "updated_at",
        ])
        SyncLease.release(lease_name)


@shared_task(
    bind=True,
    name="surveys.sync_cint_redirects",
    max_retries=15,
    soft_time_limit=240,
    time_limit=270,
)
def sync_cint_redirects_task(
    self,
    integration_id,
    batch_size=25,
    force=False,
    after_id=0,
    survey_ids=None,
):
    """Configure new/backfill Cint survey callbacks in serialized batches."""

    lease_name = f"cint-redirects-{integration_id}"
    if not SyncLease.acquire(lease_name, seconds=300):
        if survey_ids:
            # A webhook-targeted batch must not disappear simply because a
            # larger redirect batch owns the integration lease. Retry the same
            # durable survey IDs with a bounded backoff; the survey's missing
            # contract fields remain the source of truth, and later webhook
            # deliveries can enqueue them again after retries are exhausted.
            retries = max(0, int(getattr(self.request, "retries", 0) or 0))
            raise self.retry(
                exc=RuntimeError("redirect batch already running"),
                countdown=min(30, 2 ** min(retries + 1, 5)),
            )
        return {"status": "skipped", "reason": "redirect batch already running"}
    continue_batching = False
    result = {"next_after_id": max(0, int(after_id or 0))}
    try:
        integration = ClientIntegration.objects.select_related("client").get(
            pk=integration_id,
            is_active=True,
            provider_code="cint",
        )
        result = sync_cint_redirect_contracts(
            integration,
            batch_size=batch_size,
            force=force,
            after_id=after_id,
            survey_ids=survey_ids,
        )
        continue_batching = bool(result.get("has_more", result["remaining"] > 0))
        result["status"] = "success" if not result["failures"] else "partial"
        return result
    finally:
        SyncLease.release(lease_name)
        if continue_batching:
            sync_cint_redirects_task.apply_async(
                args=[integration_id],
                kwargs={
                    "batch_size": batch_size,
                    "force": force,
                    "after_id": result.get("next_after_id", 0),
                    "survey_ids": survey_ids,
                },
                countdown=1,
            )


@shared_task(
    name="surveys.process_cint_opportunities_delivery",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
    soft_time_limit=600,
    time_limit=630,
)
def process_cint_opportunities_delivery_task(delivery_id):
    """Process a verified webhook receipt outside the public HTTP request."""

    from .cint_webhooks import process_delivery

    delivery_ref = CintWebhookDelivery.objects.only("integration_id").get(pk=delivery_id)
    lease_name = f"cint-webhook-{delivery_ref.integration_id}"
    if not SyncLease.acquire(lease_name, seconds=700):
        raise RuntimeError("A previous Cint webhook delivery is still processing.")
    try:
        try:
            delivery = process_delivery(delivery_id)
        except Exception as exc:
            CintWebhookDelivery.objects.filter(pk=delivery_id).update(
                status=CintWebhookDelivery.Status.FAILED,
                error=str(exc)[:10000],
            )
            raise
        recovery = {"processed": [], "failed": []}
        try:
            recovery = _recover_cint_delivery_backlog(
                delivery.integration_id,
                exclude_ids={delivery.pk},
            )
        except Exception:
            # Backlog maintenance is strictly best-effort. It must never
            # relabel a successfully processed live callback as FAILED or
            # delay its retry.
            logger.exception(
                "Cint webhook backlog recovery query failed integration=%s",
                delivery.integration_id,
            )
        return {
            "delivery_id": delivery.pk,
            "status": delivery.status,
            "created": delivery.created_count,
            "updated": delivery.updated_count,
            "closed": delivery.closed_count,
            "skipped": delivery.skipped_count,
            "errors": delivery.error_count,
            "recovery": recovery,
        }
    finally:
        SyncLease.release(lease_name)


def _recover_cint_delivery_backlog(integration_id, *, exclude_ids=(), limit=None):
    """Process a tiny oldest-first recovery slice after a live delivery.

    The live delivery is always handled first so current inventory stays fresh.
    A bounded tail then makes receipts orphaned by worker restarts or exhausted
    lease retries eventually progress without releasing an 8k-task storm. No
    recursive task is scheduled; the next genuine webhook advances the backlog.
    """

    from .cint_webhooks import process_delivery

    batch_size = max(0, min(
        int(
            limit
            if limit is not None
            else getattr(settings, "CINT_OPPORTUNITIES_RECOVERY_BATCH", 1)
        ),
        10,
    ))
    if not batch_size:
        return {"processed": [], "failed": []}
    stale_before = timezone.now() - timedelta(minutes=15)
    candidates = list(
        CintWebhookDelivery.objects.filter(integration_id=integration_id)
        .filter(
            Q(status=CintWebhookDelivery.Status.RECEIVED)
            | Q(
                status=CintWebhookDelivery.Status.PROCESSING,
                received_at__lte=stale_before,
            )
        )
        .exclude(pk__in=set(exclude_ids))
        .only("id", "integration_id", "received_at", "status")
        .order_by("received_at", "pk")[:batch_size]
    )
    processed = []
    failed = []
    for candidate in candidates:
        try:
            process_delivery(candidate.pk)
            processed.append(candidate.pk)
        except Exception as exc:
            # Recovery runs inside the current integration lease, outside the
            # failed delivery's normal Celery retry envelope. Put transient
            # failures back into RECEIVED and enqueue the ordinary task so its
            # bounded autoretries remain authoritative. If the broker is down,
            # the next live callback will select this retained receipt again.
            CintWebhookDelivery.objects.filter(pk=candidate.pk).update(
                status=CintWebhookDelivery.Status.RECEIVED,
                error=str(exc)[:10000],
            )
            failed.append(candidate.pk)
            logger.exception(
                "Cint webhook backlog recovery failed integration=%s delivery=%s",
                integration_id,
                candidate.pk,
            )
            try:
                process_cint_opportunities_delivery_task.delay(candidate.pk)
            except Exception:
                logger.exception(
                    "Could not requeue Cint backlog delivery integration=%s delivery=%s",
                    integration_id,
                    candidate.pk,
                )
    return {"processed": processed, "failed": failed}


@shared_task(name="surveys.sync_innovatemr_surveys")
def sync_innovatemr_surveys_task():
    integration = ClientIntegration.objects.filter(
        is_active=True, client__is_active=True, provider_code="innovatemr"
    ).order_by("id").first()
    if not integration:
        return {"status": "skipped", "reason": "no active integration"}
    return sync_client_integration_task(integration.pk)


@shared_task(name="surveys.refresh_stale_details")
def refresh_stale_details_task():
    integration = ClientIntegration.objects.filter(is_active=True, client__is_active=True).order_by("id").first()
    if not integration:
        return {"status": "skipped", "reason": "no active integration"}
    api = InnovateMRClient(integration=integration)
    refreshed = failures = 0
    for survey in _stale_surveys(integration, integration.detail_refresh_batch):
        try:
            replace_survey_details(api, survey)
            refreshed += 1
        except Exception:
            failures += 1
    return {"refreshed": refreshed, "failures": failures}


@shared_task(name="surveys.reconcile_pending_attempts")
def reconcile_pending_attempts_task():
    lease_name = "innovatemr-attempt-reconciliation"
    if not SyncLease.acquire(lease_name, seconds=300):
        return {"status": "skipped", "reason": "previous attempt reconciliation is still running"}
    now = timezone.now()
    retry_before = now - timedelta(seconds=settings.INNOVATEMR_ATTEMPT_RECONCILE_INTERVAL_SECONDS)
    lookback = now - timedelta(hours=settings.INNOVATEMR_ATTEMPT_RECONCILE_LOOKBACK_HOURS)
    pending = SurveyAttempt.objects.select_related("survey__integration").filter(
        status=SurveyAttempt.Status.REDIRECTED, callback_at__isnull=True, initiated_at__gte=lookback,
    ).exclude(survey__integration__provider_code__in=("rfg", "cint")).filter(
        Q(upstream_checked_at__isnull=True) | Q(upstream_checked_at__lte=retry_before)
    ).order_by(
        "upstream_checked_at", "-initiated_at"
    )[: settings.INNOVATEMR_ATTEMPT_RECONCILE_BATCH]
    clients = {}
    checked = terminal = failures = 0
    try:
        for attempt in pending:
            try:
                integration = attempt.survey.integration
                client = clients.setdefault(integration.pk if integration else None, InnovateMRClient(integration=integration))
                terminal += int(reconcile_attempt_status(client, attempt))
                checked += 1
            except InnovateMRAPIError:
                SurveyAttempt.objects.filter(pk=attempt.pk).update(upstream_checked_at=now)
                failures += 1
        return {"checked": checked, "terminal": terminal, "failures": failures}
    finally:
        SyncLease.release(lease_name)


def _rfg_log_datetime(value):
    """Parse an RFG project-log timestamp into an aware UTC datetime."""

    if not value:
        return None
    parsed = parse_datetime(str(value))
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed.astimezone(dt_timezone.utc)


def _rfg_log_tracking_id(entry):
    """Read only provider-echoed platform tracking identifiers from a log row."""

    for key in ("tid", "TID", "trackId", "trackerId"):
        value = str(entry.get(key) or "").strip()
        if value:
            return value
    return ""


def _rfg_log_loi_seconds(entry, fallback):
    """Use a non-negative provider LOI when available, otherwise local elapsed time."""

    try:
        return max(0, int(entry.get("loi")))
    except (TypeError, ValueError):
        return fallback


def reconcile_rfg_project_log_entries(attempts, log_entries, *, checked_at=None):
    """Apply one project's authenticated RFG log entries to exact pending RIDs.

    The caller owns the provider request.  Keeping the database transition in
    this small shared function lets the public result page and the background
    task use the identical verification, locking and audit path.
    """

    checked_at = checked_at or timezone.now()
    pending_statuses = (SurveyAttempt.Status.INITIATED, SurveyAttempt.Status.REDIRECTED)
    attempt_by_rid = {attempt.rid: attempt for attempt in attempts}
    terminal_entries = {}
    for entry in log_entries:
        tracking_id = _rfg_log_tracking_id(entry)
        result_code = str(entry.get("result") or "").strip()
        if tracking_id not in attempt_by_rid or result_code not in RFG_STATUS_MAP:
            continue
        current = terminal_entries.get(tracking_id)
        if current is None or (_rfg_log_datetime(entry.get("end")) or checked_at) >= (
            _rfg_log_datetime(current.get("end")) or checked_at
        ):
            terminal_entries[tracking_id] = entry

    # A successful authenticated log request is the checkpoint for all
    # attempts in this project, including journeys that are still in progress.
    SurveyAttempt.objects.filter(
        pk__in=[attempt.pk for attempt in attempts],
        status__in=pending_statuses,
        callback_at__isnull=True,
    ).update(upstream_checked_at=checked_at)

    matched = applied = 0
    callback_attempts = []
    for rid, entry in terminal_entries.items():
        result_code = str(entry.get("result")).strip()
        requested_status = RFG_STATUS_MAP[result_code]
        matched += 1
        finished_at = _rfg_log_datetime(entry.get("end")) or checked_at
        with transaction.atomic():
            locked = SurveyAttempt.objects.select_for_update().get(pk=attempt_by_rid[rid].pk)
            if locked.status not in pending_statuses or locked.callback_at is not None:
                continue
            if finished_at < locked.loi_started_at:
                finished_at = checked_at
            audit = locked.upstream_transaction_data if isinstance(locked.upstream_transaction_data, dict) else {}
            outcome = describe_rfg_outcome({"result": result_code})
            # ``sesskey`` is a bearer-like session identifier. Keep enough
            # audit evidence for support without storing it or raw tracking IDs.
            locked.upstream_transaction_data = {
                **audit,
                "rfg_log_reconciliation": {
                    "source": "livealert/log/1",
                    "result": result_code,
                    "started_at": str(entry.get("start") or ""),
                    "ended_at": str(entry.get("end") or ""),
                    "loi_seconds": _rfg_log_loi_seconds(entry, locked.calculate_loi_seconds(finished_at)),
                    "tracking_id_matched": True,
                },
                "rfg_outcome": {
                    key: value for key, value in outcome.items() if key != "sesskey"
                },
            }
            locked.status = requested_status
            locked.callback_at = finished_at
            locked.last_callback_at = checked_at
            locked.loi_seconds = _rfg_log_loi_seconds(
                entry, locked.calculate_loi_seconds(finished_at)
            )
            locked.callback_count += 1
            locked.status_source = "rfg_log_reconciliation"
            locked.is_verified = True
            locked.upstream_checked_at = checked_at
            locked.save(update_fields=[
                "status", "callback_at", "last_callback_at", "loi_seconds", "callback_count",
                "status_source", "is_verified", "upstream_checked_at", "upstream_transaction_data", "updated_at",
            ])
            finalize_attempt_capacity(locked)
            callback_attempts.append(locked)
            applied += 1
    return {"matched": matched, "applied": applied, "callback_attempts": callback_attempts}


@shared_task(
    name="surveys.reconcile_rfg_log_outcomes",
    soft_time_limit=120,
    time_limit=150,
)
def reconcile_rfg_log_outcomes_task():
    """Finalize RFG journeys from the signed LiveAlert project log.

    RFG browser redirects are presentation-only and can be replayed by anyone.
    This task deliberately never finalizes from those redirects.  It accepts an
    outcome only after the RFG-authenticated API echoes the immutable attempt
    RID in ``tid`` (or a documented equivalent tracking ID).
    """

    lease_name = "rfg-log-outcome-reconciliation"
    if not SyncLease.acquire(lease_name, seconds=180):
        return {"status": "skipped", "reason": "previous RFG log reconciliation is still running"}

    now = timezone.now()
    retry_before = now - timedelta(seconds=settings.RFG_LOG_RECONCILE_INTERVAL_SECONDS)
    lookback = now - timedelta(hours=settings.RFG_LOG_RECONCILE_LOOKBACK_HOURS)
    pending_statuses = (SurveyAttempt.Status.INITIATED, SurveyAttempt.Status.REDIRECTED)
    pending = list(
        SurveyAttempt.objects.select_related("survey__integration")
        .filter(
            survey__integration__provider_code="rfg",
            status__in=pending_statuses,
            callback_at__isnull=True,
            initiated_at__gte=lookback,
        )
        .filter(Q(upstream_checked_at__isnull=True) | Q(upstream_checked_at__lte=retry_before))
        .order_by("-initiated_at", "upstream_checked_at", "pk")[: settings.RFG_LOG_RECONCILE_BATCH]
    )
    if not pending:
        SyncLease.release(lease_name)
        return {"checked": 0, "matched": 0, "applied": 0, "failures": 0}

    grouped = {}
    for attempt in pending:
        survey = attempt.survey
        integration = survey.integration
        key = (integration.pk, survey.source_key)
        grouped.setdefault(key, {"integration": integration, "attempts": []})["attempts"].append(attempt)

    checked = matched = applied = failures = 0
    supplier_callback_attempts = []
    try:
        providers = {}
        for group in grouped.values():
            integration = group["integration"]
            attempts = group["attempts"]
            start = min(attempt.initiated_at for attempt in attempts) - timedelta(minutes=5)
            try:
                provider = providers.setdefault(integration.pk, get_provider(integration))
                log_entries = provider.project_log(attempts[0].survey.source_key, start=start, end=now)
            except ProviderSurveyUnavailable:
                # Historical inventory can contain projects that RFG has
                # removed from the authenticated account. Do not burn every
                # two-minute run retrying those dead project IDs; a later
                # inventory refresh/new attempt can still bring them back.
                SurveyAttempt.objects.filter(
                    pk__in=[attempt.pk for attempt in attempts],
                    status__in=pending_statuses,
                    callback_at__isnull=True,
                ).update(upstream_checked_at=now + timedelta(hours=24))
                failures += len(attempts)
                logger.info(
                    "RFG project log unavailable for removed project integration=%s project=%s",
                    integration.pk,
                    attempts[0].survey.source_key,
                )
                continue
            except Exception:
                failures += len(attempts)
                logger.exception(
                    "RFG project-log reconciliation failed integration=%s project=%s",
                    integration.pk,
                    attempts[0].survey.source_key,
                )
                continue

            checked += len(attempts)
            reconciliation = reconcile_rfg_project_log_entries(
                attempts, log_entries, checked_at=now
            )
            matched += reconciliation["matched"]
            applied += reconciliation["applied"]
            supplier_callback_attempts.extend(reconciliation["callback_attempts"])
        for attempt in supplier_callback_attempts:
            queue_supplier_result_callback(attempt)
        return {"checked": checked, "matched": matched, "applied": applied, "failures": failures}
    finally:
        SyncLease.release(lease_name)


@shared_task(
    bind=True,
    name="surveys.deliver_supplier_result_callback",
    max_retries=5,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=45,
    time_limit=60,
)
def deliver_supplier_result_callback_task(self, attempt_id, event_id):
    """Send one persisted supplier result with bounded exponential retries."""

    try:
        return deliver_supplier_result_callback(attempt_id, event_id)
    except SupplierCallbackRetryableError as exc:
        countdown = min(300, 5 * (2 ** int(self.request.retries or 0)))
        raise self.retry(exc=exc, countdown=countdown)


@shared_task(name="surveys.dispatch_pending_supplier_callbacks")
def dispatch_pending_supplier_callbacks_task():
    """Recover callbacks stranded by broker outages or killed workers."""

    now = timezone.now()
    stale_before = now - timedelta(seconds=60)
    lookback = now - timedelta(
        hours=settings.SUPPLIER_CALLBACK_RECOVERY_LOOKBACK_HOURS
    )
    candidates = SurveyAttempt.objects.filter(
        status__in=(
            SurveyAttempt.Status.COMPLETED,
            SurveyAttempt.Status.TERMINATED,
            SurveyAttempt.Status.OVER_QUOTA,
            SurveyAttempt.Status.QUALITY_TERMINATED,
        ),
        callback_at__gte=lookback,
        upstream_transaction_data__supplier_callback_delivery__state__in=(
            "queued", "queue_failed", "delivering",
        ),
    ).only(
        "id", "upstream_transaction_data", "callback_at",
    ).order_by("-callback_at")[: settings.SUPPLIER_CALLBACK_RECOVERY_BATCH]
    queued = []
    failures = 0
    for attempt in candidates:
        audit = attempt.upstream_transaction_data
        record = (
            audit.get(DELIVERY_AUDIT_KEY, {})
            if isinstance(audit, dict)
            else {}
        )
        event_id = str(record.get("event_id") or "")
        if not event_id:
            continue
        updated_at = parse_datetime(str(record.get("updated_at") or ""))
        if (
            record.get("state") != "queue_failed"
            and updated_at
            and updated_at > stale_before
        ):
            continue
        try:
            deliver_supplier_result_callback_task.delay(attempt.pk, event_id)
            queued.append(attempt.pk)
        except Exception as exc:  # pragma: no cover - environment-specific broker failures
            logger.error(
                "Could not recover supplier callback attempt_id=%s error_type=%s",
                attempt.pk,
                type(exc).__name__,
            )
            failures += 1
    return {"queued": queued, "count": len(queued), "failures": failures}
