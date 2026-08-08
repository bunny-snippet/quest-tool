# Vendor, client, quantity and CPI foundation

This foundation is additive and is isolated on the UAT branch. Existing respondent entry/callback code does not enforce allocations yet.

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
5. `VendorSurveyAllocation` limits one survey inside the parent client allocation and may override CPI cut.
6. `AllocationReservation` records the reserved, consumed, released or expired quantity associated with one survey attempt.

## CPI precedence and snapshot

For external vendors, cut precedence is survey override, client override, then vendor default. Internal vendors always receive a zero-percent cut. On reservation, `SurveyAttempt` freezes:

- vendor and client;
- client and survey allocation IDs;
- source CPI;
- applied cut percentage;
- payable CPI; and
- currency.

Changing the live survey CPI later cannot change an existing attempt snapshot.

## Quantity lifecycle

The reservation service locks client and survey allocation rows in one database transaction. Capacity is available only when client remaining, survey remaining and upstream survey remaining are all positive.

- Initiation: reserve one client unit and one survey unit.
- Status `1`: move the unit from reserved to consumed at both levels.
- Status `2`, `3` or `4`: release both reserved units.
- Abandoned attempt: a future cleanup task will expire the reservation and release both units.

Finalization is idempotent. Database check constraints prevent consumed or reserved counters from exceeding their limits.

## UAT API

All endpoints require function permissions and are documented in Swagger:

- `/api/v1/vendors/clients/`
- `/api/v1/vendors/integrations/`
- `/api/v1/vendors/commercial-profiles/`
- `/api/v1/vendors/client-allocations/`
- `/api/v1/vendors/survey-allocations/`
- `/api/v1/vendors/reservations/` (read-only audit)

Super admins and non-vendor management accounts see the full authorized dataset. Vendor accounts and respondents below an internal vendor are restricted to that vendor's allocations. Commercial policies and quantities remain owner-controlled and read-only for vendor-scoped accounts, even if a manage permission is assigned accidentally.

The first migrations map existing `company_name=InnovateMR` surveys to a seeded InnovateMR client without changing survey IDs, source CPI or respondent flow. Every later InnovateMR inventory sync applies the same client mapping, and its closed-survey pass cannot close inventory belonging to a future provider.
