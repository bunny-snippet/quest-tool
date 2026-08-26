"""Invalidate Projects metadata when access/pricing configuration changes."""

from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from accounts.models import EmployeeProfile, RoleFunctionPermission, UserFunctionOverride
from vendors.models import (
    OrganizationClientAccess,
    VendorAPIKey,
    VendorClientAllocation,
    VendorCommercialProfile,
    VendorSurveyAllocation,
)

from .project_cache import invalidate_project_cache
from .report_cache import invalidate_report_metadata_cache
from .models import Survey, SurveyAttempt


@receiver(post_save, sender=EmployeeProfile)
@receiver(post_save, sender=RoleFunctionPermission)
@receiver(post_save, sender=UserFunctionOverride)
@receiver(post_save, sender=OrganizationClientAccess)
@receiver(post_save, sender=VendorClientAllocation)
@receiver(post_save, sender=VendorCommercialProfile)
@receiver(post_save, sender=VendorSurveyAllocation)
@receiver(post_delete, sender=EmployeeProfile)
@receiver(post_delete, sender=RoleFunctionPermission)
@receiver(post_delete, sender=UserFunctionOverride)
@receiver(post_delete, sender=OrganizationClientAccess)
@receiver(post_delete, sender=VendorClientAllocation)
@receiver(post_delete, sender=VendorCommercialProfile)
@receiver(post_delete, sender=VendorSurveyAllocation)
def invalidate_projects_after_scope_change(**kwargs):
    invalidate_project_cache()
    invalidate_report_metadata_cache()


@receiver(post_save, sender=SurveyAttempt)
def invalidate_report_metadata_after_attempt_change(*, created, update_fields, **kwargs):
    """Refresh supplier/client selectors only when their dimensions can change."""

    changed = set(update_fields or ())
    if created or not update_fields or changed & {"vendor", "survey", "platform_user"}:
        invalidate_report_metadata_cache()


@receiver(post_delete, sender=SurveyAttempt)
@receiver(post_delete, sender=Survey)
def invalidate_report_metadata_after_inventory_delete(**kwargs):
    invalidate_report_metadata_cache()


@receiver(post_save, sender=Survey)
def invalidate_report_metadata_after_inventory_change(*, created, update_fields, **kwargs):
    changed = set(update_fields or ())
    if created or not update_fields or changed & {
        "client", "buyer_id", "country", "country_code"
    }:
        invalidate_report_metadata_cache()


@receiver(m2m_changed, sender=VendorAPIKey.client_allocations.through)
def invalidate_project_counts_after_api_key_scope_change(*, action, **kwargs):
    """Expire key-scoped pagination totals when its client grants change."""

    if action in {"post_add", "post_remove", "post_clear"}:
        invalidate_project_cache(filters=False, counts=True)
