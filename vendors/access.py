from accounts.models import EmployeeProfile


VENDOR_ACCOUNT_TYPES = {
    EmployeeProfile.AccountType.INTERNAL_VENDOR,
    EmployeeProfile.AccountType.EXTERNAL_VENDOR,
}


def vendor_scope_user_id(user) -> int | None:
    """Return the vendor owning this account, including internal-vendor descendants."""

    if not user or not user.is_authenticated:
        return None
    if hasattr(user, "_vendor_scope_user_id_cache"):
        return user._vendor_scope_user_id_cache
    current = user
    visited: set[int] = set()
    while current and current.pk not in visited:
        visited.add(current.pk)
        profile = EmployeeProfile.objects.select_related("created_by").filter(user=current).first()
        if not profile:
            user._vendor_scope_user_id_cache = None
            return None
        if profile.account_type in VENDOR_ACCOUNT_TYPES:
            user._vendor_scope_user_id_cache = current.pk
            return current.pk
        current = profile.created_by
    user._vendor_scope_user_id_cache = None
    return None


def is_external_vendor_scope(user) -> bool:
    """Whether the current account is an external vendor or one of its descendants."""

    if not user or not user.is_authenticated:
        return False
    if hasattr(user, "_external_vendor_scope_cache"):
        return user._external_vendor_scope_cache
    vendor_id = vendor_scope_user_id(user)
    result = bool(vendor_id and EmployeeProfile.objects.filter(
        user_id=vendor_id,
        account_type=EmployeeProfile.AccountType.EXTERNAL_VENDOR,
    ).exists())
    user._external_vendor_scope_cache = result
    return result
