"""Provider-neutral inventory preview, test, upsert and detail-refresh services."""

import logging
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from vendors.models import ClientIntegration

from .models import Survey, SyncRun
from .project_cache import invalidate_project_cache
from .providers import ProviderError, get_provider


logger = logging.getLogger(__name__)


def _preserve_provider_local_state(integration, survey, normalized):
    """Keep provider state hydrated outside inventory when list rows omit it."""
    if integration.provider_code != "cint" or survey is None:
        return
    # Cint's inventory endpoints commonly omit SupplierLink/Target. A later
    # detail hydration retrieves or creates that link, so an inventory refresh
    # must not erase it while a respondent is completing the pre-screener.
    if not normalized.values.get("entry_link") and survey.entry_link:
        normalized.values["entry_link"] = survey.entry_link
    if not normalized.values.get("test_entry_link") and survey.test_entry_link:
        normalized.values["test_entry_link"] = survey.test_entry_link
    local_raw_data = survey.raw_data or {}
    for key in (
        "_cint_supplier_link",
        "_cint_redirect_contract",
        "_cint_redirect_synced_at",
        "_cint_redirect_supplier_code",
        "_cint_redirect_method",
    ):
        value = local_raw_data.get(key)
        if value not in (None, "") and key not in normalized.raw_data:
            normalized.raw_data[key] = value
        normalized.values["raw_data"] = normalized.raw_data


def provider_preview(integration: ClientIntegration, limit: int = 10) -> dict:
    """Fetch a bounded, read-only inventory preview without changing local surveys."""
    provider = get_provider(integration)
    seen_at = timezone.now()
    rows = []
    inventory = provider.inventory()
    for payload in inventory[: max(1, min(int(limit), 25))]:
        normalized = provider.normalize_inventory_item(payload, seen_at)
        rows.append({
            "source_id": normalized.source_key,
            "name": normalized.values.get("name", ""),
            "country": normalized.values.get("country_code", ""),
            "cpi": normalized.values.get("cpi"),
            "loi": normalized.values.get("loi"),
            "status": normalized.values.get("status"),
            "modified_at": normalized.modified_at,
        })
    return {"total_received": len(inventory), "results": rows}


def test_provider_connection(integration: ClientIntegration) -> dict:
    now = timezone.now()
    try:
        result = get_provider(integration).test_connection()
    except Exception as exc:
        ClientIntegration.objects.filter(pk=integration.pk).update(
            last_tested_at=now,
            last_test_status="failed",
            last_test_error=str(exc)[:2000],
            scheduled_sync_enabled=False,
        )
        raise
    ClientIntegration.objects.filter(pk=integration.pk).update(
        last_tested_at=now,
        last_test_status="success",
        last_test_error="",
        scheduled_sync_enabled=True,
        sync_interval_seconds=60,
    )
    return result


def _survey_changed(survey: Survey, normalized) -> bool:
    if survey.raw_data != normalized.raw_data:
        return True
    return any(
        getattr(survey, field) != value
        for field, value in normalized.values.items()
        if field != "last_seen_at"
    )


def sync_client_integration(integration: ClientIntegration, *, refresh_details=False) -> SyncRun:
    """Synchronize one verified provider connection into its owning client."""
    if not integration.is_active:
        raise ProviderError("This client integration is inactive.")
    provider = get_provider(integration)
    now = timezone.now()
    run = SyncRun.objects.create(integration=integration)
    touched = []
    try:
        inventory = provider.inventory()
        run.fetched_full = len(inventory)
        prepared_rows = [
            provider.normalize_inventory_item(payload, now)
            for payload in inventory
        ]
        source_keys = [row.source_key for row in prepared_rows]
        # Load only rows represented by this response; never materialize an
        # integration's entire historical inventory into worker memory.
        existing_surveys = {
            survey.source_key: survey
            for survey in Survey.objects.filter(
                integration=integration,
                source_key__in=source_keys,
            )
        }
        normalized_rows = {}
        for normalized in prepared_rows:
            existing = existing_surveys.get(normalized.source_key)
            try:
                normalized = provider.prepare_inventory_item(normalized, existing)
            except Exception:
                run.detail_failures += 1
                logger.exception(
                    "Provider pre-persistence preparation failed integration=%s survey=%s",
                    integration.pk,
                    normalized.source_key,
                )
                continue
            normalized_rows[normalized.source_key] = normalized
        run.unique_surveys = len(normalized_rows)
        run.detail_failures += len(getattr(provider, "inventory_failures", []))

        with transaction.atomic():
            for source_key, normalized in normalized_rows.items():
                survey = Survey.objects.filter(integration=integration, source_key=source_key).first()
                _preserve_provider_local_state(integration, survey, normalized)
                values = {
                    **normalized.values,
                    "client": integration.client,
                    "integration": integration,
                    "source_key": source_key,
                    "source_id": normalized.numeric_source_id,
                }
                if survey is None:
                    survey = Survey.objects.create(**values)
                    run.created += 1
                    touched.append(survey)
                elif _survey_changed(survey, normalized):
                    source_changed = (
                        survey.source_modified_at != normalized.modified_at
                        or survey.raw_data != normalized.raw_data
                    )
                    for field, value in values.items():
                        setattr(survey, field, value)
                    if source_changed:
                        survey.detail_synced_at = None
                    survey.save()
                    run.updated += 1
                    touched.append(survey)
                else:
                    survey.last_seen_at = now
                    survey.integration = integration
                    survey.save(update_fields=["last_seen_at", "integration", "updated_at"])
                    run.unchanged += 1

            if provider.close_missing_inventory_items:
                run.closed = Survey.objects.filter(
                    integration=integration,
                    status=Survey.Status.LIVE,
                ).exclude(source_key__in=normalized_rows).update(
                    status=Survey.Status.CLOSED,
                    updated_at=now,
                )
            else:
                # Cint open opportunities disappear after link creation. Only
                # rows explicitly rejected by the current CPI/locale policy are
                # closed here; allocated rows absent from the feed stay live.
                run.closed = Survey.objects.filter(
                    integration=integration,
                    status=Survey.Status.LIVE,
                    source_key__in=getattr(provider, "rejected_source_keys", set()),
                ).update(status=Survey.Status.CLOSED, updated_at=now)

        if refresh_details:
            detail_batch = int((integration.config or {}).get("detail_refresh_batch", integration.detail_refresh_batch))
            candidates = touched[: max(0, min(detail_batch, 50))]
            for survey in candidates:
                try:
                    provider.refresh_details(survey)
                except Exception:
                    run.detail_failures += 1
                    logger.exception("Provider detail refresh failed for integration=%s survey=%s", integration.pk, survey.pk)
        run.status = SyncRun.Status.PARTIAL if run.detail_failures else SyncRun.Status.SUCCESS
    except Exception as exc:
        run.status = SyncRun.Status.FAILED
        run.error = str(exc)[:10000]
        ClientIntegration.objects.filter(pk=integration.pk).update(last_test_error=str(exc)[:2000])
        logger.exception("Provider sync failed for integration=%s", integration.pk)
        raise
    finally:
        finished = timezone.now()
        run.finished_at = finished
        run.save()
        ClientIntegration.objects.filter(pk=integration.pk).update(
            last_sync_finished_at=finished,
            last_sync_status={
                SyncRun.Status.SUCCESS: "success",
                SyncRun.Status.PARTIAL: "partial",
                SyncRun.Status.FAILED: "failed",
            }.get(run.status, run.status),
            last_sync_error=run.error,
            last_sync_summary={
                "run_id": run.pk,
                "created": run.created,
                "updated": run.updated,
                "unchanged": run.unchanged,
                "closed": run.closed,
                "detail_failures": run.detail_failures,
            },
        )
    if run.status in {SyncRun.Status.SUCCESS, SyncRun.Status.PARTIAL}:
        invalidate_project_cache()
    return run


