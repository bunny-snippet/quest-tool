"""Provider-neutral inventory preview, test, upsert and detail-refresh services."""

import logging
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from vendors.models import ClientIntegration

from .models import Survey, SyncRun
from .project_cache import invalidate_project_cache
from .report_cache import invalidate_report_metadata_cache
from .providers import ProviderError, get_provider


logger = logging.getLogger(__name__)
INVENTORY_WRITE_BATCH_SIZE = 250
PROJECT_FILTER_FIELDS = frozenset({
    "client", "company_name", "country", "country_code", "buyer_id",
    "survey_type", "cpi",
})
REPORT_METADATA_FIELDS = frozenset({"client", "buyer_id", "country", "country_code"})


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
        "_cint_redirect_verified_at",
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


def _survey_changed_fields(survey: Survey, normalized, values=None) -> set[str]:
    """Return synchronized fields whose persisted value actually changed."""

    changed = set()
    for field, value in (values or normalized.values).items():
        if field == "last_seen_at":
            continue
        model_field = Survey._meta.get_field(field)
        current_value = getattr(survey, model_field.attname)
        expected_value = (
            getattr(value, "pk", value)
            if model_field.is_relation
            else value
        )
        if current_value != expected_value:
            changed.add(field)
    if survey.raw_data != normalized.raw_data:
        # Provider adapters normally include raw_data in ``values``. Keep the
        # independent comparison used by the old save path so a custom adapter
        # cannot accidentally leave the newest provider payload in memory only.
        normalized.values["raw_data"] = normalized.raw_data
        changed.add("raw_data")
    return changed


def sync_client_integration(integration: ClientIntegration, *, refresh_details=False) -> SyncRun:
    """Synchronize one verified provider connection into its owning client."""
    if not integration.is_active:
        raise ProviderError("This client integration is inactive.")
    provider = get_provider(integration)
    now = timezone.now()
    run = SyncRun.objects.create(integration=integration)
    touched = []
    project_filter_fields_changed = False
    project_count_fields_changed = False
    report_metadata_fields_changed = False
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

        normalized_items = list(normalized_rows.items())
        for offset in range(0, len(normalized_items), INVENTORY_WRITE_BATCH_SIZE):
            batch = normalized_items[offset:offset + INVENTORY_WRITE_BATCH_SIZE]
            changed_surveys = []
            changed_update_fields = set()
            unchanged_surveys = []
            # Short transactions prevent a 3k+ survey inventory response from
            # holding row locks for the entire provider sync.
            with transaction.atomic():
                for source_key, normalized in batch:
                    # ``existing_surveys`` was loaded in one query above. Do not
                    # repeat one SELECT per survey inside the write loop.
                    survey = existing_surveys.get(source_key)
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
                        existing_surveys[source_key] = survey
                        run.created += 1
                        touched.append(survey)
                        project_filter_fields_changed = True
                        project_count_fields_changed = True
                        report_metadata_fields_changed = True
                    else:
                        # Include local ownership/identity fields as well as the
                        # provider payload. Client integrations can be reassigned
                        # through the management API; leaving ``Survey.client``
                        # on its prior owner would put inventory in the wrong
                        # authorization and supplier-allocation scope.
                        changed_fields = _survey_changed_fields(
                            survey,
                            normalized,
                            values,
                        )
                        if not changed_fields:
                            survey.last_seen_at = now
                            survey.updated_at = now
                            unchanged_surveys.append(survey)
                            run.unchanged += 1
                            continue
                        if "raw_data" in changed_fields:
                            values["raw_data"] = normalized.raw_data
                        source_changed = (
                            survey.source_modified_at != normalized.modified_at
                            or survey.raw_data != normalized.raw_data
                        )
                        for field, value in values.items():
                            setattr(survey, field, value)
                        if source_changed:
                            survey.detail_synced_at = None
                            changed_fields.add("detail_synced_at")
                        # bulk_update does not apply auto_now. Preserve the old
                        # save() timestamp contract explicitly while collapsing
                        # hundreds of one-row UPDATEs into bounded statements.
                        survey.updated_at = now
                        changed_fields.update({"last_seen_at", "updated_at"})
                        changed_surveys.append(survey)
                        changed_update_fields.update(changed_fields)
                        run.updated += 1
                        touched.append(survey)
                        # Any changed list field can alter a cached filtered
                        # total (status, text search, dates, CPI, and so on).
                        # One generation bump per completed sync preserves
                        # correctness without returning to per-row churn.
                        project_count_fields_changed = True
                        project_filter_fields_changed = (
                            project_filter_fields_changed
                            or bool(changed_fields & PROJECT_FILTER_FIELDS)
                        )
                        report_metadata_fields_changed = (
                            report_metadata_fields_changed
                            or bool(changed_fields & REPORT_METADATA_FIELDS)
                        )
                if changed_surveys:
                    Survey.objects.bulk_update(
                        changed_surveys,
                        sorted(changed_update_fields),
                        batch_size=INVENTORY_WRITE_BATCH_SIZE,
                    )
                if unchanged_surveys:
                    Survey.objects.bulk_update(
                        unchanged_surveys,
                        ["last_seen_at", "updated_at"],
                        batch_size=INVENTORY_WRITE_BATCH_SIZE,
                    )

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
        if run.closed:
            project_count_fields_changed = True
            report_metadata_fields_changed = True

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
        if project_filter_fields_changed or project_count_fields_changed:
            invalidate_project_cache(
                filters=project_filter_fields_changed,
                counts=project_count_fields_changed,
            )
        if report_metadata_fields_changed:
            # Per-row post_save signals are intentionally bypassed by the bulk
            # update above. One generation bump keeps selectors correct without
            # invalidating them hundreds of times during one inventory run.
            invalidate_report_metadata_cache()
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
    # Quota/targeting detail rows are read from their dedicated endpoints and
    # do not change Project list rows or filter options.  Invalidating the
    # complete Project cache here forced an expensive metadata rebuild after
    # every small detail batch.
    return {"refreshed": refreshed, "failures": failures}


