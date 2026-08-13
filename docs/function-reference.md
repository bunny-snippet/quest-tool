# Production function reference

This reference explains the non-obvious production functions that carry business
logic. Django model field declarations, serializers that only declare fields,
admin classes, migrations and URL lists are described at file level in the
[developer handbook](developer-handbook.md); they do not contain an independent
runtime algorithm. Private helpers are included because changing them can alter
provider or reporting behavior.

The notation is:

- **Input/output** - the contract visible to its caller.
- **Connected to** - the next important functions, models or external system.
- **Side effect** - database/cache/provider changes, if any.

## `accounts/access.py`

- `is_super_admin_account(user)` detects Django superusers or active application
  Super Admin roles. It is the common bypass used by permission and scope helpers.
- `effective_permission_codes(user)` starts from active role grants, adds user
  `allow` overrides and finally removes user `deny` overrides. Deny therefore wins.
  Results drive templates, decorators and DRF permissions.
- `has_function_access(user, code)` is the single-code boolean wrapper around the
  effective set.
- `function_permission_required(code)` returns a Django view decorator. Anonymous
  users go to login; authenticated denied users receive HTTP 403.
- `any_function_permission_required(*codes)` is the OR version for views that can
  be reached through more than one capability.
- `subordinate_user_ids(user)` follows `EmployeeProfile.created_by` recursively
  and returns delegated descendants.
- `manageable_user_ids(user)` combines subordinate scope with the current account
  policy and is used by Access Control CRUD.
- `activity_visible_user_ids(user)` is the report boundary: employee sees self;
  TL sees employees in the same assigned Shift; broader roles see their permitted
  organization descendants. Traffic Reports, User Hits and Dashboard depend on it.
- `assignable_functions(user)` prevents delegated users from granting functions
  they do not hold.
- `assignable_roles(user)` returns roles the current actor may assign without rank
  or workspace escalation.
- `can_manage_role(user, role)` protects system roles and roles owned by another
  delegated workspace.
- `HasFunctionPermission.has_permission(request, view)` is the DRF bridge. It uses
  `view.required_function_permission` or action-specific declarations.

## `config/cache_utils.py`

- `jittered_ttl(base_seconds, jitter_seconds)` returns a positive random TTL in the
  configured range. It spreads expirations; it never restarts Redis or Django.
- `stable_cache_key(namespace, value)` JSON-normalizes and SHA-256 hashes variable
  input so PII/filter data does not appear in Redis key names.
- `_cache(alias)` resolves a Django cache backend lazily.
- `safe_cache_get`, `safe_cache_set`, `safe_cache_delete` catch/log backend errors
  and fail open. MySQL remains authoritative.
- `safe_cache_get_or_set(key, factory, ...)` reads cache, runs the supplied DB
  loader on a miss, caches its result, and returns it.
- `safe_cache_increment(key, ...)` increments namespace counters and recreates a
  missing counter safely. Vault and Projects invalidation use this pattern.

## `surveys/survey_flow.py`

- `generate_rid()` creates a shuffled 10-character string guaranteed to include
  uppercase, lowercase and numeric characters.
- `generate_prescreener_uid()` creates 16 mixed alphanumeric characters rendered
  as four groups (`XXXX-XXXX-XXXX-XXXX`).
- `ensure_attempt_prescreener_uid(attempt)` performs a compare-and-set DB update
  for legacy attempts. Concurrent callers converge on one UID; uniqueness clashes
  retry up to ten times.
- `normalize_client_ip(value)` accepts valid public/private IP text but rejects
  loopback and unspecified values, preventing `127.0.0.1` from becoming respondent
  evidence.
- `get_request_ip(request)` trusts proxy headers only when
  `TRUST_X_FORWARDED_FOR` is enabled; otherwise it uses `REMOTE_ADDR`.
- `supplier_code_from_entry_link(entry_link)` extracts real `supCode`/
  `supplierCode` for the immutable attempt snapshot. It is not the public `1000`.
- `_versioned_match(user_agent, patterns)` returns the first browser/OS name and
  captured version from a pattern list.
- `get_request_client_data(request)` creates the bounded device/browser/OS/language
  audit snapshot. It deliberately excludes cookies and Authorization headers.
- `backfill_attempt_entry_audit(attempt, request)` fills only blank entry evidence
  during rolling upgrades; it never overwrites already captured evidence.
