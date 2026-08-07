from celery import shared_task
from django.conf import settings
from django.db.models import F, Q

from .integrations import InnovateMRClient
from .models import Survey, SyncLease
from .services import replace_survey_details, sync_surveys


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
