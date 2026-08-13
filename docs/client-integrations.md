# Client integrations

Client onboarding uses a hybrid model: owners configure non-secret metadata in **Client controls → Client API integrations**, while credential values stay in the server `.env` file. `ClientIntegration.credential_env_keys` contains names such as `RFG_APID`; it never contains the APID or secret itself.

## Research For Good

1. Add the production values to the VPS `.env` as `RFG_APID` and `RFG_SECRET`.
2. Restart Gunicorn and Celery so every process receives the new environment.
3. Open **Client API integrations**, choose **Add integration**, select the RFG client and choose **Research For Good** as the provider.
4. Complete the provider-aware form:

   | Field | Value |
   |---|---|
   | Integration name | A descriptive name such as `RFG Production` |
   | Base URL | `https://api.researchforgood.com/API` (no trailing slash) |
   | APID environment variable | `RFG_APID` |
   | Secret environment variable | `RFG_SECRET` |
   | Country | Optional two-letter ISO code, for example `US` |
   | Category | Optional `B2C` or `B2B` |
   | Public supplier code | `1000` |
   | Sync interval | `60` seconds or longer |

   Generic REST endpoint, API-header, token and Advanced fields are intentionally hidden for RFG because the backend adapter owns its signed-command contract.
5. Run **Test connection**. Only a verified connection can enable scheduled sync.
6. Use **Preview inventory** for a read-only check, then **Sync now**. Enable scheduled sync after validation.

Verified RFG inventory is synchronized automatically every 60 seconds. InnovateMR inventory is synchronized every 150 seconds. Celery Beat checks due integrations every 30 seconds, while `last_sync_started_at` and a database lease prevent overlapping provider calls. Inventory rows are keyed by `(integration, source_key)`, so equal upstream IDs belonging to separate client accounts cannot overwrite one another.

## Cint Exchange (Model 2, Method B polling)

1. Add `CINT_API_KEY=<production API key>`, `CINT_HASH_KEY=<entry-link signing key>` and `PUBLIC_APP_BASE_URL=https://api.exchange-ip.com` to the Quest VPS `.env`.
2. Create a Cint client in **Client API integrations**, then add an integration using provider **Cint Exchange**.
3. Enter the real numeric Cint Supplier Code. This value authenticates the upstream supply scope; it is not the public/masked supplier code used by the platform's respondent links.
4. Keep Base URL `https://api.samplicio.us`, credential environment variable `CINT_API_KEY`, sync interval `60`, and details per sync `1` initially.
5. Save, run **Test connection**, then run **Sync now**. A successful test enables the automatic one-minute schedule.

The adapter merges open Marketplace opportunities from `AllOfferwall/{SupplierCode}` with live allocated projects from `SupplierAllocations/All/{SupplierCode}` and de-duplicates them by Cint `SurveyNumber`. Projects are stored in the existing provider-neutral Survey table. The project eye drawer retrieves qualifications, real-time supplier quota capacity, localized question text and answer labels from the official Supply and Question Library endpoints. Cint's `Authorization` header is populated only from the encrypted credential or referenced environment variable.

Cint respondent traffic resolves `CINT_API_KEY` and `CINT_HASH_KEY` only from environment variables. New inventory is fetched only from `AllOfferwall/ByCountryLanguage/{CountryLanguageID}/{SupplierCode}` for the approved locale and RPI rules declared by `CintProvider.MARKETPLACE_RULES`; rejected rows never enter the database. Because creating a supplier link removes a survey from that open feed, `SupplierAllocations/All/{SupplierCode}` is also read during later syncs solely to refresh already-accepted source keys. It cannot introduce a new project. Cint project LOI uses `BidLengthOfInterview`, IR uses `BidIncidence`, and live remaining capacity uses `TotalRemaining`. For an accepted new row the platform POSTs `SupplierLinks/Create`; an existing row only PUTs `SupplierLinks/Update` when its locally recorded callback-contract fingerprint or Supplier Code does not match. The row is saved only after the upstream supplier-link call succeeds. The final entry URL uses the vault UID as PID, the unique attempt RID as MID, the normalized SHA-256 hash of a real email permanently assigned from the encrypted respondent pool, provider question-ID/Precode pairs, and an HMAC-SHA1 URL signature appended last. Employee login emails and plaintext respondent emails are never sent. `PUBLIC_APP_BASE_URL` must be the public HTTPS origin used for callbacks. Profile reuse remains disabled (`profile_reuse_enabled=false`, percentage `0`) unless a separately audited same-respondent policy is enabled.

