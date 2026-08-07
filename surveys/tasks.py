from celery import shared_task
from django.conf import settings
from django.db.models import F, Q
from django.utils import timezone
from datetime import timedelta

from .integrations import InnovateMRAPIError, InnovateMRClient
from .models import Survey, SurveyAttempt, SyncLease
from .services import reconcile_attempt_status, replace_survey_details, sync_surveys


@shared_task(name="surveys.sync_innovatemr_surveys", autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def sync_innovatemr_surveys_task():
    lease_name = "innovatemr-inventory"
    if not SyncLease.acquire(lease_name, seconds=300):
        return {"status": "skipped", "reason": "previous inventory sync is still running"}
    try:
        return sync_surveys().__dict__
    finally:
        SyncLease.release(lease_name)


@shared_task(name="surveys.refresh_stale_details", autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 2})
def refresh_stale_details_task():
    lease_name = "innovatemr-details"
    if not SyncLease.acquire(lease_name, seconds=300):
        return {"status": "skipped", "reason": "previous detail refresh is still running"}
    stale_surveys = Survey.objects.filter(status=Survey.Status.LIVE).filter(
        Q(quota_synced_at__isnull=True)
        | Q(targeting_synced_at__isnull=True)
        | Q(source_modified_at__isnull=False, quota_synced_at__lt=F("source_modified_at"))
        | Q(source_modified_at__isnull=False, targeting_synced_at__lt=F("source_modified_at"))
    ).order_by("detail_synced_at", "-source_modified_at")[: settings.INNOVATEMR_DETAIL_REFRESH_BATCH]
    try:
        client = InnovateMRClient()
        refreshed = failures = 0
        for survey in stale_surveys:
            try:
                replace_survey_details(client, survey)
                refreshed += 1
            except Exception:
                failures += 1
        return {"refreshed": refreshed, "failures": failures}
    finally:
        SyncLease.release(lease_name)


@shared_task(name="surveys.reconcile_pending_attempts")
def reconcile_pending_attempts_task():
    """Polling fallback for attempts still returning to legacy redirect URLs."""
    lease_name = "innovatemr-attempt-reconciliation"
    if not SyncLease.acquire(lease_name, seconds=300):
        return {"status": "skipped", "reason": "previous attempt reconciliation is still running"}
    now = timezone.now()
    retry_before = now - timedelta(seconds=settings.INNOVATEMR_ATTEMPT_RECONCILE_INTERVAL_SECONDS)
    lookback = now - timedelta(hours=settings.INNOVATEMR_ATTEMPT_RECONCILE_LOOKBACK_HOURS)
    pending = SurveyAttempt.objects.select_related("survey").filter(
        status=SurveyAttempt.Status.REDIRECTED,
        callback_at__isnull=True,
        initiated_at__gte=lookback,
    ).filter(
        Q(upstream_checked_at__isnull=True) | Q(upstream_checked_at__lte=retry_before)
    ).order_by("upstream_checked_at", "-initiated_at")[: settings.INNOVATEMR_ATTEMPT_RECONCILE_BATCH]
    try:
        client = InnovateMRClient()
        checked = terminal = failures = 0
        for attempt in pending:
            try:
                terminal += int(reconcile_attempt_status(client, attempt))
                checked += 1
            except InnovateMRAPIError:
                SurveyAttempt.objects.filter(pk=attempt.pk).update(upstream_checked_at=now)
                failures += 1
        return {"checked": checked, "terminal": terminal, "failures": failures}
    finally:
        SyncLease.release(lease_name)
