"""Code-owned catalog for function-level permissions.

Add every new UI/API capability here. ``manage.py migrate`` synchronizes new
entries into AccessFunction so they automatically appear in role and user
permission editors. Default grants are only applied when a function is first
created; later administrator changes are never overwritten.
"""

from django.db import transaction


# code, name, module, description, default system roles
FUNCTION_CATALOG = (
    ("dashboard.view", "View dashboard", "Dashboard", "Open the internal dashboard.", ("employee", "team-lead", "manager", "admin", "super-admin")),
    ("projects.view", "View projects", "Projects · Page", "Browse the synchronized survey inventory.", ("employee", "team-lead", "manager", "admin", "super-admin")),
    ("projects.export", "Export projects CSV", "Projects · Actions", "Download all projects matching the current filters.", ("employee", "team-lead", "manager", "admin", "super-admin")),
    ("projects.filter.cpi", "Use CPI filter and sorting", "Projects · Filters", "Filter projects by CPI range and sort by CPI.", ("admin", "super-admin")),
    ("survey_details.view", "View survey details", "Projects · Actions", "Open pre-screening and quota details.", ("employee", "team-lead", "manager", "admin", "super-admin")),
    ("survey_links.copy", "Copy pre-screener links", "Projects · Actions", "Copy internal respondent start links.", ("employee", "team-lead", "manager", "admin", "super-admin")),
    ("projects.column.project_id", "Show Project ID column", "Projects · Table columns", "Display the internal project identifier.", ("employee", "team-lead", "manager", "admin", "super-admin")),
    ("projects.column.survey", "Show Survey column", "Projects · Table columns", "Display survey ID, name and client.", ("employee", "team-lead", "manager", "admin", "super-admin")),
    ("projects.column.market", "Show Market column", "Projects · Table columns", "Display country and language.", ("employee", "team-lead", "manager", "admin", "super-admin")),
    ("projects.column.completes", "Show Completes column", "Projects · Table columns", "Display completion progress and sample size.", ("employee", "team-lead", "manager", "admin", "super-admin")),
    ("projects.column.cpi", "Show CPI column", "Projects · Table columns", "Display cost per interview.", ("employee", "team-lead", "manager", "admin", "super-admin")),
    ("projects.column.loi_ir", "Show LOI / IR column", "Projects · Table columns", "Display interview length and incidence rate.", ("employee", "team-lead", "manager", "admin", "super-admin")),
    ("projects.column.entry_link", "Show Entry link column", "Projects · Table columns", "Display the internal pre-screener copy action.", ("employee", "team-lead", "manager", "admin", "super-admin")),
    ("projects.column.modified", "Show Modified column", "Projects · Table columns", "Display source timestamp and survey status.", ("employee", "team-lead", "manager", "admin", "super-admin")),
    ("projects.column.actions", "Show Actions column", "Projects · Table columns", "Display the survey details action.", ("employee", "team-lead", "manager", "admin", "super-admin")),
    ("attempts.view", "View survey tracking", "Tracking", "View respondent attempts, IPs, statuses and LOI.", ("team-lead", "manager", "admin", "super-admin")),
    ("attempts.export", "Export survey tracking", "Tracking", "Export respondent tracking records.", ("super-admin",)),
    ("user_hits.view", "View user hits", "Tracking", "View date-wise hits and completes by device.", ("team-lead", "manager", "admin", "super-admin")),
    ("sync.view", "View synchronization history", "Synchronization", "View inventory synchronization runs.", ("manager", "admin", "super-admin")),
    ("sync.run", "Run synchronization", "Synchronization", "Manually trigger an inventory synchronization.", ("admin", "super-admin")),
    ("permissions.view", "View permission catalog", "Access control", "View functions that may be delegated.", ("team-lead", "manager", "admin", "super-admin")),
    ("roles.view", "View roles", "Access control", "View roles available within the user's scope.", ("team-lead", "manager", "admin", "super-admin")),
    ("roles.create", "Create subordinate roles", "Access control", "Create roles from grantable functions.", ("admin", "super-admin")),
    ("roles.update", "Update owned roles", "Access control", "Update roles created by the current user.", ("admin", "super-admin")),
    ("roles.delete", "Delete owned roles", "Access control", "Delete unused roles created by the current user.", ("admin", "super-admin")),
    ("users.view", "View subordinate users", "Access control", "View users created below the current account.", ("team-lead", "manager", "admin", "super-admin")),
    ("users.create", "Create subordinate users", "Access control", "Create permitted subordinate accounts.", ("manager", "admin", "super-admin")),
    ("users.update", "Update subordinate users", "Access control", "Update roles and individual overrides.", ("manager", "admin", "super-admin")),
    ("users.delete", "Delete subordinate users", "Access control", "Delete permitted subordinate accounts.", ("admin", "super-admin")),
    ("users.manage", "Manage employees", "Access control", "Legacy employee-management access.", ("admin", "super-admin")),
    ("access.manage", "Manage roles and permissions", "Access control", "Configure functions, roles and user overrides.", ("super-admin",)),
    ("api_docs.view", "View API documentation", "Development", "Open internal Swagger documentation.", ("admin", "super-admin")),
)


@transaction.atomic
def sync_access_function_catalog(**_kwargs):
    """Synchronize code-defined functions without resetting configured access."""

    from .models import AccessFunction, Role, RoleFunctionPermission

    for code, name, module, description, default_roles in FUNCTION_CATALOG:
        function, created = AccessFunction.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "module": module,
                "description": description,
                "is_active": True,
            },
        )
        if not created:
            continue
        for role in Role.objects.filter(slug__in=default_roles, is_active=True):
            RoleFunctionPermission.objects.get_or_create(
                role=role,
                function=function,
                defaults={"allowed": True},
            )