- `create_attempt(survey, platform_user, ip, client_data)` atomically allocates RID
  and UID and freezes survey CPI, user, supplier and entry-client evidence. A rare
  RID/UID uniqueness collision retries.
- `build_outbound_url(entry_link, rid, answers)` is the generic InnovateMR path. It
  preserves the exact provider link, replaces/adds `PID=RID`, forces
  `trackId=RID`, and appends upstream question values.
- `status_rid_from_request(request)` accepts legacy capitalization/aliases and
  prefers provider `tid`/`trackId` because those fields carry the platform RID.
  The generic callback then resolves either `SurveyAttempt.rid` or
  `prescreener_uid` and always renders the matched attempt's canonical RID.

## `surveys/providers/base.py` and `registry.py`

- `environment_value(reference, label)` treats the configured value as an
  environment-variable *name*, validates it, then returns the secret value. It
  never returns the name as a credential.
- `NormalizedSurvey` is the adapter-to-sync immutable DTO: provider key, optional
  numeric key, provider modified time, normalized model fields and raw payload.
- `SurveyProvider` defines required adapter methods: connection test, inventory,
  normalization, detail refresh and outbound URL. Default duplicate check is false.
- `_provider_classes()` imports adapters lazily to avoid circular imports.
- `has_provider(code)` answers whether a specialized adapter exists.
- `provider_catalog()` combines installed adapters with generic InnovateMR,
  BioBrain and Custom REST form metadata.
- `get_provider(integration, session=None)` constructs the adapter selected by
  `integration.provider_code`; missing adapters raise a safe configuration error.

## `surveys/providers/rfg.py`

- `__init__` resolves APID/secret environment references, validates HTTPS/base
  host, loads locale/config and accepts an injectable session/clock for tests.
- `_command(payload)` adds RFG APID/time/HMAC-SHA1 authentication, POSTs signed
  JSON, validates the RFG result envelope and returns the response object.
- `explorer_read(command, **parameters)` exposes only allow-listed caller commands
  through the protected upstream explorer.
- `test_connection()` executes the documented connection command and returns a
  small safe summary.
- `inventory()` requests LiveAlert opportunities and returns normalized source
  rows to `sync_client_integration`.
- `_datetime(value)` and `_money(value)` safely normalize RFG time and currency
  values without leaking parsing failures into sync.
- `normalize_inventory_item(payload, seen_at)` maps RFG survey ID, market, CPI,
  LOI, incidence, buyer/type and availability into `NormalizedSurvey` while
  retaining the complete provider row.
- `targeting(source_key)` calls `livealert/targeting/1`; RFG quotas are embedded in
  this response.
- `datapoint(name)` resolves human-readable question/options metadata.
- `create_link(source_key)` obtains the provider's respondent entry base link.
- `_question_id(value)` creates a stable local numeric ID for string RFG datapoints.
- `refresh_details(survey)` builds required birthday/gender/postal questions,
  qualifying datapoint questions and quota rows, transactionally replaces old
  detail rows, stores the entry link, then refreshes canonical mappings.
- `duplicate_check(survey, attempt, ip, fingerprint)` sanitizes fingerprint and
  sends the **platform attempt RID as RFG `rid`** with survey/IP. It runs before
  browser redirect.
- `_answer_map(answers)` converts record-keyed form answers to RFG-keyed values.
- `build_outbound_url(survey, attempt, answers)` validates required profile data,
  converts age to birthday, removes stale `tid`, and sends only the **platform RID
  as RFG `rid`**. It also sends country, postal, gender, integration, Project ID
  and selected RFG datapoints.
- `_age_on`, `_birthday_from_age_or_date`, `_age_from_age_or_date` preserve legacy
  date answers while supporting the current age input and RFG birthday requirement.
- `_postal_is_valid(country, postal)` applies known country patterns and a safe
  non-empty fallback for markets without a local regex.
- `validate_prescreener(survey, answers)` enforces profile validity, optional strict
  targeting, qualifying choices and exclusive-option rules. Relaxed mode still
  validates required age/gender/postal format but lets RFG make the final match.

## `surveys/providers/cint.py`

- `__init__` resolves the API key, real numeric Supplier Code, exact Samplicio
  production host, timeouts, inventory-source flags, supplier-link behavior and
  hash-key environment name.
