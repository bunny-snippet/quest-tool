# Respondent pre-screener and redirect lifecycle

## Entry contract

`GET /survey/start?surveyId={source_id}&supplierCode={supplier_code}&userId={user_id}&code={local_id}`

All four values are required and the copied link is generated with the authenticated employee's real database user ID. Before rendering any questions, the server rejects duplicated/extra parameters and verifies that:

- the user exists, is active, and still has Projects and Copy Link access;
- `surveyId` and the 14-digit `code` resolve to the same live local survey;
- `supplierCode` matches the supplier code in that survey's stored allocated entry link; and
- the survey has a non-empty allocated entry link.

An invalid or inconsistent user, code, survey, supplier, or additional query parameter returns the generic Invalid survey link page and creates no attempt.

The server creates a unique RID and records the validated user foreign key, a user-ID snapshot, `initiated`, initiation time and IP before redirecting to the canonical RID form. RID is exactly 10 characters and always includes uppercase, lowercase and numeric characters. The canonical `?rid=` route also rejects unknown RIDs, extra parameters, and attempts whose user was removed or disabled.

## Pre-screener

Targeting questions are rendered by type:

- Single Punch → radio options
- Multi Punch → checkbox options
- AGE/Numeric Open Ended → constrained numeric input
- other open-ended questions → text input

Answers are persisted but are not used as an authoritative local rejection. InnovateMR may fill missing profile gaps and performs its own entrance checks. Age answers are mapped to the matching upstream range OptionId when available.

## Supplier redirect

The exact stored `entryLink` is parsed. Its PID is replaced with RID, `trackId=RID` is added, and captured `QuestionKey=OptionId` pairs are appended. `survNum` and `supCode` are preserved from the allocated link; they are never reconstructed from client parameters.

InnovateMR owns the browser redirect after the respondent leaves this application. Configure the account-level or survey-level return URLs in InnovateMR to point to the public deployment, using `%%trackId%%` as the RID, for example:

`https://survey.example.com/survey?status=1&rid=%%trackId%%`

Use status 1, 2, 3 and 4 for complete, terminate, over-quota and quality-terminate destinations respectively. A redirect to another domain such as `api.quantichamps.com` and a `code=null` value are produced by that upstream redirect configuration, not by the local Django callback route.

## Callback contract

`GET /survey?status={1|2|3|4}&rid={RID}`

The first callback sets the terminal status, callback time/IP and `loi_seconds = callback_at - initiated_at`. Later requests only update `last_callback_at` and `callback_count`, protecting the original outcome and LOI from refreshes.

Status mapping:

1. Completed
2. Terminated
3. Over quota
4. Quality terminated

The landing page accepts RID aliases `PID`, `pid`, `QSID`, `qsid`, and `trackId` for integration tolerance. The canonical parameter remains `rid`.

## Trust and verification

Browser redirects can be forged. Every callback starts as `is_verified=false`. Add InnovateMR server-to-server notification or redirect-hash validation before using a completion for rewards, invoices or financial reporting. The staff-only `/api/v1/survey-attempts/` endpoint and Django Admin expose the audit trail.
