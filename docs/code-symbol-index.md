# Complete production symbol index

Generated inventory of every top-level Python class/function and class method,
plus every named browser JavaScript function. Line numbers are navigation hints;
regenerate this index after major refactors. Detailed behavior is in
[function-reference.md](function-reference.md) and call chains are in
[developer-handbook.md](developer-handbook.md).

## `accounts/access.py`

- L44 `is_super_admin_account` (function) - Return whether the account may access temporarily restricted super-admin areas.
- L54 `effective_permission_codes` (function) - Module-internal helper; see its module responsibility and callers.
- L80 `has_function_access` (function) - Module-internal helper; see its module responsibility and callers.
- L86 `function_permission_required` (function) - Module-internal helper; see its module responsibility and callers.
- L100 `any_function_permission_required` (function) - Module-internal helper; see its module responsibility and callers.
- L113 `subordinate_user_ids` (function) - Module-internal helper; see its module responsibility and callers.
- L129 `manageable_user_ids` (function) - Subordinates plus members explicitly placed in an internal vendor workspace.
- L146 `activity_visible_user_ids` (function) - Return users whose tracking activity is visible to ``user``.
- L218 `assignable_functions` (function) - Module-internal helper; see its module responsibility and callers.
- L223 `assignable_roles` (function) - Module-internal helper; see its module responsibility and callers.
- L235 `can_manage_role` (function) - Module-internal helper; see its module responsibility and callers.
- L239 `HasFunctionPermission` (class) - Django/DRF framework hook or declarative data-shaping method.
- L242 `HasFunctionPermission.has_permission` (method) - Django/DRF framework hook or declarative data-shaping method.

## `accounts/admin.py`

- L6 `RoleFunctionPermissionInline` (class) - Django/DRF framework hook or declarative data-shaping method.
- L12 `RoleAdmin` (class) - Django/DRF framework hook or declarative data-shaping method.
- L21 `AccessFunctionAdmin` (class) - Django/DRF framework hook or declarative data-shaping method.
- L28 `EmployeeProfileAdmin` (class) - Django/DRF framework hook or declarative data-shaping method.
- L35 `UserFunctionOverrideAdmin` (class) - Django/DRF framework hook or declarative data-shaping method.

## `accounts/apps.py`

- L4 `AccountsConfig` (class) - Django/DRF framework hook or declarative data-shaping method.
- L8 `AccountsConfig.ready` (method) - Django/DRF framework hook or declarative data-shaping method.

## `accounts/context_processors.py`

- L4 `access_context` (function) - Module-internal helper; see its module responsibility and callers.

## `accounts/forms.py`

- L9 `WorkspaceAuthenticationForm` (class) - Django/DRF framework hook or declarative data-shaping method.
- L14 `WorkspaceAuthenticationForm.confirm_login_allowed` (method) - Django/DRF framework hook or declarative data-shaping method.
- L27 `FirstAdminSetupForm` (class) - Django/DRF framework hook or declarative data-shaping method.
- L35 `FirstAdminSetupForm.clean_username` (method) - Django/DRF framework hook or declarative data-shaping method.
- L41 `FirstAdminSetupForm.clean` (method) - Django/DRF framework hook or declarative data-shaping method.

## `accounts/function_catalog.py`

- L265 `sync_access_function_catalog` (function) - Synchronize code-defined functions without resetting configured access.

## `accounts/management/commands/role_config.py`

- L20 `serialize_role_config` (function) - Module-internal helper; see its module responsibility and callers.
- L48 `write_role_config` (function) - Module-internal helper; see its module responsibility and callers.
- L58 `validate_role_config` (function) - Module-internal helper; see its module responsibility and callers.
- L110 `Command` (class) - Django/DRF framework hook or declarative data-shaping method.
- L113 `Command.add_arguments` (method) - Django/DRF framework hook or declarative data-shaping method.
- L125 `Command.handle` (method) - Django/DRF framework hook or declarative data-shaping method.

## `accounts/models.py`

