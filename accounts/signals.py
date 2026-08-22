"""User lifecycle signal that guarantees an EmployeeProfile row."""

from django.contrib.auth import get_user_model
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .access import invalidate_effective_permission_cache
from .models import (
    AccessFunction,
    EmployeeProfile,
    Role,
    RoleFunctionPermission,
    UserFunctionOverride,
)


@receiver(post_save, sender=get_user_model())
def ensure_employee_profile(sender, instance, created, **kwargs):
    if not created:
        return
    role_slug = "super-admin" if instance.is_superuser else "employee"
    role = Role.objects.filter(slug=role_slug).first()
    EmployeeProfile.objects.get_or_create(user=instance, defaults={"role": role})


def _invalidate_permissions(**kwargs):
    # Incrementing a namespace generation is O(1) and works across every
    # Gunicorn/Celery process. Stale snapshots expire naturally by TTL.
    invalidate_effective_permission_cache()


for permission_model in (
    AccessFunction,
    Role,
    RoleFunctionPermission,
    EmployeeProfile,
    UserFunctionOverride,
):
    post_save.connect(
        _invalidate_permissions,
        sender=permission_model,
        dispatch_uid=f"accounts.permission-cache.save.{permission_model._meta.label_lower}",
    )
    post_delete.connect(
        _invalidate_permissions,
        sender=permission_model,
        dispatch_uid=f"accounts.permission-cache.delete.{permission_model._meta.label_lower}",
    )
