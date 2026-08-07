# Survey Workspace

Django-based internal survey workspace that synchronizes live InnovateMR Supplier API inventory, stores it locally, and exposes responsive Projects UI plus a documented REST API.

## What is included

- Dashboard placeholder and responsive Projects workspace matching the supplied table reference.
- Full and cursor-paged InnovateMR inventory ingestion.
- Deterministic merge by `surveyId`; the payload with the newest `modifiedDate` wins.
- Immutable 14-digit project IDs in `YYYYMM########` format, for example `20260800000001`.
- Quota and survey-targeting/pre-screening persistence with stale-data refresh.
- Celery Beat jobs every 60 seconds for inventory and bounded detail refresh.
- Search, multi-select company/market/status filters, date/CPI filters, ordering, pagination, and mobile survey cards.
- Direct survey detail drawer with equal-width Pre-screening and Quota tabs.
- Dynamic respondent pre-screener with 10-character RID, answer capture, supplier redirect, four callback outcomes, IP tracking and measured LOI.
- Session login plus dynamic role/function access control with per-user allow and deny overrides.
- Swagger UI, ReDoc, downloadable OpenAPI schema, Django Admin, sync audit records, and automated tests.

## Local setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python manage.py migrate
python manage.py runserver
```

Set `INNOVATEMR_API_TOKEN` in `.env` before synchronizing. The credential is only read by Django and is never returned to browser code.

Open:

- Projects UI: `http://127.0.0.1:8000/projects/`
- Dashboard: `http://127.0.0.1:8000/dashboard/`
- Swagger: `http://127.0.0.1:8000/api/docs/`
- ReDoc: `http://127.0.0.1:8000/api/redoc/`
- Admin: `http://127.0.0.1:8000/admin/`

Run one sync without Celery:

```powershell
python manage.py sync_surveys
```

## Scheduled jobs

Start Redis, then run these in separate terminals. On Windows, Celery's solo pool is the most reliable local option.

```powershell
celery -A config worker --loglevel=info --pool=solo
celery -A config beat --loglevel=info
```

Beat schedules two jobs every minute:

1. `surveys.sync_innovatemr_surveys` fetches both inventory endpoints, merges them, upserts current rows, and closes surveys no longer present.
2. `surveys.refresh_stale_details` refreshes a bounded batch of quotas and targeting. `INNOVATEMR_DETAIL_REFRESH_BATCH` controls the batch size.

Opening Quota or Pre-screening in the UI also refreshes that survey immediately when its cached details are older than its source `modifiedDate`. Cached details remain available during a temporary upstream outage.

## REST API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/surveys/` | Paginated, searchable survey inventory |
| `GET` | `/api/v1/surveys/{local_id}/` | One survey with stored quotas and targeting |
| `GET` | `/api/v1/surveys/{local_id}/quotas/` | Current quota definitions |
| `GET` | `/api/v1/surveys/{local_id}/targeting/` | Pre-screening questions and accepted options |
| `POST` | `/api/v1/sync/` | Queue an inventory sync |
| `POST` | `/api/v1/sync/?wait=true` | Run a synchronous operational sync |
| `GET` | `/api/v1/sync-runs/` | Sync audit history |
| `GET` | `/api/v1/survey-attempts/` | Staff-only RID, answers, status, IP and LOI audit |
| CRUD | `/api/v1/access/roles/` | Roles and their explicit function assignments |
| CRUD | `/api/v1/access/functions/` | Function permission catalog |
| CRUD | `/api/v1/access/users/` | Employee accounts, role and individual allow/deny overrides |
| `GET` | `/api/schema/` | OpenAPI 3 schema |

The interactive docs describe every query parameter, response object, enum, pagination envelope, and nested detail shape.

## Respondent pre-screener flow

Create an attempt by opening a validated survey link:

```text
/survey/start?surveyId=32655971&userId=294&code=20260800000001
```

The server validates that `surveyId` and the 14-digit internal `code` identify the same live survey, creates a cryptographically random 10-character RID containing uppercase, lowercase and numeric characters, stores `initiated_at` plus the request IP, and redirects to the canonical `/survey/start?rid=...` form.

The Projects table's **Copy link** button returns this platform pre-screener URL with a `[%%userId%%]` placeholder. The InnovateMR `entryLink` remains server-side redirect data and is used only after the pre-screener is submitted.

On submit it stores question answers, takes the exact InnovateMR `entryLink`, replaces its PID with RID, adds `trackId=RID` and available `QuestionKey=OptionId` values, marks the attempt redirected, and sends the browser to InnovateMR.

Configure these four return outcomes with InnovateMR (use the deployed HTTPS hostname in production):

```text
/survey?status=1&rid=%%pid%%   # Completed
/survey?status=2&rid=%%pid%%   # Terminated
/survey?status=3&rid=%%pid%%   # Over quota
/survey?status=4&rid=%%pid%%   # Quality terminated
```

Each callback captures its first arrival time, callback IP, callback count and LOI in seconds from `initiated_at`. Browser callbacks remain `is_verified=false`; a trusted InnovateMR S2S notification or redirect hash must verify them before financial reconciliation.

## Verification

```powershell
python manage.py check
python manage.py test
python manage.py spectacular --file schema.yml --validate
python manage.py collectstatic --noinput
```

See [architecture](docs/architecture.md) and [synchronization runbook](docs/synchronization.md) for the internal design and operations contract.