- L8 `AccessFunction` (class) - Django/DRF framework hook or declarative data-shaping method.
- L20 `AccessFunction.__str__` (method) - Django/DRF framework hook or declarative data-shaping method.
- L24 `Role` (class) - Django/DRF framework hook or declarative data-shaping method.
- L48 `Role.__str__` (method) - Django/DRF framework hook or declarative data-shaping method.
- L52 `RoleFunctionPermission` (class) - Django/DRF framework hook or declarative data-shaping method.
- L63 `RoleFunctionPermission.__str__` (method) - Django/DRF framework hook or declarative data-shaping method.
- L67 `EmployeeProfile` (class) - Django/DRF framework hook or declarative data-shaping method.
- L93 `EmployeeProfile.__str__` (method) - Django/DRF framework hook or declarative data-shaping method.
- L97 `UserFunctionOverride` (class) - Django/DRF framework hook or declarative data-shaping method.
- L113 `UserFunctionOverride.__str__` (method) - Django/DRF framework hook or declarative data-shaping method.

## `accounts/serializers.py`

- L18 `AccessFunctionSerializer` (class) - Django/DRF framework hook or declarative data-shaping method.
- L25 `RoleSerializer` (class) - Django/DRF framework hook or declarative data-shaping method.
- L39 `RoleSerializer.get_effective_permission_codes` (method) - Django/DRF framework hook or declarative data-shaping method.
- L42 `RoleSerializer.validate_permission_codes` (method) - Django/DRF framework hook or declarative data-shaping method.
- L56 `RoleSerializer._set_permissions` (method) - Django/DRF framework hook or declarative data-shaping method.
- L63 `RoleSerializer.create` (method) - Django/DRF framework hook or declarative data-shaping method.
- L73 `RoleSerializer.update` (method) - Django/DRF framework hook or declarative data-shaping method.
- L81 `UserAccessSerializer` (class) - Django/DRF framework hook or declarative data-shaping method.
- L118 `UserAccessSerializer.get_full_name` (method) - Django/DRF framework hook or declarative data-shaping method.
- L121 `UserAccessSerializer.get_role_details` (method) - Django/DRF framework hook or declarative data-shaping method.
- L125 `UserAccessSerializer.get_account_type_details` (method) - Django/DRF framework hook or declarative data-shaping method.
- L129 `UserAccessSerializer.get_organization_unit_details` (method) - Django/DRF framework hook or declarative data-shaping method.
- L145 `UserAccessSerializer.get_allowed_overrides` (method) - Django/DRF framework hook or declarative data-shaping method.
- L148 `UserAccessSerializer.get_denied_overrides` (method) - Django/DRF framework hook or declarative data-shaping method.
- L151 `UserAccessSerializer.get_effective_permissions` (method) - Django/DRF framework hook or declarative data-shaping method.
- L154 `UserAccessSerializer.validate` (method) - Django/DRF framework hook or declarative data-shaping method.
- L213 `UserAccessSerializer._forced_role` (method) - Django/DRF framework hook or declarative data-shaping method.
- L221 `UserAccessSerializer._ensure_vendor_policy` (method) - Django/DRF framework hook or declarative data-shaping method.
- L245 `UserAccessSerializer.validate_role` (method) - Django/DRF framework hook or declarative data-shaping method.
- L252 `UserAccessSerializer.validate_email` (method) - Django/DRF framework hook or declarative data-shaping method.
- L260 `UserAccessSerializer._validate_codes` (method) - Django/DRF framework hook or declarative data-shaping method.
- L273 `UserAccessSerializer.validate_allow_codes` (method) - Django/DRF framework hook or declarative data-shaping method.
- L276 `UserAccessSerializer.validate_deny_codes` (method) - Django/DRF framework hook or declarative data-shaping method.
- L279 `UserAccessSerializer._update_access` (method) - Django/DRF framework hook or declarative data-shaping method.
- L310 `UserAccessSerializer.create` (method) - Django/DRF framework hook or declarative data-shaping method.
- L345 `UserAccessSerializer.update` (method) - Django/DRF framework hook or declarative data-shaping method.

## `accounts/signals.py`

- L9 `ensure_employee_profile` (function) - Module-internal helper; see its module responsibility and callers.

## `accounts/views.py`

