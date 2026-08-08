from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission

from .models import AccessFunction, EmployeeProfile, Role, UserFunctionOverride


def effective_permission_codes(user) -> set[str]:
    if not user or not user.is_authenticated or not user.is_active:
        return set()
    if user.is_superuser:
        return set(AccessFunction.objects.filter(is_active=True).values_list("code", flat=True))

    profile = EmployeeProfile.objects.select_related("role").filter(user=user).first()
    codes: set[str] = set()
    if profile and profile.role and profile.role.is_active:
        codes.update(
            profile.role.function_assignments.filter(allowed=True, function__is_active=True)
            .values_list("function__code", flat=True)
        )
    for code, effect in user.function_overrides.filter(function__is_active=True).values_list("function__code", "effect"):
        if effect == UserFunctionOverride.Effect.ALLOW:
            codes.add(code)
        else:
            codes.discard(code)
    return codes


def has_function_access(user, code: str) -> bool:
    return bool(user and user.is_authenticated and user.is_active and (user.is_superuser or code in effective_permission_codes(user)))


def function_permission_required(code: str):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not has_function_access(request.user, code):
                raise PermissionDenied(f"You do not have access to {code}.")
            return view_func(request, *args, **kwargs)

        return wrapped
    return decorator


def any_function_permission_required(*codes: str):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not any(has_function_access(request.user, code) for code in codes):
                raise PermissionDenied("You do not have access to this management area.")
            return view_func(request, *args, **kwargs)
        return wrapped
    return decorator


def subordinate_user_ids(user) -> set[int]:
    if not user or not user.is_authenticated:
        return set()
    if user.is_superuser:
        from django.contrib.auth import get_user_model
        return set(get_user_model().objects.values_list("id", flat=True))
    descendants: set[int] = set()
    frontier = {user.id}
    while frontier:
        children = set(EmployeeProfile.objects.filter(created_by_id__in=frontier).values_list("user_id", flat=True)) - descendants
        descendants.update(children)
        frontier = children
    descendants.discard(user.id)
    return descendants


def assignable_functions(user):
    queryset = AccessFunction.objects.filter(is_active=True)
    return queryset if user.is_superuser else queryset.filter(code__in=effective_permission_codes(user))


def assignable_roles(user):
    if user.is_superuser:
        return Role.objects.filter(is_active=True)
    permitted = effective_permission_codes(user)
    role_ids = []
    for role in Role.objects.filter(is_active=True).prefetch_related("function_assignments__function"):
        role_codes = {item.function.code for item in role.function_assignments.all() if item.allowed and item.function.is_active}
        if role_codes.issubset(permitted):
            role_ids.append(role.id)
    return Role.objects.filter(id__in=role_ids)


def can_manage_role(user, role) -> bool:
    return bool(user.is_superuser or (not role.is_system and role.created_by_id == user.id))


class HasFunctionPermission(BasePermission):
    message = "Your account does not have access to this function."

    def has_permission(self, request, view):
        resolver = getattr(view, "get_required_function_permission", None)
        codes = resolver() if resolver else getattr(view, "required_function_permission", None)
        if isinstance(codes, str):
            codes = (codes,)
        return bool(codes and any(has_function_access(request.user, code) for code in codes))
