# Vendor, client, quantity and CPI operations

This feature is additive and isolated on the UAT branch. Vendor allocation is enforced in project listing, copied-link validation, respondent initiation, callback finalization and legacy callback reconciliation. Ordinary non-vendor accounts continue to use the original inventory flow.

## Account rules

- `EmployeeProfile.account_type` remains the source of truth for employee, internal-vendor and external-vendor identity.
- Only an account with `vendors.manage` may create vendor accounts.
- An internal vendor with `respondents.create` may create employee/respondent children only.
- An external vendor is always terminal and cannot create children, even if a permission is accidentally allowed.
- Branch/company and sub-branch/department apply to the internal hierarchy. External vendors store neither value and User Hits reports branch as not applicable.

## Data hierarchy

1. `Client` identifies a buyer/source account.
2. `ClientIntegration` stores non-secret upstream connection metadata. It stores the environment-variable name for a credential, never the token.
3. `VendorCommercialProfile` stores a vendor's default CPI cut and currency.
4. `VendorClientAllocation` grants client visibility and limits total quantity across that client's surveys.
5. `VendorSurveyAllocation` is an optional survey-specific block/limit inside the parent client allocation and may override CPI cut. If it is absent, the client grant applies to every available survey for that client.
6. `AllocationReservation` records the reserved, consumed, released or expired quantity associated with one survey attempt.

## CPI precedence and snapshot

For external vendors, cut precedence is survey override, client override, then vendor default. Internal vendors always receive a zero-percent cut. External-vendor project and tracking APIs do not expose source CPI; they return payable CPI and the applied cut. On reservation, `SurveyAttempt` freezes:

- vendor and client;
- client and survey allocation IDs;
- source CPI;
- applied cut percentage;
- payable CPI; and
- currency.

Changing the live survey CPI later cannot change an existing attempt snapshot.

## Quantity lifecycle

The reservation service locks the client row and any optional survey row in one database transaction. Capacity is available only when client remaining, an applicable survey override remaining and upstream survey remaining are all positive.

- Initiation: reserve one client unit and, when configured, one survey-override unit.
- Status `1`: move the reserved unit to consumed.
- Status `2`, `3` or `4`: release the reserved unit.
- Abandoned attempt: `vendors.expire_allocation_reservations` runs every `VENDOR_RESERVATION_CLEANUP_INTERVAL_SECONDS` and releases reservations older than `VENDOR_RESERVATION_TTL_MINUTES`.

Finalization is idempotent. Database check constraints prevent consumed or reserved counters from exceeding their limits.

## UAT API

All endpoints require function permissions and are documented in Swagger:

- `/api/v1/vendors/clients/`
- `/api/v1/vendors/integrations/`
- `/api/v1/vendors/commercial-profiles/`
- `/api/v1/vendors/client-allocations/`
- `/api/v1/vendors/survey-allocations/`
- `/api/v1/vendors/reservations/` (read-only audit)
- `/api/v1/vendors/directory/` (vendor policy directory)
- `/api/v1/vendors/management-options/` (non-secret vendor/client selector data)

The responsive `/vendors/` workspace uses these APIs for commercial policies, client visibility/quantity grants and optional survey overrides. User creation stays in the Access Control modal so account type, role and function-level allow/deny overrides have one source of truth.

Super admins and non-vendor management accounts see the full authorized dataset. Vendor accounts and respondents below an internal vendor are restricted to that vendor's allocations. Commercial policies and quantities remain owner-controlled and read-only for vendor-scoped accounts, even if a manage permission is assigned accidentally.

The first migrations map existing `company_name=InnovateMR` surveys to a seeded InnovateMR client without changing survey IDs, source CPI or respondent flow. Every later InnovateMR inventory sync applies the same client mapping, and its closed-survey pass cannot close inventory belonging to a future provider.