- L31 `WorkspaceLoginView` (class) - Django/DRF framework hook or declarative data-shaping method.
- L36 `WorkspaceLoginView.form_valid` (method) - Django/DRF framework hook or declarative data-shaping method.
- L43 `WorkspaceLogoutView` (class) - Django/DRF framework hook or declarative data-shaping method.
- L48 `first_admin_setup` (function) - Module-internal helper; see its module responsibility and callers.
- L72 `access_control_page` (function) - Module-internal helper; see its module responsibility and callers.
- L126 `cint_email_pool_import` (function) - Module-internal helper; see its module responsibility and callers.
- L194 `AccessFunctionViewSet` (class) - Django/DRF framework hook or declarative data-shaping method.
- L198 `AccessFunctionViewSet.get_required_function_permission` (method) - Django/DRF framework hook or declarative data-shaping method.
- L201 `AccessFunctionViewSet.get_queryset` (method) - Django/DRF framework hook or declarative data-shaping method.
- L217 `RoleViewSet` (class) - Django/DRF framework hook or declarative data-shaping method.
- L221 `RoleViewSet.get_required_function_permission` (method) - Django/DRF framework hook or declarative data-shaping method.
- L227 `RoleViewSet.get_queryset` (method) - Django/DRF framework hook or declarative data-shaping method.
- L230 `RoleViewSet.perform_update` (method) - Django/DRF framework hook or declarative data-shaping method.
- L235 `RoleViewSet.perform_destroy` (method) - Django/DRF framework hook or declarative data-shaping method.
- L258 `UserAccessViewSet` (class) - Django/DRF framework hook or declarative data-shaping method.
- L262 `UserAccessViewSet.get_required_function_permission` (method) - Django/DRF framework hook or declarative data-shaping method.
- L275 `UserAccessViewSet.get_queryset` (method) - Django/DRF framework hook or declarative data-shaping method.
- L285 `UserAccessViewSet.perform_destroy` (method) - Django/DRF framework hook or declarative data-shaping method.

## `config/api_docs.py`

- L25 `is_documentation_admin` (function) - Module-internal helper; see its module responsibility and callers.
- L35 `_basic_credentials` (function) - Module-internal helper; see its module responsibility and callers.
- L48 `_basic_challenge` (function) - Module-internal helper; see its module responsibility and callers.
- L55 `DocumentationProtectionMixin` (class) - Protect schema, Swagger UI and ReDoc before DRF renders any content.
- L58 `DocumentationProtectionMixin.dispatch` (method) - Django/DRF framework hook or declarative data-shaping method.
- L85 `IsDocumentationAdmin` (class) - Django/DRF framework hook or declarative data-shaping method.
- L88 `IsDocumentationAdmin.has_permission` (method) - Django/DRF framework hook or declarative data-shaping method.

## `config/cache_utils.py`

- L18 `jittered_ttl` (function) - Return a positive TTL spread around the configured base.
- L39 `stable_cache_key` (function) - Build a bounded key without exposing filter values in Redis key names.
- L49 `_cache` (function) - Module-internal helper; see its module responsibility and callers.
- L53 `safe_cache_get` (function) - Module-internal helper; see its module responsibility and callers.
- L61 `safe_cache_set` (function) - Module-internal helper; see its module responsibility and callers.
- L81 `safe_cache_delete` (function) - Module-internal helper; see its module responsibility and callers.
- L89 `safe_cache_get_or_set` (function) - Module-internal helper; see its module responsibility and callers.
- L111 `safe_cache_increment` (function) - Increment a namespace version without making cache availability critical.

## `config/celery.py`

- L13 `reconcile_credentials_on_startup` (function) - Baseline/detect environment-backed credentials when a worker or beat starts.

## `config/settings.py`

- L10 `env_bool` (function) - Module-internal helper; see its module responsibility and callers.

## `config/urls.py`

- L8 `ProtectedSchemaView` (class) - Django/DRF framework hook or declarative data-shaping method.
- L12 `ProtectedSwaggerView` (class) - Django/DRF framework hook or declarative data-shaping method.
- L16 `ProtectedRedocView` (class) - Django/DRF framework hook or declarative data-shaping method.

## `prescreener_vault/apps.py`

- L4 `PrescreenerVaultConfig` (class) - Django/DRF framework hook or declarative data-shaping method.

## `prescreener_vault/cache.py`