def sync_cint_redirect_contracts(
    integration: ClientIntegration,
    *,
    batch_size=25,
    force=False,
    after_id=0,
    survey_ids=None,
) -> dict:
    """Update one bounded batch of Cint redirects not on the current contract."""

    if integration.provider_code != "cint":
        raise ProviderError("Redirect contract synchronization is only available for Cint.")
    if not integration.is_active:
        raise ProviderError("This Cint integration is inactive.")
    provider = get_provider(integration)
    fingerprint = provider.redirect_contract_fingerprint()
    # Closed/deactivated opportunities cannot accept respondents and Cint may
    # return 404 when their supplier links are no longer available. Keeping
    # them in the backfill queue caused a permanent upstream retry storm.
    base = Survey.objects.filter(
        integration=integration,
        status=Survey.Status.LIVE,
    )
    scoped_ids = None
    if survey_ids is not None:
        scoped_ids = sorted({
            int(survey_id)
            for survey_id in survey_ids
            if str(survey_id).strip().isdigit() and int(survey_id) > 0
        })
        base = base.filter(pk__in=scoped_ids)
    if force:
        # A local fingerprint proves what this application previously sent, not
        # what is currently stored upstream. Force mode deliberately ignores it
        # so externally overwritten/legacy callbacks are reasserted via PUT.
        pending_query = base
    else:
        pending_query = base.filter(
            Q(raw_data___cint_redirect_contract__isnull=True)
            | Q(raw_data___cint_redirect_supplier_code__isnull=True)
            | Q(raw_data___cint_redirect_verified_at__isnull=True)
            | ~Q(raw_data___cint_redirect_contract=fingerprint)
            | ~Q(raw_data___cint_redirect_supplier_code=provider.supplier_code)
        ).filter(
            Q(raw_data___cint_redirect_terminal__isnull=True)
            | Q(raw_data___cint_redirect_terminal=False)
        )
    cursor = max(0, int(after_id or 0))
    pending = pending_query.filter(pk__gt=cursor).order_by("pk")
    limit = max(1, min(int(batch_size), 100))
    # Fetch one sentinel row instead of running two exact COUNTs over JSON
    # predicates. On production's Cint inventory those duplicate scans consumed
    # 6-20 seconds even when there was no redirect work to perform.
    window = list(pending[: limit + 1])
    candidates = window[:limit]
    has_more = len(window) > limit
    updated = failures = retryable_failures = 0
    errors = []
    for survey in candidates:
        try:
            provider.update_supplier_link_redirects(survey)
            updated += 1
        except Exception as exc:
            failures += 1
            errors.append({"survey": survey.source_key, "error": str(exc)[:500]})
            raw_data = dict(survey.raw_data or {})
            raw_data["_cint_redirect_last_error"] = str(exc)[:500]
            raw_data["_cint_redirect_last_failed_at"] = timezone.now().isoformat()
            terminal_failure = (
                getattr(exc, "status_code", None) == 404
                or "(HTTP 404)" in str(exc)
            )
            if terminal_failure:
                # A fresh Cint webhook replaces provider raw data and clears
                # this marker, allowing exactly one new retry if the survey is
                # reactivated or becomes linkable later.
                raw_data["_cint_redirect_terminal"] = True
                raw_data["_cint_redirect_terminal_at"] = timezone.now().isoformat()
            else:
                retryable_failures += 1
            Survey.objects.filter(pk=survey.pk).update(
                raw_data=raw_data,
                updated_at=timezone.now(),
            )
            logger.exception(
                "Cint redirect update failed integration=%s survey=%s",
                integration.pk,
                survey.pk,
            )
    next_after_id = candidates[-1].pk if candidates else cursor
    # Continue past individual failures so one bad upstream survey cannot hold
    # every later webhook opportunity behind it. A later callback/maintenance
    # run starts again at zero and retries any still-pending failed records.
    # The task dispatcher only needs to know whether another bounded batch is
    # required. Avoid the two global JSON COUNT scans while keeping the legacy
    # numeric fields as honest lower bounds: retryable failures in this window
    # remain pending, and the sentinel proves at least one later row exists.
    # Do not label the value exact: concurrent webhooks can add pending rows
    # after the sentinel read, and avoiding a post-update COUNT is the purpose
    # of this hot-path optimization.
    remaining = retryable_failures + int(has_more)
    return {
        "processed": len(candidates),
        "updated": updated,
        "failures": failures,
        "remaining": remaining,
        "pending_total": None,
        "pending_lower_bound": remaining,
        "counts_exact": False,
        "counts_lower_bound": True,
        "has_more": has_more,
        "scoped_survey_count": len(scoped_ids) if scoped_ids is not None else None,
        "force": bool(force),
        "next_after_id": next_after_id,
        "errors": errors,
    }
