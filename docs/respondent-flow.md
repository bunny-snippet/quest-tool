# Respondent pre-screener and redirect lifecycle

## Entry contract

`GET /survey/start?entry={opaque_encrypted_token}`

The copied link contains encrypted survey, user and optional supplier-allocation identifiers. Its GET is read-only and renders a CSRF-protected auto-submit gate, so crawlers/link previews cannot create attempts or reserve capacity. The POST rejects duplicated/extra parameters, authenticates the token and verifies that:

- the user exists, is active, and still has Projects and Copy Link access;
- the token's survey resolves to a live survey in that user's current scope;
- the survey has a non-empty allocated entry link.

For a vendor or an internal-vendor respondent, validation also requires a currently active client allocation with remaining quantity. An explicit survey override acts as an additional allow/block, date-window and quantity rule. Attempt creation and capacity reservation commit together, so an exhausted allocation cannot create an untracked respondent attempt.

An invalid or inconsistent user, code, survey, supplier, or additional query parameter returns the generic Invalid survey link page and creates no attempt.

The server creates a unique internal RID and public PID, records the validated user foreign key, entry time/IP and safe browser/device audit, then redirects with a short-lived encrypted `journey` token bound to the browser session. Cookies and authorization headers are never copied into the audit snapshot. The form displays PID only; RID is not placed in the page or continuation URL. A copied/tampered journey token, a token opened in another session, or a posted PID/RID cannot select an attempt.

## Pre-screener

Targeting questions are rendered by type:

- Single Punch → radio options
- Multi Punch → checkbox options
- AGE/Numeric Open Ended → constrained numeric input
- other open-ended questions → text input

Answers are persisted but are not used as an authoritative local rejection. InnovateMR may fill missing profile gaps and performs its own entrance checks. Age answers are mapped to the matching upstream range OptionId when available.

## Supplier redirect

For Research For Good, the platform attempt RID and reusable prescreener identity
remain separate throughout the redirect and callback journey. The 10-character
platform RID is sent as RFG `tid`, while the 19-character prescreener vault UID is
sent as RFG `rid`. RFG callbacks resolve `tid` first and UID-based `rid` second,
then always update the matched attempt under its canonical platform RID.

The public copied link always uses the platform-facing supplier code, so an upstream/vendor supplier code is not exposed there. The exact stored `entryLink` is parsed only after validation. Its PID is replaced with RID, `trackId=RID` is added, and captured `QuestionKey=OptionId` pairs are appended. `survNum` and the real upstream `supCode` are preserved from the allocated link; they are never reconstructed from client parameters. This keeps InnovateMR routing intact while allowing the same public code to be used for future providers.

InnovateMR owns the browser redirect after the respondent leaves this application. Configure the account-level or survey-level return URLs in InnovateMR to point to the public deployment. Each URL must carry `%%trackId%%` as the RID, `%%termReason%%` for the immediate provider reason, and `%%hashdata%%` as the final HMAC value. For example:

`https://survey.example.com/survey?status=1&rid=%%trackId%%&termReason=%%termReason%%&hash=%%hashdata%%`

Use status 1, 2, 3 and 4 for complete, terminate, over-quota and quality-terminate destinations respectively. A redirect to another domain such as `api.quantichamps.com` and a `code=null` value are produced by that upstream redirect configuration, not by the local Django callback route.

## Callback contract

`GET /survey?status={1|2|3|4}&rid={RID}`

The first authenticated callback sets the terminal status, callback time/exit IP, exit browser/device/OS/user-agent and `loi_seconds = callback_at - initiated_at`. A raw callback replay cannot replace or mutate that terminal outcome. The clean PID-only result URL is display-only.

The same transaction finalizes the vendor reservation: complete consumes the frozen quantity, while terminate, over-quota and quality-terminate release it. Reconciled upstream terminal statuses use the identical finalization service.

When an attempt came from an external supplier API key, RFG's verified
server-to-server outcome and local pre-survey rejection paths persist one
supplier callback event before dispatching it to Celery. The worker sends only
the public PID plus normalized status metadata, includes a stable `eventId`,
uses the optional HMAC signature, follows no redirects and retries bounded
network/429/5xx failures. Its queued/delivered/failed state is merged into the
attempt audit without storing the callback URL, signature or internal RID.

When a survey still has a legacy redirect configured, the browser cannot return its result to this application. As a temporary fallback, Celery polls InnovateMR's authenticated `getSurveyTransactionsByCond/{surveyId}/{PID}` endpoint for recent redirected attempts. PID and `trackId` both contain our RID, so the task can reconcile the terminal status, upstream public IP, end time and LOI without access to the legacy destination. Direct callbacks remain preferred and win any race with polling.

Status mapping:

1. Completed
2. Terminated
3. Over quota
4. Quality terminated

Pre-survey statuses are collapsed into the same five operational UI states: pending/redirected both display as Initiated, pre-survey termination maps to 2, pre-survey over-quota maps to 3, and pre-survey quality termination maps to 4.

Provider callback aliases are resolved only at callback boundaries. Browser prescreener continuation uses the session-bound `journey` token; direct PID/RID continuation is available only behind the explicit legacy rollback setting, which is disabled by default.

For Cint, the copied platform URL still opens the local pre-screener. On submit the immutable vault UID is the Cint PID and the attempt RID is the Cint MID. A real respondent email from the encrypted vault pool is permanently assigned to that UID; the live link receives only its normalized SHA-256 `cint_email`, never the employee login email or plaintext. A reused UID resolves to the same identity, while a new UID can only claim an unassigned address. The captured Cint question/precode parameters are added to the link. The entire URL including its required trailing `&` is signed with HMAC-SHA1 using `CINT_HASH_KEY`; URL-safe Base64 without padding is appended as the final lowercase `hash` parameter. Links over 1999 characters fail closed.

## Trust and verification

Browser redirects can be forged. InnovateMR callbacks therefore fail closed by default: they are rejected before any status, complete credit, callback timestamp, or capacity counter is changed unless `INNOVATEMR_CALLBACK_HASH_KEY` is configured and their HMAC matches. Verification uses the raw, fully hydrated public URL with the hash value emptied, preserving query order and encoding, and defaults to InnovateMR's HMAC-SHA256 mechanism. The received hash is never persisted or shown. A verified redirect reason is retained separately from the Survey Transactions response and takes display priority in Traffic/Term Reports; scheduled transaction reconciliation continues to preserve the provider's detailed history. The staff-only `/studies/` page, `/api/v1/survey-attempts/` endpoint and Django Admin expose the audit trail.

The Studies page applies user, status, text and entry/exit date-time filters server-side. `/api/v1/survey-attempts/export/` applies the identical filter contract but exports the complete related audit dataset rather than only the compact UI columns. Viewing requires `attempts.view`; downloading requires the independently assignable `attempts.export` function permission.