- L20 `_namespace_version` (function) - Return the current logical cache generation.
- L26 `invalidate_vault_cache` (function) - Logically invalidate all cached vault reads in one constant-time write.
- L32 `apply_submission_filters` (function) - Apply the Panelist Data UI filters to a vault queryset.
- L49 `vault_filter_options` (function) - Return cached distinct country/language/age/gender selector values.
- L91 `vault_filtered_summary` (function) - Return cached filter-aware vault totals.
- L116 `cached_profile` (function) - Return a bounded normalized profile snapshot without raw question payloads.

## `prescreener_vault/cint_email_pool.py`

- L32 `CintEmailPoolExhausted` (class) - Raised when no unassigned real respondent email remains.
- L38 `CintEmailPoolConfigurationError` (class) - Raised for invalid encryption, UID/RID or disabled identity state.
- L44 `_fernet` (function) - Derive the vault email cipher from the stable deployment secret.
- L56 `clean_cint_email` (function) - Apply Cint's documented normalization before SHA-256 hashing.
- L76 `cint_email_hash` (function) - Return Cint's SHA-256 hash of the normalized real email.
- L82 `add_real_email` (function) - Encrypt and add one real email; normalized duplicates are idempotent.
- L109 `reveal_email` (function) - Operational helper; ordinary respondent flows never decrypt the email.
- L120 `_cache_key` (function) - Create the Redis key for a non-secret UID-to-identity assignment.
- L126 `_identity_payload` (function) - Return the bounded cache representation; never include decrypted email.
- L136 `_load_or_assign` (function) - Return UID's stable identity or atomically claim the first available row.
- L192 `_record_distinct_session` (function) - Audit one RID use and update identity counters exactly once.
- L228 `assigned_email_hash` (function) - Return one stable real-email hash and audit each distinct Cint session.
- L263 `email_pool_status` (function) - Return operational counts without exposing any email address or hash.

## `prescreener_vault/management/commands/backfill_prescreener_vault.py`

- L10 `Command` (class) - Django/DRF framework hook or declarative data-shaping method.
- L13 `Command.add_arguments` (method) - Django/DRF framework hook or declarative data-shaping method.
- L24 `Command.handle` (method) - Django/DRF framework hook or declarative data-shaping method.

## `prescreener_vault/management/commands/cint_email_pool.py`

- L11 `Command` (class) - Django/DRF framework hook or declarative data-shaping method.
- L17 `Command.add_arguments` (method) - Django/DRF framework hook or declarative data-shaping method.
- L37 `Command._file_values` (method) - Django/DRF framework hook or declarative data-shaping method.
- L49 `Command.handle` (method) - Django/DRF framework hook or declarative data-shaping method.

## `prescreener_vault/models.py`

- L6 `PrescreenerSubmission` (class) - Immutable submission snapshot stored outside the operational database.
- L38 `PrescreenerAnswer` (class) - Django/DRF framework hook or declarative data-shaping method.
- L66 `PrescreenerAnswerValue` (class) - Django/DRF framework hook or declarative data-shaping method.
- L88 `CintRespondentEmail` (class) - One real respondent email, permanently assignable to at most one UID.
- L128 `CintRespondentEmailUse` (class) - Idempotent audit of one email identity being used by one RID/session.

## `prescreener_vault/router.py`

- L6 `PrescreenerVaultRouter` (class) - Keep vault models in their dedicated database and everything else out.
- L11 `PrescreenerVaultRouter.db_for_read` (method) - Django/DRF framework hook or declarative data-shaping method.
- L16 `PrescreenerVaultRouter.db_for_write` (method) - Django/DRF framework hook or declarative data-shaping method.
- L21 `PrescreenerVaultRouter.allow_relation` (method) - Django/DRF framework hook or declarative data-shaping method.
- L27 `PrescreenerVaultRouter.allow_migrate` (method) - Django/DRF framework hook or declarative data-shaping method.

## `prescreener_vault/services.py`