Canonical qualifications are exposed at `/api/v1/canonical-questions/` and provider mappings at `/api/v1/provider-question-mappings/`. Mapping rows are scoped by provider, country, language and Cint CountryLanguageID, so integrations and external suppliers can use stable platform keys while the backend translates provider-specific question IDs and answer precodes.

## BioBrain / Voqall

BioBrain is provisioned in both Quest and Quant as a hidden client integration. Add only `BIOBRAIN_API_KEY` to the relevant VPS `.env`, then restart the web, worker and beat processes. With no key, the dispatcher skips the integration and the BioBrain client is excluded from client catalogs, organization grants, vendor allocations and Projects. The first successful sync containing at least one inventory row activates and publishes the client automatically.

The adapter uses `https://partner-api.voqall.com/api/v1/surveys`, sends the key only in `EQ-PARTNER-ACCESS-KEY`, and normalizes BioBrain inventory, quota and qualification payloads into the same internal survey models used by other providers. Each deployment has its own environment, so the client remains independently hidden in Quest or Quant until that deployment receives a valid key and inventory.

Inventory sync stores normalized projects first, then runs a bounded detail refresh outside the inventory transaction. The detail adapter stores targeting, quotas, every displayable provider answer and the permanent entry link. The respondent flow collects birthday, gender and country-valid postal code plus relevant targeting answers. Non-matching answers end locally with a recorded reason. Immediately before an eligible redirect it obtains RFG's official browser fingerprint when available, calls `duplicateCheck` with the persistent prescreener UID as RFG RID plus IP/fingerprint, then appends tracking and profile parameters to the provider link. The platform's 10-character attempt RID is sent as RFG `tid`; its 19-character vault UID is sent as RFG `rid`. If fingerprint generation is unavailable, RFG's documented `fingerprint: 0` plus mandatory UID/RID fallback is used.

Every visible live RFG inventory row can expose the platform Copy Link even while its bounded background detail refresh is pending. On the first valid platform start, a missing permanent RFG link and targeting snapshot are hydrated before an attempt is created. The raw URL returned by `livealert/createLink/1` is never treated as respondent-ready on its own because it does not yet contain the mandatory RID and profile parameters.

Configure the RFG server callback to the production HTTPS endpoint:

```text
https://api.exchange-ip.com/survey/rfg/callback?result={start.result}&tid={params.tid}&rid={params.rid}&ruledOutBy={start.ruledOutBy}&sesskey={sesskey}
```

Use RFG's server-to-server callback mode. RFG echoes the platform attempt key from `{params.tid}` and the persistent vault identity from `{params.rid}`. The endpoint resolves `tid` first and keeps UID-based `rid` lookup for compatibility. `{start.result}` supplies the documented terminal result. The endpoint records exit IP/time, LOI, the human-readable outcome and raw callback metadata, then finalizes allocation capacity. It rejects unknown tracking values and requests outside the documented RFG callback IP allowlist. If RFG changes its callback addresses, update `config.callback_ip_allowlist` through an audited integration update before switching traffic.

Provider field names never replace platform identity: after either TID or UID
lookup succeeds, status pages, Traffic Reports and callback acknowledgements use
the matched row's 10-character `SurveyAttempt.rid`. The callback JSON exposes
RFG echoes separately as `provider_tid` and `provider_rid`.

Configure the respondent-facing complete and non-complete page redirects to the dedicated outcome page:

```text
https://api.exchange-ip.com/survey/rfg/result?result={start.result}&tid={params.tid}&rid={params.rid}&ruledOutBy={start.ruledOutBy}&sesskey={sesskey}
```

The browser outcome page explains complete, terminate, quota, duplicate, paused, profile-validation and security result codes. A browser redirect is display-only and never marks a completion verified or payable; only the trusted server callback does that. RFG may also append `liveP`, `liveS`, `liveI` and `quotaThrottle`, which are retained in the attempt audit and used to improve the displayed reason.

## Operational API

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/v1/vendors/integrations/providers/` | Installed adapters and credential field metadata |
| `POST` | `/api/v1/vendors/integrations/{id}/test-connection/` | Authenticated non-mutating provider test |
| `GET` | `/api/v1/vendors/integrations/{id}/preview/` | Bounded read-only inventory preview |
| `POST` | `/api/v1/vendors/integrations/{id}/sync-now/` | Queue an immediate integration sync |

Access Control exposes separate functions for `clients.integration.view`, `manage`, `test`, `preview` and `sync`, so roles and individual overrides can grant each operation independently. External vendors are hard-blocked from all five functions even if an override is attempted.
