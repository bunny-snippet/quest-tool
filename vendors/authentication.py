"""Authenticate external supplier API requests using stored key hashes."""

from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from accounts.models import EmployeeProfile

from .models import VendorAPIKey
from .security import digest_api_key
from .access import is_valid_supplier_profile


class VendorAPIKeyAuthentication(BaseAuthentication):
    """Authenticate an external supplier using X-API-Key or Authorization: Api-Key."""

    keyword = "Api-Key"

    def authenticate(self, request):
        raw_key = request.META.get("HTTP_X_API_KEY", "").strip()
        authorization = request.META.get("HTTP_AUTHORIZATION", "").strip()
        if not raw_key and authorization:
            parts = authorization.split(None, 1)
            if len(parts) == 2 and parts[0].lower() == self.keyword.lower():
                raw_key = parts[1].strip()
        if not raw_key:
            return None

        api_key = VendorAPIKey.objects.select_related(
            "vendor", "vendor__employee_profile", "vendor__vendor_commercial_profile"
        ).filter(key_hash=digest_api_key(raw_key)).first()
        now = timezone.now()
        if not api_key or not api_key.is_active or api_key.revoked_at:
            raise AuthenticationFailed("Invalid or revoked supplier API key.")
        if api_key.expires_at and api_key.expires_at <= now:
            raise AuthenticationFailed("Vendor API key has expired.")
        vendor = api_key.vendor
        if not vendor.is_active:
            raise AuthenticationFailed("Vendor account is inactive.")
        profile = getattr(vendor, "employee_profile", None)
        if not is_valid_supplier_profile(profile) or profile.account_type != EmployeeProfile.AccountType.EXTERNAL_VENDOR:
            raise AuthenticationFailed("API key is not assigned to an external supplier.")
        commercial = getattr(vendor, "vendor_commercial_profile", None)
        if not commercial or not commercial.is_active or not commercial.api_access_enabled:
            raise AuthenticationFailed("API delivery is not enabled for this vendor.")

        # Avoid turning a frequently used supplier key into a hot write-locked
        # row. The audit timestamp remains current within the configured
        # interval and authentication itself is still checked on every call.
        write_interval = timedelta(
            seconds=settings.VENDOR_API_KEY_LAST_USED_WRITE_INTERVAL_SECONDS
        )
        cutoff = now - write_interval
        if api_key.last_used_at is None or api_key.last_used_at < cutoff:
            # Keep the predicate in SQL as well as the inexpensive Python gate:
            # the gate removes the UPDATE statement on normal hot-key traffic,
            # while the predicate prevents concurrent stale readers from all
            # writing the same row.
            updated = VendorAPIKey.objects.filter(pk=api_key.pk).filter(
                Q(last_used_at__isnull=True) | Q(last_used_at__lt=cutoff)
            ).update(last_used_at=now)
            if updated:
                api_key.last_used_at = now
        return vendor, api_key

    def authenticate_header(self, request):
        return self.keyword