- L19 `PrescreenerVaultError` (class) - Django/DRF framework hook or declarative data-shaping method.
- L23 `PrescreenerVaultDisabled` (class) - Django/DRF framework hook or declarative data-shaping method.
- L27 `operational_answer_value` (function) - Do not duplicate new answer payloads in the operational DB once enabled.
- L32 `_clean_token` (function) - Module-internal helper; see its module responsibility and callers.
- L36 `_canonical_attribute` (function) - Module-internal helper; see its module responsibility and callers.
- L57 `_normalize_profile_value` (function) - Module-internal helper; see its module responsibility and callers.
- L66 `_age_group` (function) - Module-internal helper; see its module responsibility and callers.
- L77 `_age_from_value` (function) - Module-internal helper; see its module responsibility and callers.
- L93 `_question_snapshots` (function) - Module-internal helper; see its module responsibility and callers.
- L160 `capture_prescreener_submission` (function) - Persist one immutable, idempotent RID/UID submission in the vault.
- L236 `increment_profile_usage` (function) - Atomically audit one policy-approved reuse of the same respondent profile.

## `surveys/admin.py`

- L16 `SurveyQuotaInline` (class) - Django/DRF framework hook or declarative data-shaping method.
- L22 `TargetingQuestionInline` (class) - Django/DRF framework hook or declarative data-shaping method.
- L29 `SurveyAdmin` (class) - Django/DRF framework hook or declarative data-shaping method.
- L38 `SyncRunAdmin` (class) - Django/DRF framework hook or declarative data-shaping method.
- L44 `SurveyAttemptAdmin` (class) - Django/DRF framework hook or declarative data-shaping method.
- L57 `CanonicalOptionInline` (class) - Django/DRF framework hook or declarative data-shaping method.
- L63 `CanonicalQuestionAdmin` (class) - Django/DRF framework hook or declarative data-shaping method.
- L70 `ProviderOptionMappingInline` (class) - Django/DRF framework hook or declarative data-shaping method.
- L77 `ProviderQuestionMappingAdmin` (class) - Django/DRF framework hook or declarative data-shaping method.
- L92 `CanonicalOptionAdmin` (class) - Django/DRF framework hook or declarative data-shaping method.

## `surveys/apps.py`

- L4 `SurveysConfig` (class) - Django/DRF framework hook or declarative data-shaping method.
- L9 `SurveysConfig.ready` (method) - Django/DRF framework hook or declarative data-shaping method.

## `surveys/dashboard.py`

- L30 `dashboard_attempts` (function) - Apply the same hierarchy and respondent scope used by Studies.
- L51 `dashboard_client_options` (function) - Return only clients present inside the viewer's hierarchy-scoped traffic.
- L63 `_visible_revenue` (function) - Module-internal helper; see its module responsibility and callers.
- …16838 tokens truncated…`escapeHtml` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L29 `number` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L30 `moneyNumber` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L32 `formatCurrency` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L43 `formatLoi` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L50 `animateNumber` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L57 `frame` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L66 `updateSummary` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L83 `svgLine` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L87 `axisGrid` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L95 `labelStride` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L100 `animateChart` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L112 `renderVolume` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L119 `x` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L120 `y` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L121 `rateY` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L136 `renderFinance` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L154 `x` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L155 `revenueY` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L156 `lineY` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L177 `renderClients` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L190 `renderStatus` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L201 `renderDevices` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L217 `renderTopUsers` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L224 `updateGraphControls` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L240 `render` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L257 `showError` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L264 `loadDashboard` - Browser helper; detailed page responsibility is listed in the developer handbook.

## `static/surveys/prescreened_data.js`

- L13 `openAnswers` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L30 `closeAnswers` - Browser helper; detailed page responsibility is listed in the developer handbook.

## `static/surveys/projects.js`

- L4 `$` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L39 `escapeHtml` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L40 `formatDate` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L41 `money` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L50 `toast` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L59 `sourceTimestamp` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L71 `selectedValues` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L76 `updateMultiLabel` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L89 `updateBuyerOptions` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L107 `closeMultiSelects` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L153 `selectedOrdering` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L157 `closeCpiFilter` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L164 `updateCpiControl` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L191 `resetCpiControl` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L231 `queryString` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L250 `dateBoundary` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L257 `loadSurveys` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L281 `render` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L299 `rowTemplate` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L316 `cardTemplate` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L330 `scheduleLoad` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L359 `go` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L379 `setActiveTab` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L389 `openDrawer` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L402 `loadDrawerDetails` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L418 `renderActiveDetail` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L432 `closeDrawer` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L442 `renderQuotas` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L447 `quotaTargeting` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L452 `renderQuestions` - Browser helper; detailed page responsibility is listed in the developer handbook.

