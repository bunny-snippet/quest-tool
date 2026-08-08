# Vendor, client, quantity and CPI operations

This feature is additive and isolated on the UAT branch. Vendor allocation is enforced in project listing, copied-link validation, respondent initiation, callback finalization and legacy callback reconciliation. Ordinary non-vendor accounts continue to use the original inventory flow.

## Account rules

- `EmployeeProfile.account_type` remains the source of truth for employee, internal-vendor and external-vendor identity.
- Only an account with `vendors.manage` may create vendor accounts.
- Every internal vendor is assigned the system `Admin` role automatically. With `respondents.create` it may create employee/respondent children only.
- Every external vendor is assigned the safe system `External Vendor` role automatically. It can receive individual allow/deny overrides for permitted business functions.
- An external vendor is always terminal and cannot create users or roles, even if an Admin role or management allow-override is assigned accidentally. Identity, role, client, allocation and synchronization management functions are removed at permission evaluation time as a second line of defense.
- Branch/company and sub-branch/department apply to the internal hierarchy. External vendors store neither value and User Hits reports branch as not applicable.

## Data hierarchy

1. `Client` identifies a buyer/source account.
2. `ClientIntegration` stores non-secret upstream connection metadata. It stores the environment-variable name for a credential, never the token.
3. `VendorCommercialProfile` stores a vendor's default CPI cut, currency and delivery mode (`panel`, `api` or `both`). Internal vendors are always panel-only with zero cut.
4. `VendorClientAllocation` makes a client eligible for project assignment and limits total completes across that client's allocated projects. It does not expose every client survey.
5. `VendorSurveyAllocation` is a mandatory project whitelist entry inside the parent client allocation. It controls project visibility, the per-project complete cap and an optional CPI-cut override. Without an active allocation, that project is absent from both panel and API responses and its respondent link is rejected.
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

Each new attempt resolves the current source CPI and current survey/client/vendor cut again. Consequently, a completed attempt at CPI 3 remains CPI 3 after an update, while later attempts use the newly published CPI and current configured cut.

## External delivery channels

An external vendor can be configured as Panel only, API only, or Panel + API. API-only vendors cannot establish or retain a browser session. Panel-only vendors cannot receive or use API keys.

Owner workspace users issue revocable keys from Vendor Management. A plaintext key is displayed exactly once; the database stores only an HMAC-SHA256 digest plus a masked prefix/suffix. Send the key as either:

```http
X-API-Key: exh_...
```

or:

```http
Authorization: Api-Key exh_...
```

The key authenticates as its external vendor. It does not carry a copied client list or copied CPI: every request applies that vendor's current function permissions, active client grants, explicit project allocations, quantities and per-client/project CPI cut. The same external vendor can therefore receive selected Client ABC projects at 30% cut and selected Client BCZ projects at 50% cut in both panel and API responses. `/api/v1/surveys/?client_name=ABC` filters the allocated client label.

## Quantity lifecycle

The reservation service locks the client row and mandatory project-allocation row in one database transaction. Capacity is available only when client remaining, project remaining and upstream survey remaining are all positive.

- Initiation: reserve one client unit and one project-allocation unit.
- Status `1`: move the reserved unit to consumed.
- Status `2`, `3` or `4`: release the reserved unit.
- Abandoned attempt: `vendors.expire_allocation_reservations` runs every `VENDOR_RESERVATION_CLEANUP_INTERVAL_SECONDS` and releases reservations older than `VENDOR_RESERVATION_TTL_MINUTES`.

Finalization is idempotent. Database check constraints prevent consumed or reserved counters from exceeding their limits.

## UAT API

All endpoints require function permissions and are documented in Swagger:

- `/api/v1/vendors/clients/`
- `/api/v1/vendors/integrations/`
- `/api/v1/vendors/commercial-profiles/`
- `/api/v1/vendors/api-keys/` (issue, list masked metadata, update label/expiry and revoke)
- `/api/v1/vendors/client-allocations/`
- `/api/v1/vendors/survey-allocations/`
- `/api/v1/vendors/reservations/` (read-only audit)
- `/api/v1/vendors/directory/` (vendor policy directory)
- `/api/v1/vendors/management-options/` (non-secret vendor/client selector data)

The responsive `/vendors/` workspace uses separate modals for commercial policy, client allocation, project allocation and API-key operations. User creation stays in the Access Control modal so account type, role and function-level allow/deny overrides have one source of truth.

Super admins and non-vendor management accounts see the full authorized dataset. Vendor accounts and respondents below an internal vendor are restricted to that vendor's allocations. Commercial policies, quantities and API keys remain owner-controlled and read-only for vendor-scoped accounts, even if a manage permission is assigned accidentally.

The first migrations map existing `company_name=InnovateMR` surveys to a seeded InnovateMR client without changing survey IDs, source CPI or respondent flow. Every later InnovateMR inventory sync applies the same client mapping, and its closed-survey pass cannot close inventory belonging to a future provider.
