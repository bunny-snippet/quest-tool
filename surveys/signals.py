"""Invalidate Projects metadata when access/pricing configuration changes."""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from accounts.models import EmployeeProfile, RoleFunctionPermission, UserFunctionOverride
from vendors.models import (
    OrganizationClientAccess,
    VendorClientAllocation,
    VendorCommercialProfile,
    VendorSurveyAllocation,
)

from .project_cache import invalidate_project_cache


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