- `_request(path, method, payload, allow_not_found)` sends server-side Authorization,
  enforces connect/read/wall timeouts, validates JSON/ApiResult and raises sanitized
  provider errors.
- `explorer_read` and `explorer_create_supplier_link` are protected Swagger bridges.
- `_rows(data, key)` returns only list rows from a response envelope.
- `_load_definitions()` caches Cint country/language, sample and study lookup maps
  inside one adapter instance.
- `test_connection()` verifies credentials with a bounded lookup.
- `inventory()` merges open Offerwall opportunities and supplier allocations by
  SurveyNumber, preserving supplier-specific fields and continuing with allocated
  data if only the open feed fails.
- `_integer`, `_decimal`, `_microsoft_datetime` are defensive provider parsers.
- `_same_supplier` compares supplier IDs without numeric/string mismatch.
- `_supplier_allocation` selects this integration's allocation from a survey row.
- `_supplier_link` extracts this supplier's live/test link from different response
  shapes.
- `_country_language` expands CountryLanguageID into codes/names.
- `_survey_type` maps sample type to platform B2B/B2C display.
- `normalize_inventory_item` produces the Cint `NormalizedSurvey`, preserving
  allocation/link metadata and provider definitions in raw data.
- `_question_library`, `_question_metadata`, `_question_options` load localized
  qualification content.
- `_conditions(rows)` normalizes Cint question/operator/precode constraints.
- `_numeric_ranges(values)` compresses qualifying integer ages into inclusive UI
  ranges.
- `refresh_details(survey)` loads qualifications, supplier quotas, localized
  questions/options, qualifying hints and canonical mappings, then ensures a
  callback-enabled supplier link.
- `_redirect_payload()` creates OWS terminal URLs. Cint `[%MID%]` is returned as
  the platform callback `rid`, so MID must contain our 10-character RID.
- `ensure_supplier_link(survey)` returns cached live link or fetches/creates one;
  it stores only sanitized supplier-link metadata in raw data.
- `_entry_signature(unsigned_url, key)` HMAC-SHA1 signs the exact URL including the
  required trailing ampersand and returns URL-safe unpadded Base64.
- `build_outbound_url(survey, attempt, answers)` validates the provider host, sends
  **UID as Cint PID**, **RID as Cint MID**, obtains the stable UID email hash,
  appends qualification values, signs the URL and enforces Cint's length limit.

## `surveys/provider_services.py`

- `_preserve_provider_local_state` prevents a Cint inventory row that omits links
  from erasing a link hydrated by the detail process.
- `provider_preview(integration, limit)` fetches/normalizes a bounded read-only
  sample without writing Survey rows.
- `test_provider_connection(integration)` records success/failure; success enables
  scheduled sync, failure disables it until retested.
- `_survey_changed(survey, normalized)` compares raw and normalized persistent
  fields, ignoring only `last_seen_at`.
- `sync_client_integration(integration, refresh_details)` creates a `SyncRun`,
  fetches/deduplicates inventory, atomically creates/updates/closes surveys, records
  counters and optionally hydrates bounded details. It always finalizes audit state.
- `refresh_client_integration_details(integration, limit)` selects missing/stale
  live surveys and calls adapter `refresh_details` outside the inventory transaction.

## `surveys/services.py` (InnovateMR)

- `_integer`, `_decimal` safely parse provider scalar values.
- `_stable_question_id(item)` derives a deterministic local question ID when the
  provider omits a usable numeric ID.
- `parse_upstream_datetime(value)` accepts known InnovateMR timestamp formats and
  returns aware UTC.
- `payload_modified_at(payload)` chooses modified, then created, then oldest time.
- `merge_inventory(*inventories)` deduplicates by survey ID; newest provider time
  wins and the later input wins exact ties.
- `_survey_values(payload, seen_at)` maps InnovateMR fields to Survey columns.
- `_detail_changed(existing, incoming)` decides whether cached details became stale.
- `replace_survey_quotas` and `replace_survey_targeting` fetch and transactionally
  replace one detail type; documented 404 becomes a successful empty snapshot.
- `replace_survey_details` performs both detail refreshes.
- `_transaction_status(value)` maps provider transaction text/code to platform status.
- `_attempt_transaction(attempt, rows)` selects the transaction belonging to RID
  from provider rows.
- `reconcile_attempt_status(client, attempt)` polls transaction history, records
  reason/IP/end time and finalizes a terminal attempt idempotently.
