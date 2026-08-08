# Authentication and function access

Internal workspace pages use Django session authentication. Public respondent URLs (`/survey/start` and `/survey`) intentionally remain unauthenticated because InnovateMR respondents and callbacks must reach them.

## Resolution order

Access is calculated for a function code such as `projects.view`:

1. inactive or anonymous users are denied;
2. Django superusers are allowed;
3. the user's active role supplies its explicitly allowed functions;
4. a per-user `allow` override adds a function;
5. a per-user `deny` override removes a function and wins over the role.

Ranks are display metadata only. Roles do not inherit from lower ranks, so a Team Lead does not automatically receive an Employee function unless it is explicitly assigned.

## Delegated vendor hierarchy

An account may be an Employee, Internal vendor or External vendor. Every account records who created it, forming a subordinate tree. Super Admin can see the entire tree; a delegated vendor can only view or modify users below its own account.

Role and user management use granular functions such as `roles.create`, `users.create`, `users.update` and `users.delete`. A delegated user can only grant functions present in their own effective access, preventing privilege escalation. Custom roles also record their creator; non-superusers may update or delete only roles they created. System roles cannot be deleted.

The Access Control page performs user, role and permission assignment in responsive on-screen modals. The REST APIs remain the backend contract and enforce the same scope rules independently of the UI.

Navigation is rendered from effective access: denied Dashboard, Projects, Access Control and API documentation links are omitted instead of leading to a forbidden page. The post-login landing route selects the first page the user can actually access.

Projects presentation has independent `projects.column.*` functions for Project ID, Survey, Market, Completes, CPI, LOI/IR, Entry link, Modified and Actions. Default roles receive every column; a role assignment or per-user deny can remove any column from both desktop tables and mobile cards. Entry link and Actions additionally require their underlying copy-link and survey-detail functions.

`projects.export` independently controls the Projects CSV button and export API. `projects.filter.cpi` independently controls CPI sorting/range UI and rejects direct CPI filter API parameters when denied. These functions, like every table-column function, can be assigned on a role and then allowed or denied for one user.

The code-owned catalog in `accounts/function_catalog.py` is synchronized after `manage.py migrate`. New capabilities added there automatically become `AccessFunction` records and appear in both role defaults and individual user override editors. Default grants are applied only when a function is first created, so later administrator customizations are not reset by deployments.

## Initial roles

- Employee
- Team Lead
- Manager
- Admin
- Super Admin

All accounts created without an explicit role default to Employee. The one-time `/setup/` route creates the first Django superuser and assigns the Super Admin role; it permanently closes as soon as any user exists.

## Management

Users with `access.manage` can manage the role and function catalog at `/api/v1/access/roles/` and `/api/v1/access/functions/`. Users with `users.manage` can manage accounts, roles and per-user overrides at `/api/v1/access/users/`.

New protected Django views use `@function_permission_required("function.code")`. DRF views use `HasFunctionPermission` and declare `required_function_permission`, or resolve it by action.
