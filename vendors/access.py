from accounts.models import EmployeeProfile


VENDOR_ACCOUNT_TYPES = {
    EmployeeProfile.AccountType.INTERNAL_VENDOR,
    EmployeeProfile.AccountType.EXTERNAL_VENDOR,
}


def vendor_scope_user_id(user) -> int | None:
    """Return the vendor owning this account, including internal-vendor descendants."""

    if not user or not user.is_authenticated:
        return None
    current = user
    visited: set[int] = set()
    while current and current.pk not in visited:
        visited.add(current.pk)
        profile = EmployeeProfile.objects.select_related("created_by").filter(user=current).first()
        if not profile:
            return None
        if profile.account_type in VENDOR_ACCOUNT_TYPES:
            return current.pk
        current = profile.created_by
    return None
