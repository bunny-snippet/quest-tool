"""Public provider callback endpoints with provider-specific authentication."""

import hashlib
import json

from django.conf import settings
from django.http import JsonResponse
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .cint_webhooks import (
    CintWebhookError,
    delivery_event_key,
    extract_opportunities,
    process_delivery,
    resolve_local_test_integration,
    resolve_signed_integration,
    webhook_receiver_enabled,
)
from .models import CintWebhookDelivery


def _local_test_authorized(request):
    """Permit unsigned replay only from loopback in DEBUG with an explicit token."""

    configured = str(getattr(settings, "CINT_OPPORTUNITIES_LOCAL_TEST_TOKEN", ""))
    supplied = str(request.headers.get("X-Cint-Local-Test-Token") or "")
    remote = str(request.META.get("REMOTE_ADDR") or "")
    return bool(
        settings.DEBUG
        and configured
        and remote in {"127.0.0.1", "::1"}
        and constant_time_compare(configured, supplied)
    )


@csrf_exempt
@require_POST
def cint_opportunities_webhook(request):
    """Authenticate, deduplicate and process a Cint Feed Opportunities batch."""

    if not webhook_receiver_enabled():
        return JsonResponse({"detail": "Cint Opportunities webhook is disabled."}, status=404)
    raw_body = request.body
    maximum = int(getattr(settings, "CINT_OPPORTUNITIES_MAX_PAYLOAD_BYTES", 10 * 1024 * 1024))
    if not raw_body or len(raw_body) > maximum:
        return JsonResponse({"detail": "Webhook payload is empty or too large."}, status=413)
    try:
        payload = json.loads(raw_body.decode("utf-8"))
        rows = extract_opportunities(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, CintWebhookError) as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    maximum_surveys = int(
        getattr(settings, "CINT_OPPORTUNITIES_MAX_SURVEY_COUNT", 1000)
    )
    if len(rows) > maximum_surveys:
        return JsonResponse({"detail": "Webhook contains too many surveys."}, status=413)

    signature_header = str(request.headers.get("X-Lucid-Signature") or "")
    local_test = not signature_header and _local_test_authorized(request)
    try:
        if local_test:
            integration = resolve_local_test_integration()
            signature_timestamp = None
            signature_key_id = "local-test"
        else:
            integration, signature_timestamp, signature_key_id = resolve_signed_integration(
                raw_body,
                signature_header,
            )
    except CintWebhookError as exc:
        return JsonResponse({"detail": str(exc)}, status=401)

    event_key = delivery_event_key(integration.pk, signature_header, raw_body)
    delivery, created = CintWebhookDelivery.objects.get_or_create(
        event_key=event_key,
        defaults={
            "integration": integration,
            "payload_sha256": hashlib.sha256(raw_body).hexdigest(),
            "signature_timestamp": signature_timestamp,
            "signature_key_id": signature_key_id,
            "signature_header": signature_header,
            "payload": payload,
            "item_count": len(rows),
        },
    )
    if not created and delivery.status in {
        CintWebhookDelivery.Status.PROCESSED,
        CintWebhookDelivery.Status.PROCESSING,
        CintWebhookDelivery.Status.RECEIVED,
    }:
        return JsonResponse({
            "status": "duplicate",
            "delivery_id": delivery.pk,
            "delivery_status": delivery.status,
        })

    if getattr(settings, "CINT_OPPORTUNITIES_PROCESS_SYNCHRONOUS", settings.DEBUG):
        delivery = process_delivery(delivery.pk)
        return JsonResponse({
            "status": delivery.status,
            "delivery_id": delivery.pk,
            "received": delivery.item_count,
            "created": delivery.created_count,
            "updated": delivery.updated_count,
            "closed": delivery.closed_count,
            "skipped": delivery.skipped_count,
            "errors": delivery.error_count,
            "local_test": local_test,
        }, status=200 if not delivery.error_count else 500)

    from .tasks import process_cint_opportunities_delivery_task

    try:
        process_cint_opportunities_delivery_task.delay(delivery.pk)
    except Exception as exc:
        delivery.status = CintWebhookDelivery.Status.FAILED
        delivery.error = f"Could not queue webhook processing: {exc}"[:10000]
        delivery.save(update_fields=["status", "error"])
        return JsonResponse({"detail": "Webhook processing is temporarily unavailable."}, status=503)
    return JsonResponse({
        "status": "accepted",
        "delivery_id": delivery.pk,
        "received": delivery.item_count,
    }, status=202)
