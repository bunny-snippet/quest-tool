# Respondent pre-screener and redirect lifecycle

## Entry contract

`GET /survey/start?surveyId={source_id}&userId={user_id}&code={local_id}`

All three values are required. `surveyId` and `code` must resolve to the same live local survey with a non-empty allocated entry link. `userId` is currently captured from the caller; replace this trust boundary with the planned profile API lookup when its contract is available.

The server creates a unique RID and records `initiated`, initiation time and IP before redirecting to the canonical RID form. RID is exactly 10 characters and always includes uppercase, lowercase and numeric characters.

## Pre-screener

Targeting questions are rendered by type:

- Single Punch → radio options
- Multi Punch → checkbox options
- AGE/Numeric Open Ended → constrained numeric input
- other open-ended questions → text input

Answers are persisted but are not used as an authoritative local rejection. InnovateMR may fill missing profile gaps and performs its own entrance checks. Age answers are mapped to the matching upstream range OptionId when available.

## Supplier redirect

The exact stored `entryLink` is parsed. Its PID is replaced with RID, `trackId=RID` is added, and captured `QuestionKey=OptionId` pairs are appended. `survNum` and `supCode` are preserved from the allocated link; they are never reconstructed from client parameters.

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