- `SyncSummary` holds sync counters returned to commands/tasks/API.
- `sync_surveys(client=None, integration=None)` fetches full+paged InnovateMR feeds,
  merges, upserts, closes absent rows only after complete success, writes `SyncRun`
  and invalidates Projects cache.

## `surveys/tasks.py`

- `_stale_surveys(integration, limit)` returns live rows whose details are missing
  or older than source modification.
- `dispatch_due_integrations_task()` acquires due integration leases and queues one
  task per integration; it does not fetch provider data itself.
- `sync_client_integration_task(id)` dispatches specialized RFG/Cint or generic
  InnovateMR sync and records integration last-sync status.
- `sync_innovatemr_surveys_task()` is the legacy single-account InnovateMR task.
- `refresh_stale_details_task()` processes a bounded stale-detail batch.
- `reconcile_pending_attempts_task()` round-robins redirected InnovateMR attempts
  that have no callback and asks transaction reconciliation to finalize them.

## `prescreener_vault/services.py`

- `operational_answer_value(answers)` returns the bounded answer form allowed to
  remain in the operational attempt after the full vault capture.
- `_clean_token` normalizes question metadata for matching.
- `_canonical_attribute` infers age/DOB, gender, ethnicity, postal, country or
  language from question key/text/category; unknown questions remain `other`.
- `_normalize_profile_value` makes stable lowercase/canonical values for reuse.
- `_age_group` assigns configured reporting bands.
- `_age_from_value` derives age from numeric age or DOB at submission time.
- `_question_snapshots` joins submitted answers to targeting metadata and returns
  immutable question rows, normalized values and reusable profile dimensions.
- `capture_prescreener_submission` verifies vault enabled/non-empty answers,
  guarantees UID, rejects conflicting retries, writes submission/question/value
  rows in one vault transaction and invalidates cache after commit.
- `increment_profile_usage(uid)` atomically increments the audit count only when a
  policy-approved reuse actually occurs.

## `prescreener_vault/cache.py`

- `_namespace_version` returns the non-expiring logical cache generation.
- `invalidate_vault_cache` increments that generation in constant time.
- `apply_submission_filters` applies search/country/language/age-group/gender SQL.
- `vault_filter_options` caches distinct selector options for the current version.
- `vault_filtered_summary` caches filter-aware counts using a hashed selector key.
- `cached_profile(uid)` returns a bounded reusable profile without raw questions.

## `prescreener_vault/cint_email_pool.py`

- `_fernet` derives a Fernet key from the stable respondent-email encryption key.
  Changing that key makes existing encrypted rows undecryptable.
- `clean_cint_email` lowercases/validates and applies Gmail dot/plus normalization.
- `cint_email_hash` returns Cint's SHA-256 normalized-email value.
- `add_real_email` encrypts one address and idempotently rejects normalized duplicate.
- `reveal_email` is an admin/CLI helper; respondent redirects use only stored hash.
- `_cache_key` and `_identity_payload` create a non-PII UID cache representation.
- `_load_or_assign(uid)` uses a vault transaction and row locks to permanently
  assign the first available real identity to one UID.
- `_record_distinct_session(identity_id, uid, rid)` records one use per RID and
  atomically maintains first/last/use-count audit fields.
- `assigned_email_hash(uid, rid)` validates formats, reads cache or DB assignment,
  repairs stale cache after restore, audits the RID and returns only the hash.
- `email_pool_status` aggregates available/assigned/disabled identities and uses.

## `surveys/mappings.py`

- `_question_value_type` reduces provider types to canonical single/multi/numeric/
  date/text types.
- `infer_canonical_code` maps recognizable profile questions to stable platform
  codes and hashes unknown provider-specific questions.
- `_option_parts` extracts external value/label from heterogeneous option payloads.
- `_canonical_option_code` creates stable canonical option keys.
- `sync_survey_mappings(survey)` upserts canonical questions/options and links the
  survey's provider question/option IDs to them.
- `provider_answers(canonical_answers, provider_code, locale...)` reverses canonical
  answers into a selected provider's external question/option IDs.

## `vendors/access.py` and `vendors/services.py`

- `vendor_scope_user_id` identifies the supplier workspace owner that scopes data.
- `is_external_vendor_scope` distinguishes external suppliers from main/internal.
- `organization_workspace_owner_ids` returns main/internal owner IDs the actor may
  operate.
