"""Code-owned, page-grouped catalog for function-level permissions.

Every new page component must be registered here. ``manage.py migrate``
synchronizes entries into AccessFunction so they appear in both role and
individual-user permission editors. Default grants are applied only when a
function is first created; later administrator changes are never overwritten.
"""

from django.db import transaction


ALL_ROLES = ("employee", "team-lead", "manager", "admin", "super-admin")
TRACKING_ROLES = ("team-lead", "manager", "admin", "super-admin")
ADMIN_ROLES = ("admin", "super-admin")


# code, name, page-wise group, description, default system roles
FUNCTION_CATALOG = (
    ("dashboard.view", "View dashboard page and sidebar item", "Dashboard - Page & navigation", "Open the dashboard and display its sidebar navigation item.", ALL_ROLES),

    ("projects.view", "View Projects page, sidebar and rows", "Projects - Page & navigation", "Open Projects, display its sidebar item and read project rows through the API.", ALL_ROLES),
    ("projects.filter.search", "Use Search filter", "Projects - Filters", "Search project identifiers and descriptive fields.", ALL_ROLES),
    ("projects.filter.country", "Use Country filter", "Projects - Filters", "Filter projects by one or more countries.", ALL_ROLES),
    ("projects.filter.status", "Use Status filter", "Projects - Filters", "Filter projects by one or more statuses.", ALL_ROLES),
    ("projects.filter.client", "Use Client filter", "Projects - Filters", "Filter projects by one or more clients.", ALL_ROLES),
    ("projects.filter.cpi", "Use CPI range and sorting filter", "Projects - Filters", "Filter projects by CPI range and sort by CPI.", ADMIN_ROLES),
    ("projects.filter.date", "Use Date and time filter", "Projects - Filters", "Filter project created or modified timestamps with combined From and To controls.", ALL_ROLES),
    ("projects.filters.clear", "Clear project filters", "Projects - Filters", "Use the Clear filters action on Projects.", ALL_ROLES),
    ("projects.export", "Export projects CSV", "Projects - Actions", "Download all projects matching the permitted current filters.", ALL_ROLES),
    ("survey_details.view", "View survey details", "Projects - Actions", "Open pre-screening and quota details.", ALL_ROLES),
    ("survey_links.copy", "Copy pre-screener links", "Projects - Actions", "Copy internal respondent start links.", ALL_ROLES),
    ("projects.control.page_size", "Change rows per page", "Projects - Page controls", "Change the number of project rows displayed per page.", ALL_ROLES),
    ("projects.control.pagination", "Use pagination", "Projects - Page controls", "Move between pages of project rows.", ALL_ROLES),
    ("projects.column.project_id", "Show Project ID column", "Projects - Table columns", "Display the internal project identifier.", ALL_ROLES),
    ("projects.column.survey", "Show Survey column", "Projects - Table columns", "Display survey ID, name and client.", ALL_ROLES),
    ("projects.column.market", "Show Market column", "Projects - Table columns", "Display country and language.", ALL_ROLES),
    ("projects.column.completes", "Show Completes column", "Projects - Table columns", "Display completion progress and sample size.", ALL_ROLES),
    ("projects.column.cpi", "Show CPI column", "Projects - Table columns", "Display cost per interview.", ALL_ROLES),
    ("projects.column.loi_ir", "Show LOI / IR column", "Projects - Table columns", "Display interview length and incidence rate.", ALL_ROLES),
    ("projects.column.entry_link", "Show Entry link column", "Projects - Table columns", "Display the internal pre-screener copy action.", ALL_ROLES),
    ("projects.column.modified", "Show Modified column", "Projects - Table columns", "Display source timestamp and survey status.", ALL_ROLES),
    ("projects.column.actions", "Show Actions column", "Projects - Table columns", "Display the survey details action.", ALL_ROLES),

    ("attempts.view", "View Studies page, sidebar and rows", "Studies - Page & navigation", "Open Studies, display its sidebar item and read respondent rows through the API.", TRACKING_ROLES),
    ("studies.filter.search", "Use Search filter", "Studies - Filters", "Search respondent, survey, user, network and browser data.", TRACKING_ROLES),
    ("studies.filter.user", "Use User filter", "Studies - Filters", "Filter Studies by one or more users.", TRACKING_ROLES),
    ("studies.filter.status", "Use Status filter", "Studies - Filters", "Filter Studies by one or more outcomes.", TRACKING_ROLES),
    ("studies.filter.date", "Use Date and time filter", "Studies - Filters", "Filter entry or callback timestamps with combined From and To controls.", TRACKING_ROLES),
    ("studies.filters.clear", "Clear study filters", "Studies - Filters", "Use the Clear filters action on Studies.", TRACKING_ROLES),
    ("attempts.export", "Export full Studies CSV", "Studies - Actions", "Export complete respondent audit records matching permitted filters.", ("super-admin",)),
    ("studies.control.page_size", "Change rows per page", "Studies - Page controls", "Change the number of study rows displayed per page.", TRACKING_ROLES),
    ("studies.control.pagination", "Use pagination", "Studies - Page controls", "Move between pages of respondent rows.", TRACKING_ROLES),
    ("studies.column.project_id", "Show Project ID column", "Studies - Table columns", "Display the internal project identifier.", TRACKING_ROLES),
    ("studies.column.survey_id", "Show Survey ID column", "Studies - Table columns", "Display the upstream survey identifier.", TRACKING_ROLES),
    ("studies.column.respondent_id", "Show Respondent ID column", "Studies - Table columns", "Display the internal respondent RID.", TRACKING_ROLES),
    ("studies.column.user", "Show Name column", "Studies - Table columns", "Display respondent user name and email.", TRACKING_ROLES),
    ("studies.column.device", "Show Device column", "Studies - Table columns", "Display the captured device type.", TRACKING_ROLES),
    ("studies.column.ip", "Show IP address column", "Studies - Table columns", "Display captured entry and exit network IPs.", TRACKING_ROLES),
    ("studies.column.loi", "Show LOI column", "Studies - Table columns", "Display measured survey duration.", TRACKING_ROLES),
    ("studies.column.status", "Show Status column", "Studies - Table columns", "Display the normalized survey outcome.", TRACKING_ROLES),
    ("studies.column.start", "Show Start column", "Studies - Table columns", "Display the survey start timestamp.", TRACKING_ROLES),
    ("studies.column.end", "Show End column", "Studies - Table columns", "Display the callback or current end timestamp.", TRACKING_ROLES),

    ("user_hits.view", "View User Hits page, sidebar and rows", "User Hits - Page & navigation", "Open User Hits, display its sidebar item and read aggregated user rows through the API.", TRACKING_ROLES),
    ("user_hits.filter.search", "Use Search filter", "User Hits - Filters", "Search users, email addresses, branches and sub-branches.", TRACKING_ROLES),
    ("user_hits.filter.branch", "Use Branch filter", "User Hits - Filters", "Filter User Hits by one or more branches.", TRACKING_ROLES),
    ("user_hits.filter.sub_branch", "Use Sub-branch filter", "User Hits - Filters", "Filter User Hits by one or more sub-branches.", TRACKING_ROLES),
    ("user_hits.filter.shift", "Use Shift filter", "User Hits - Filters", "Filter User Hits by one or more shifts.", TRACKING_ROLES),
    ("user_hits.filter.user", "Use User filter", "User Hits - Filters", "Filter User Hits by one or more users.", TRACKING_ROLES),
    ("user_hits.filter.date", "Use Date and time filter", "User Hits - Filters", "Filter entry timestamps with combined From and To controls.", TRACKING_ROLES),
    ("user_hits.filters.clear", "Clear User Hits filters", "User Hits - Filters", "Use the Clear filters action on User Hits.", TRACKING_ROLES),
    ("user_hits.summary", "View overview totals", "User Hits - Page controls", "Display hit, complete, conversion and active-user summary cards.", TRACKING_ROLES),
    ("user_hits.control.page_size", "Change rows per page", "User Hits - Page controls", "Change the number of user-hit rows displayed per page.", TRACKING_ROLES),
    ("user_hits.control.pagination", "Use pagination", "User Hits - Page controls", "Move between pages of user-hit rows.", TRACKING_ROLES),
    ("user_hits.column.branch", "Show Branch column", "User Hits - Table columns", "Display the user's branch.", TRACKING_ROLES),
    ("user_hits.column.sub_branch", "Show Sub-branch column", "User Hits - Table columns", "Display the user's sub-branch.", TRACKING_ROLES),
    ("user_hits.column.shift", "Show Shift column", "User Hits - Table columns", "Display the user's assigned shift.", TRACKING_ROLES),
    ("user_hits.column.user", "Show User column", "User Hits - Table columns", "Display user identity details.", TRACKING_ROLES),
    ("user_hits.column.date", "Show Date column", "User Hits - Table columns", "Display the IST calendar date.", TRACKING_ROLES),
    ("user_hits.column.hits", "Show Hits column", "User Hits - Table columns", "Display total and device-wise hits.", TRACKING_ROLES),
    ("user_hits.column.completes", "Show Completes column", "User Hits - Table columns", "Display total and device-wise completes.", TRACKING_ROLES),

    ("organization.view", "View Organization page and hierarchy", "Organization - Page & navigation", "Open Organization and read the permitted Branch, Sub-branch and Shift tree.", ADMIN_ROLES),
    ("organization.summary", "View organization overview", "Organization - Page controls", "Display hierarchy, member and client-grant totals.", ADMIN_ROLES),
    ("organization.tab.structure", "View Structure tab", "Organization - Tabs", "Display Branch, Sub-branch and Shift hierarchy.", ADMIN_ROLES),
    ("organization.tab.client_access", "View Client access tab", "Organization - Tabs", "Display client visibility assignments for hierarchy units.", ADMIN_ROLES),
    ("organization.tab.clients", "View Client catalog tab", "Organization - Tabs", "Display the platform client catalog.", ("super-admin",)),
    ("organization.manage", "Manage organization units", "Organization - Actions", "Create, edit and deactivate Branch, Sub-branch and Shift units.", ADMIN_ROLES),
    ("organization.clients.manage", "Manage unit client access", "Organization - Actions", "Assign eligible clients to Branch, Sub-branch or Shift units.", ADMIN_ROLES),
    ("organization.action.create_unit", "Create organization unit", "Organization - Actions", "Open and submit the Branch, Sub-branch or Shift creation modal.", ADMIN_ROLES),
    ("organization.action.edit_unit", "Edit organization unit", "Organization - Actions", "Edit or deactivate an organization unit.", ADMIN_ROLES),
    ("organization.action.assign_client", "Assign unit client", "Organization - Actions", "Create or edit a unit-level client visibility grant.", ADMIN_ROLES),
    ("organization.column.unit.path", "Show Unit path column", "Organization - Structure columns", "Display the full Branch / Sub-branch / Shift path.", ADMIN_ROLES),
    ("organization.column.unit.type", "Show Unit type column", "Organization - Structure columns", "Display the hierarchy level.", ADMIN_ROLES),
    ("organization.column.unit.members", "Show Members column", "Organization - Structure columns", "Display assigned user totals.", ADMIN_ROLES),
    ("organization.column.unit.clients", "Show Clients column", "Organization - Structure columns", "Display direct client grant totals.", ADMIN_ROLES),
    ("organization.column.unit.status", "Show Unit status column", "Organization - Structure columns", "Display active or inactive state.", ADMIN_ROLES),
    ("organization.column.unit.actions", "Show Unit actions column", "Organization - Structure columns", "Display unit editing actions.", ADMIN_ROLES),
    ("organization.column.access.unit", "Show Client-access unit column", "Organization - Client access columns", "Display the assigned organization unit.", ADMIN_ROLES),
    ("organization.column.access.client", "Show Client-access client column", "Organization - Client access columns", "Display the visible client.", ADMIN_ROLES),
    ("organization.column.access.status", "Show Client-access status column", "Organization - Client access columns", "Display client-grant state.", ADMIN_ROLES),
    ("organization.column.access.actions", "Show Client-access actions column", "Organization - Client access columns", "Display client-grant editing actions.", ADMIN_ROLES),

    ("sync.view", "View synchronization history", "Synchronization", "View inventory synchronization runs.", ("manager", "admin", "super-admin")),
    ("sync.run", "Run synchronization", "Projects - Actions", "Manually trigger an inventory synchronization.", ADMIN_ROLES),
    ("permissions.view", "View permission catalog", "Access control", "View functions that may be delegated.", TRACKING_ROLES),
    ("roles.view", "View roles", "Access control", "View roles available within the user's scope.", TRACKING_ROLES),
    ("roles.create", "Create subordinate roles", "Access control", "Create roles from grantable functions.", ADMIN_ROLES),
    ("roles.update", "Update owned roles", "Access control", "Update roles created by the current user.", ADMIN_ROLES),
    ("roles.delete", "Delete owned roles", "Access control", "Delete unused roles created by the current user.", ADMIN_ROLES),
    ("users.view", "View subordinate users", "Access control", "View users created below the current account.", TRACKING_ROLES),
    ("users.create", "Create subordinate users", "Access control", "Create permitted subordinate accounts.", ("manager", "admin", "super-admin")),
    ("users.update", "Update subordinate users", "Access control", "Update roles and individual overrides.", ("manager", "admin", "super-admin")),
    ("users.delete", "Delete subordinate users", "Access control", "Delete permitted subordinate accounts.", ADMIN_ROLES),
    ("users.manage", "Manage employees", "Access control", "Legacy employee-management access.", ADMIN_ROLES),
    ("access.manage", "Manage roles and permissions", "Access control", "Configure functions, roles and user overrides.", ("super-admin",)),
    ("api_docs.view", "View API documentation", "Development", "Open internal Swagger documentation.", ADMIN_ROLES),
    ("clients.view", "View clients", "Vendor Management - Page & navigation", "View client and integration metadata.", ("super-admin",)),
    ("clients.manage", "Manage clients", "Vendor Management - Actions", "Create and update client metadata.", ("super-admin",)),
    ("clients.integration.view", "View client integrations", "Organization - Client integrations", "View non-secret provider connection metadata and status.", ("super-admin",)),
    ("clients.integration.manage", "Create and edit integrations", "Organization - Client integrations", "Create, update, pause or deactivate client integration settings.", ("super-admin",)),
    ("clients.integration.test", "Test integration connection", "Organization - Client integrations", "Run a safe provider authentication test.", ("super-admin",)),
    ("clients.integration.preview", "Preview provider inventory", "Organization - Client integrations", "Fetch a bounded read-only provider inventory preview.", ("super-admin",)),
    ("clients.integration.sync", "Run client integration sync", "Organization - Client integrations", "Queue an immediate provider inventory synchronization.", ("super-admin",)),
    ("vendors.view", "View Vendor Management page and sidebar", "Vendor Management - Page & navigation", "Open Vendor Management and display its sidebar item.", ("super-admin",)),
    ("vendors.manage", "Manage vendor policies", "Vendor Management - Actions", "Configure vendor CPI and delivery policies.", ("super-admin",)),
    ("allocations.view", "View allocations", "Vendor Management - Page & navigation", "View client and project quantity allocations.", ("super-admin",)),
    ("allocations.manage", "Manage allocations", "Vendor Management - Actions", "Create and update client and project caps.", ("super-admin",)),
    ("respondents.create", "Create vendor respondents", "Access control - Actions", "Create respondents below an internal vendor.", ("admin", "super-admin")),
    ("vendors.summary", "View overview totals", "Vendor Management - Page controls", "Display vendor, allocation, quantity and project summary cards.", ("super-admin",)),
    ("vendors.tab.policies", "View Vendors tab", "Vendor Management - Tabs", "Display the Vendors commercial-policy tab.", ("super-admin",)),
    ("vendors.tab.clients", "View Client allocations tab", "Vendor Management - Tabs", "Display client allocation records.", ("super-admin",)),
    ("vendors.tab.projects", "View Project allocations tab", "Vendor Management - Tabs", "Display project allocation records.", ("super-admin",)),
    ("vendors.tab.api_keys", "View API keys tab", "Vendor Management - Tabs", "Display external vendor API keys.", ("super-admin",)),
    ("vendors.action.create", "Create vendor", "Vendor Management - Actions", "Open Access Control to create a vendor account.", ("super-admin",)),
    ("vendors.action.edit_policy", "Edit vendor policy", "Vendor Management - Actions", "Create or edit vendor commercial policy records.", ("super-admin",)),
    ("vendors.action.allocate_client", "Allocate client", "Vendor Management - Actions", "Create or edit client allocation records.", ("super-admin",)),
    ("vendors.action.allocate_project", "Allocate project", "Vendor Management - Actions", "Create or edit project allocation records.", ("super-admin",)),
    ("vendors.action.create_api_key", "Generate API key", "Vendor Management - Actions", "Issue a new external vendor API key.", ("super-admin",)),
    ("vendors.action.revoke_api_key", "Revoke API key", "Vendor Management - Actions", "Revoke an external vendor API key.", ("super-admin",)),
    ("vendors.column.vendor.name", "Show Vendor column", "Vendor Management - Vendor columns", "Display vendor identity.", ("super-admin",)),
    ("vendors.column.vendor.type", "Show Type column", "Vendor Management - Vendor columns", "Display internal or external vendor type.", ("super-admin",)),
    ("vendors.column.vendor.cpi", "Show Default CPI column", "Vendor Management - Vendor columns", "Display the vendor CPI policy.", ("super-admin",)),
    ("vendors.column.vendor.clients", "Show Clients column", "Vendor Management - Vendor columns", "Display allocated-client counts.", ("super-admin",)),
    ("vendors.column.vendor.status", "Show Status column", "Vendor Management - Vendor columns", "Display vendor policy status.", ("super-admin",)),
    ("vendors.column.vendor.actions", "Show vendor Actions column", "Vendor Management - Vendor columns", "Display vendor policy row actions.", ("super-admin",)),
    ("vendors.column.client.vendor", "Show Vendor column", "Vendor Management - Client allocation columns", "Display the allocated vendor.", ("super-admin",)),
    ("vendors.column.client.client", "Show Client column", "Vendor Management - Client allocation columns", "Display the allocated client.", ("super-admin",)),
    ("vendors.column.client.quantity", "Show Quantity column", "Vendor Management - Client allocation columns", "Display client capacity usage.", ("super-admin",)),
    ("vendors.column.client.cpi", "Show CPI cut column", "Vendor Management - Client allocation columns", "Display the effective client CPI cut.", ("super-admin",)),
    ("vendors.column.client.window", "Show Active window column", "Vendor Management - Client allocation columns", "Display allocation start and end timestamps.", ("super-admin",)),
    ("vendors.column.client.actions", "Show client Actions column", "Vendor Management - Client allocation columns", "Display client allocation row actions.", ("super-admin",)),
    ("vendors.column.project.vendor", "Show Vendor column", "Vendor Management - Project allocation columns", "Display the project vendor.", ("super-admin",)),
    ("vendors.column.project.survey", "Show Survey column", "Vendor Management - Project allocation columns", "Display project and survey identifiers.", ("super-admin",)),
    ("vendors.column.project.client", "Show Client column", "Vendor Management - Project allocation columns", "Display the project client.", ("super-admin",)),
    ("vendors.column.project.quantity", "Show Quantity column", "Vendor Management - Project allocation columns", "Display project capacity usage.", ("super-admin",)),
    ("vendors.column.project.cpi", "Show CPI cut column", "Vendor Management - Project allocation columns", "Display the effective project CPI cut.", ("super-admin",)),
    ("vendors.column.project.actions", "Show project Actions column", "Vendor Management - Project allocation columns", "Display project allocation row actions.", ("super-admin",)),
    ("vendors.column.api.vendor", "Show Vendor column", "Vendor Management - API key columns", "Display the API-key owner.", ("super-admin",)),
    ("vendors.column.api.key", "Show Key column", "Vendor Management - API key columns", "Display masked API-key metadata.", ("super-admin",)),
    ("vendors.column.api.created", "Show Created column", "Vendor Management - API key columns", "Display API-key creation time.", ("super-admin",)),
    ("vendors.column.api.last_used", "Show Last used column", "Vendor Management - API key columns", "Display the last API-key use time.", ("super-admin",)),
    ("vendors.column.api.expires", "Show Expires column", "Vendor Management - API key columns", "Display API-key expiration time.", ("super-admin",)),
    ("vendors.column.api.actions", "Show API-key Actions column", "Vendor Management - API key columns", "Display API-key revoke actions.", ("super-admin",)),
)


@transaction.atomic
def sync_access_function_catalog(**_kwargs):
    """Synchronize code-defined functions without resetting configured access."""

    from .models import AccessFunction, Role, RoleFunctionPermission

    for code, name, module, description, default_roles in FUNCTION_CATALOG:
        function, created = AccessFunction.objects.update_or_create(
            code=code,
            defaults={"name": name, "module": module, "description": description, "is_active": True},
        )
        if not created:
            continue
        for role in Role.objects.filter(slug__in=default_roles, is_active=True):
            RoleFunctionPermission.objects.get_or_create(
                role=role,
                function=function,
                defaults={"allowed": True},
            )
