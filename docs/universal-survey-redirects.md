# Universal survey redirects

Every provider can return a different name for the tracking value it received.
The public result endpoint accepts `tid`, `trackId`, `rid`, `pid`, `qsid` and
their common uppercase variants. Each candidate is resolved against the
attempt's immutable internal RID, platform PID and panelist UID.

Provider callback examples:

```text
Complete:          /survey?status=1&rid=<provider-echoed-tracking-id>
Terminate:         /survey?status=2&rid=<provider-echoed-tracking-id>
Quota full:        /survey?status=3&rid=<provider-echoed-tracking-id>
Quality terminate: /survey?status=4&rid=<provider-echoed-tracking-id>
```

After validation and audit capture, the browser receives one redirect to:

```text
/survey?status=<1-4>&pid=<platform-pid>
```

Only the platform PID is displayed on the result page. The internal RID stays
unchanged for Traffic Reports and provider reconciliation. RFG return details
are retained for Term Reports. Cint's transport hash is redacted before audit
storage and removed from the clean browser URL.
