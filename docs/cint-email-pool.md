# Cint real-email identity pool

Cint respondent entry uses only real email addresses supplied by the operations team. Each address is normalized, encrypted with Fernet in the dedicated prescreener vault database, and represented externally only by its SHA-256 hash. The operational database, browser URL, logs, and management-command output never receive the plaintext address.

## Identity rules

- The first Cint submission for a vault UID atomically claims one available email.
- That email remains permanently assigned to that UID and cannot be claimed by another UID.
- Reusing the same UID always returns the same email hash, including after a Redis expiry or restart.
- The attempt RID remains the unique Cint MID. Repeating the same RID is audited idempotently; a distinct RID for the same UID increments its usage count.
- The employee's account email is never used as respondent identity.
- When no real email is available, the respondent stays on the pre-screener with a retryable error. There is no dummy-email fallback.

Redis holds only a short-lived lookup containing the database row ID and hash. The vault MySQL database is authoritative, so cache expiry does not release or change an assignment.

## Configuration

Set a stable high-entropy `RESPONDENT_EMAIL_ENCRYPTION_KEY` in `.env`. Do not change it after importing addresses unless every stored value is re-encrypted first. `CINT_EMAIL_IDENTITY_CACHE_TTL_SECONDS` controls only the non-authoritative Redis lookup cache and defaults to 3600 seconds.

After deploying the migration, add real addresses without exposing them in application logs:

```bash
.venv/bin/python manage.py cint_email_pool --add person@example.com
.venv/bin/python manage.py cint_email_pool --add first@example.com --add second@example.com
.venv/bin/python manage.py cint_email_pool --file /secure/path/real-emails.csv
.venv/bin/python manage.py cint_email_pool --status
```

The import is idempotent after normalization. Gmail dots and `+tag` suffixes are normalized according to the Cint hashing rule, so aliases of the same Gmail mailbox are not added as separate identities.