def refresh_client_integration_details(integration: ClientIntegration, *, limit=None) -> dict:
    """Refresh changed provider targeting/link data outside the inventory transaction."""
    if not integration.is_active:
        raise ProviderError("This client integration is inactive.")
    provider = get_provider(integration)
    requested = limit if limit is not None else (integration.config or {}).get(
        "detail_refresh_batch", integration.detail_refresh_batch
    )
    batch = max(1, min(int(requested), 20))
    candidates = Survey.objects.filter(
        integration=integration,
        status=Survey.Status.LIVE,
    )
    if integration.provider_code == "cint":
        # Cint's list endpoints do not expose a modified timestamp and quota
        # capacity is real-time, so rotate through the oldest detail snapshots.
        candidates = candidates.order_by("detail_synced_at", "pk")[:batch]
    else:
        candidates = candidates.filter(
            detail_synced_at__isnull=True
        ).order_by("-source_modified_at", "pk")[:batch]
    refreshed = failures = 0
    for survey in candidates:
        try:
            provider.refresh_details(survey)
            refreshed += 1
        except Exception:
            failures += 1
            logger.exception("Provider detail refresh failed for integration=%s survey=%s", integration.pk, survey.pk)
    if refreshed:
        invalidate_project_cache()
    return {"refreshed": refreshed, "failures": failures}


def sync_cint_redirect_contracts(integration: ClientIntegration, *, batch_size=25) -> dict:
    """Update one bounded batch of Cint redirects not on the current contract."""

    if integration.provider_code != "cint":
        raise ProviderError("Redirect contract synchronization is only available for Cint.")
    if not integration.is_active:
        raise ProviderError("This Cint integration is inactive.")
    provider = get_provider(integration)
    fingerprint = provider.redirect_contract_fingerprint()
    pending = Survey.objects.filter(integration=integration).filter(
        Q(raw_data___cint_redirect_contract__isnull=True)
        | Q(raw_data___cint_redirect_supplier_code__isnull=True)
        | ~Q(raw_data___cint_redirect_contract=fingerprint)
        | ~Q(raw_data___cint_redirect_supplier_code=provider.supplier_code)
    ).order_by("pk")
    candidates = list(pending[: max(1, min(int(batch_size), 100))])
    updated = failures = 0
    errors = []
    for survey in candidates:
        try:
            provider.update_supplier_link_redirects(survey)
            updated += 1
        except Exception as exc:
            failures += 1
            errors.append({"survey": survey.source_key, "error": str(exc)[:500]})
            logger.exception(
                "Cint redirect update failed integration=%s survey=%s",
                integration.pk,
                survey.pk,
            )
    remaining = Survey.objects.filter(integration=integration).filter(
        Q(raw_data___cint_redirect_contract__isnull=True)
        | Q(raw_data___cint_redirect_supplier_code__isnull=True)
        | ~Q(raw_data___cint_redirect_contract=fingerprint)
        | ~Q(raw_data___cint_redirect_supplier_code=provider.supplier_code)
    ).count()
    return {
        "processed": len(candidates),
        "updated": updated,
        "failures": failures,
        "remaining": remaining,
        "errors": errors,
    }