- `organization_unit_descendant_ids` and `organization_unit_ancestor_ids` traverse
  the strict organization tree with cycle-safe sets.
- `organization_unit_rollup_counts` calculates inherited member/client counts for
  organization cards without changing grants.
- `payable_cpi(source_cpi, cut_percent)` freezes commercial CPI after percentage cut.
- `_is_active_now`, `_active_window_q`, `_available_quantity_q` centralize date and
  capacity predicates so list visibility and reservation checks agree.
- `organization_client_ids_for_user` resolves inherited Shift/Sub-branch/Branch
  grants for internal users.
- `scope_surveys_for_user(queryset, user)` intersects client integration inventory
  with organization/supplier access.
- `resolve_vendor_survey_context(..., for_update)` resolves client/survey overrides,
  CPI and remaining capacity; locking mode is used before reservation.
- `survey_pricing_for_user` returns permission/commercially adjusted display CPI.
- `reserve_attempt_capacity` creates one expiring reservation atomically with the
  attempt.
- `finalize_attempt_capacity` consumes complete reservations and releases every
  non-complete terminal outcome idempotently.
- `expire_reservation` releases one abandoned reservation safely.

## `vendors/credentials.py`

- `_fernet` derives the stable integration-token encryption key.
- `token_fingerprint` creates a non-secret equality fingerprint.
- `resolve_integration_token` prefers configured environment reference, otherwise
  decrypts stored credential.
- `_clear_integration_data` closes/clears links and provider cache for only the
  integration whose credential changed.
- `set_integration_token` preserves data when fingerprint is unchanged; a real key
  change encrypts the new token and invalidates integration-local upstream state.
- `reconcile_all_integration_credentials` detects env-key changes at process startup.

## `vendors/upstream.py`

- `OperationSpec` is the allow-list record: method/path/docs/parameters/mutation
  confirmation and response description.
- `operation_response_description` creates non-technical Swagger explanations.
- `_provider_key` canonicalizes integration provider codes.
- `_custom_operations` validates explicitly configured same-host read operations.
- `operation_specs` returns built-in plus safe custom operations for an integration.
- `credential_metadata` reports only credential variable names/configured state.
- `_configured_endpoint` and `_effective_url` expand path parameters while enforcing
  the integration's scheme/host, preventing SSRF/arbitrary proxy behavior.
- `integration_metadata` produces the client/provider operation catalog.
- `_required_values` validates required request parameters.
- `_redact` recursively removes credential values from returned provider payloads.
- `_limit_payload` bounds large inventory responses for Swagger.
- `_credential_values` collects secrets only for redaction comparison.
- `_execute_rfg`, `_execute_rest`, `_execute_cint` use the existing authenticated
  provider adapters/clients instead of accepting browser credentials.
- `execute_operation` resolves the allow-listed operation, enforces mutation
  confirmation, executes it and returns bounded/redacted metadata and data.

## Controller and browser function groups

`surveys/views.py` page functions (`dashboard_page`, `projects_page`,
`studies_page`, `user_hits_page`, `prescreener_data_page`,
`termination_reasons_page`) calculate permissions and initial template context.
Their API classes perform the actual filter/query work so HTML and JSON security
remain aligned. Export helpers reuse the same scoped/filter querysets.

The public `survey_start` POST builds provider URLs (including vault-backed Cint
identity work) before touching the main attempt row. `_mark_attempt_redirected`
then uses one conditional `UPDATE ... WHERE status='initiated'` as a
compare-and-swap. This keeps cross-database work outside main-row locks and makes
simultaneous/repeated tab submissions converge on the first immutable redirect.

`vendors/views.py` and `accounts/views.py` are thin controller layers over their
serializers/services. When debugging a rejected write, inspect serializer
`validate`/`create`/`update` first; when debugging missing data, inspect the view's
scoped `get_queryset` and then the relevant access/service helper.

Browser files follow the same sequence: collect visible filter state, build a
same-origin query, fetch JSON, escape provider/user strings, render desktop rows
and mobile cards, then update pagination/summary. Browser code never grants access;
the server removes denied data and rejects denied parameters/actions.

For the full central controller walkthrough and symptom-to-file table, see
[developer handbook](developer-handbook.md#6-deep-walkthrough-surveysviewssurvey_start).