## `static/surveys/studies.js`

- L4 `byId` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L46 `escapeHtml` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L51 `escapeAttr` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L53 `selectedValues` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L55 `updateMultiLabel` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L66 `applyMenuVisibility` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L80 `setParentVisibility` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L92 `updateStudyHierarchyOptions` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L108 `closeMultiSelects` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L117 `updateStudyBuyerOptions` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L155 `dateBoundary` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L161 `filterParams` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L188 `formatIst` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L196 `formatLoi` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L202 `formatMoney` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L213 `animateRevenue` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L220 `frame` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L229 `animateMetric` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L234 `render` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L249 `frame` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L264 `deviceBadge` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L270 `ipPair` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L276 `endTimestamp` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L277 `timestampCell` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L278 `statusPill` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L284 `updateOverview` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L298 `rowTemplate` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L315 `cardTemplate` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L324 `loadAttempts` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L349 `scheduleLoad` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L350 `go` - Browser helper; detailed page responsibility is listed in the developer handbook.

## `static/surveys/user_hits.js`

- L4 `byId` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L31 `escapeHtml` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L32 `number` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L33 `selectedValues` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L35 `updateMultiLabel` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L42 `closeMultiSelects` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L50 `applyMenuVisibility` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L64 `setParentVisibility` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L76 `updateHierarchyOptions` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L111 `filterParams` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L122 `formatDate` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L127 `deviceBreakdown` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L132 `userCell` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L134 `rowTemplate` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L146 `cardTemplate` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L157 `updateOverview` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L169 `loadHits` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L193 `scheduleLoad` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L194 `go` - Browser helper; detailed page responsibility is listed in the developer handbook.

## `static/vendors/management.js`

- L18 `readColumns` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L37 `$` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L38 `$$` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L39 `field` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L41 `csrfToken` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L46 `escapeHtml` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L52 `flattenError` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L60 `api` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L76 `fetchAll` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L88 `initials` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L92 `accountLabel` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L96 `deliveryLabel` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L100 `number` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L104 `dateTime` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L111 `toInputDateTime` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L118 `toApiDateTime` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L122 `nullableNumber` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L126 `toast` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L137 `vendorIdentity` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L141 `typeBadge` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L145 `stateBadge` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L149 `quantityMarkup` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L157 `cutMarkup` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L162 `emptyRow` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L166 `actionButton` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L170 `renderOverview` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L177 `renderVendors` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L202 `renderClientAllocations` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L221 `renderSurveyAllocations` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L240 `renderApiKeys` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L259 `render` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L263 `option` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L267 `hydrateSelects` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L279 `updatePolicyRule` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L289 `updateClientRule` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L296 `updateSurveyRule` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L303 `resetForm` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L325 `showModal` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L332 `closeModal` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L339 `openPolicy` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L356 `openApiKey` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L362 `openClientAllocation` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L381 `openSurveyAllocation` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L403 `surveyResultMarkup` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L407 `searchSurveys` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L420 `reloadData` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L501 `submitVendorForm` - Browser helper; detailed page responsibility is listed in the developer handbook.

## `static/vendors/organization.js`

- L4 `$` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L5 `$$` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L24 `escapeHtml` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L25 `slug` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L26 `label` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L27 `ownerLabel` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L28 `option` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L30 `toast` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L36 `api` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L46 `fetchAll` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L52 `stateBadge` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L53 `actionButton` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L55 `unitActions` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L60 `unitLine` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L65 `unitNode` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L70 `renderStructure` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L83 `renderAccess` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L88 `renderClients` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L90 `providerName` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L100 `renderSummary` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L108 `openModal` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L109 `closeModals` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L112 `refreshParentOptions` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L121 `openUnit` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L133 `refreshClientOptions` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L138 `openClientAccess` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L149 `openClient` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L156 `reload` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L169 `deleteUnit` - Browser helper; detailed page responsibility is listed in the developer handbook.
- L175 `removeClientAccess` - Browser helper; detailed page responsibility is listed in the developer handbook.
