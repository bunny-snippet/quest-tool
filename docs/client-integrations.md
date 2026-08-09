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
   | Sync interval | `600` seconds or longer |

   Generic REST endpoint, API-header, token and Advanced fields are intentionally hidden for RFG because the backend adapter owns its signed-command contract.
5. Run **Test connection**. Only a verified connection can enable scheduled sync.
6. Use **Preview inventory** for a read-only check, then **Sync now**. Enable scheduled sync after validation.

RFG inventory is never scheduled more frequently than 600 seconds. Celery Beat checks for due integrations every minute, while `last_sync_started_at` plus the configured interval controls whether a connection is actually queued. Inventory rows are keyed by `(integration, source_key)`, so equal upstream IDs belonging to separate client accounts cannot overwrite one another.

Inventory sync stores normalized projects first, then runs a bounded detail refresh outside the inventory transaction. The detail adapter stores targeting, quotas, provider questions and the permanent entry link. The respondent flow collects birthday, gender and postal code plus relevant targeting answers. Immediately before redirect it calls RFG `duplicateCheck`, then appends RID and profile parameters to the provider link.

Configure the RFG server callback to the production HTTPS endpoint:

```text
https://api.exchange-ip.com/survey/rfg/callback?result={start.result}&rid={params.rid}
```

Use RFG's server-to-server callback mode. `rid` is one of the tracking parameters appended to every entry link, so RFG echoes it as `{params.rid}`; `{start.result}` supplies the documented terminal result. The endpoint records exit IP/time, LOI and raw callback metadata, and finalizes allocation capacity. It rejects unknown RIDs and requests outside the documented RFG callback IP allowlist. If RFG changes its callback addresses, update `config.callback_ip_allowlist` through an audited integration update before switching traffic.

## Operational API

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/v1/vendors/integrations/providers/` | Installed adapters and credential field metadata |
| `POST` | `/api/v1/vendors/integrations/{id}/test-connection/` | Authenticated non-mutating provider test |
| `GET` | `/api/v1/vendors/integrations/{id}/preview/` | Bounded read-only inventory preview |
| `POST` | `/api/v1/vendors/integrations/{id}/sync-now/` | Queue an immediate integration sync |

Access Control exposes separate functions for `clients.integration.view`, `manage`, `test`, `preview` and `sync`, so roles and individual overrides can grant each operation independently. External vendors are hard-blocked from all five functions even if an override is attempted.
