(() => {
  const workspace = document.querySelector('#vendorWorkspace');
  if (!workspace) return;

  const backdrop = document.querySelector('#vendorModalBackdrop');
  const modalConfigs = {
    policy: { form: document.querySelector('#vendorPolicyForm'), modal: document.querySelector('#vendorPolicyModal') },
    client: { form: document.querySelector('#clientAllocationForm'), modal: document.querySelector('#clientAllocationModal') },
    survey: { form: document.querySelector('#surveyAllocationForm'), modal: document.querySelector('#surveyAllocationModal') },
    api_key: { form: document.querySelector('#vendorApiKeyForm'), modal: document.querySelector('#vendorApiKeyModal') },
  };
  let activeMode = null;
  let form = null;
  let modal = null;
  let errorBox = null;
  const canViewVendors = workspace.dataset.viewVendors === 'true';
  const canViewAllocations = workspace.dataset.viewAllocations === 'true';
  const canManageVendors = workspace.dataset.manageVendors === 'true';
  const canManageAllocations = workspace.dataset.manageAllocations === 'true';
  const state = {
    vendors: [], profiles: [], clients: [], clientAllocations: [], surveyAllocations: [], apiKeys: [],
    selectedSurvey: null, searchTimer: null,
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const field = (name, mode = activeMode) => modalConfigs[mode]?.form?.elements[name];

  function csrfToken() {
    return document.cookie.split('; ').find((item) => item.startsWith('csrftoken='))?.split('=').slice(1).join('=') ||
      document.querySelector('input[name=csrfmiddlewaretoken]')?.value || '';
  }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, (char) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
    })[char]);
  }

  function flattenError(value, prefix = '') {
    if (Array.isArray(value)) return value.map((item) => flattenError(item, prefix)).join(' ');
    if (value && typeof value === 'object') {
      return Object.entries(value).map(([key, item]) => flattenError(item, key === 'non_field_errors' ? prefix : key)).join(' ');
    }
    return `${prefix ? `${prefix}: ` : ''}${value || 'Request could not be completed.'}`;
  }

  async function api(url, options = {}) {
    const response = await fetch(url, {
      credentials: 'same-origin',
      ...options,
      headers: {
        Accept: 'application/json',
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
        'X-CSRFToken': csrfToken(),
        ...(options.headers || {}),
      },
    });
    const data = response.status === 204 ? null : await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(flattenError(data));
    return data;
  }

  async function fetchAll(url) {
    let next = `${url}${url.includes('?') ? '&' : '?'}page_size=100`;
    const rows = [];
    while (next) {
      const data = await api(next);
      if (Array.isArray(data)) return data;
      rows.push(...(data.results || []));
      next = data.next;
    }
    return rows;
  }

  function initials(name) {
    return String(name || 'V').trim().split(/\s+/).slice(0, 2).map((part) => part[0]).join('').toUpperCase();
  }

  function accountLabel(type) {
    return type === 'internal_vendor' ? 'Internal' : type === 'external_vendor' ? 'External' : 'Vendor';
  }

  function deliveryLabel(mode) {
    return mode === 'api' ? 'API only' : mode === 'both' ? 'Panel + API' : 'Panel only';
  }

  function number(value) {
    return new Intl.NumberFormat('en-IN').format(Number(value || 0));
  }

  function dateTime(value) {
    if (!value) return 'No limit';
    return new Intl.DateTimeFormat('en-IN', {
      dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Kolkata',
    }).format(new Date(value));
  }

  function toInputDateTime(value) {
    if (!value) return '';
    const date = new Date(value);
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 16);
  }

  function toApiDateTime(value) {
    return value ? new Date(value).toISOString() : null;
  }

  function nullableNumber(value) {
    return value === '' ? null : value;
  }

  function toast(message, isError = false) {
    const region = document.querySelector('#toastRegion');
    if (!region) return;
    const item = document.createElement('div');
    item.className = `toast${isError ? ' error' : ''}`;
    item.textContent = message;
    region.appendChild(item);
    requestAnimationFrame(() => item.classList.add('show'));
    setTimeout(() => { item.classList.remove('show'); setTimeout(() => item.remove(), 220); }, 3200);
  }

  function vendorIdentity(vendor) {
    return `<div class="vendor-identity"><span>${escapeHtml(initials(vendor.full_name))}</span><div><strong>${escapeHtml(vendor.full_name)}</strong><small>${escapeHtml(vendor.email || vendor.username)}</small></div></div>`;
  }

  function typeBadge(type) {
    return `<span class="vendor-type ${escapeHtml(type)}">${escapeHtml(accountLabel(type))}</span>`;
  }

  function stateBadge(active) {
    return `<span class="vendor-state${active ? '' : ' inactive'}">${active ? 'Active' : 'Inactive'}</span>`;
  }

  function quantityMarkup(record) {
    const limit = Number(record.quantity_limit || 0);
    const used = Number(record.consumed_quantity || 0);
    const reserved = Number(record.reserved_quantity || 0);
    const percent = limit ? Math.min(100, ((used + reserved) / limit) * 100) : 0;
    return `<div class="quantity-cell"><div class="quantity-line"><strong>${number(record.remaining_quantity)} left</strong><span>${number(used)} used · ${number(reserved)} held / ${number(limit)}</span></div><div class="quantity-bar"><i style="width:${percent}%"></i></div></div>`;
  }

  function cutMarkup(record, inheritedLabel = 'effective') {
    const own = record.cpi_cut_override_percent;
    return `<div class="vendor-money"><strong>${escapeHtml(record.effective_cpi_cut_percent ?? 0)}%</strong><small>${own === null || own === undefined ? inheritedLabel : 'override'}</small></div>`;
  }

  function emptyRow(columns, message) {
    return `<tr><td colspan="${columns}"><div class="vendor-empty">${escapeHtml(message)}</div></td></tr>`;
  }

  function actionButton(kind, id, allowed) {
    return allowed ? `<button class="vendor-action" type="button" data-edit-${kind}="${id}">Edit</button>` : '';
  }

  function renderOverview() {
    $('#vendorCount').textContent = number(state.vendors.length);
    $('#allocationCount').textContent = number(state.clientAllocations.filter((row) => row.is_active).length);
    $('#remainingQuantity').textContent = number(state.clientAllocations.reduce((total, row) => total + Number(row.remaining_quantity || 0), 0));
    $('#surveyRuleCount').textContent = number(state.surveyAllocations.length);
  }

  function renderVendors() {
    if (!$('#vendorRows')) return;
    const profiles = new Map(state.profiles.map((item) => [Number(item.vendor), item]));
    const rows = state.vendors.map((vendor) => {
      const profile = profiles.get(Number(vendor.id));
      const cut = vendor.account_type === 'internal_vendor' ? '0.00' : (profile?.default_cpi_cut_percent ?? vendor.default_cpi_cut_percent ?? '0.00');
      return `<tr><td>${vendorIdentity(vendor)}</td><td>${typeBadge(vendor.account_type)}</td><td><div class="vendor-money"><strong>${escapeHtml(cut)}%</strong><small>${escapeHtml(profile?.currency || vendor.currency || 'USD')} policy</small></div></td><td>${number(vendor.allocation_count)}</td><td>${stateBadge(vendor.is_active && (profile?.is_active ?? true))}<small class="delivery-label">${escapeHtml(deliveryLabel(profile?.delivery_mode || vendor.delivery_mode))}</small></td><td>${actionButton('policy', vendor.id, canManageVendors)}</td></tr>`;
    }).join('') || emptyRow(6, 'No internal or external vendors have been created yet.');
    $('#vendorRows').innerHTML = rows;
    $('#vendorCards').innerHTML = state.vendors.map((vendor) => {
      const profile = profiles.get(Number(vendor.id));
      const cut = vendor.account_type === 'internal_vendor' ? '0.00' : (profile?.default_cpi_cut_percent ?? '0.00');
      return `<article class="vendor-card"><div class="vendor-card-head">${vendorIdentity(vendor)}${typeBadge(vendor.account_type)}</div><div class="vendor-card-grid"><span>Default CPI cut<strong>${escapeHtml(cut)}%</strong></span><span>Client grants<strong>${number(vendor.allocation_count)}</strong></span><span>Delivery<strong>${escapeHtml(deliveryLabel(profile?.delivery_mode || vendor.delivery_mode))}</strong></span><span>API keys<strong>${number(vendor.api_key_count)}</strong></span></div>${actionButton('policy', vendor.id, canManageVendors)}</article>`;
    }).join('');
  }

  function renderClientAllocations() {
    if (!$('#clientAllocationRows')) return;
    $('#clientAllocationRows').innerHTML = state.clientAllocations.map((row) => `<tr><td><strong>${escapeHtml(row.vendor_name)}</strong><br>${typeBadge(row.account_type)}</td><td><strong>${escapeHtml(row.client_name)}</strong><br><small>${stateBadge(row.is_active)}</small></td><td>${quantityMarkup(row)}</td><td>${cutMarkup(row, 'vendor default')}</td><td><div class="vendor-window"><span>${dateTime(row.starts_at)}</span><span>to ${dateTime(row.ends_at)}</span></div></td><td>${actionButton('client', row.id, canManageAllocations)}</td></tr>`).join('') || emptyRow(6, 'No client allocations yet.');
    $('#clientAllocationCards').innerHTML = state.clientAllocations.map((row) => `<article class="vendor-card"><div class="vendor-card-head"><div><strong>${escapeHtml(row.vendor_name)}</strong><br><small>${escapeHtml(row.client_name)}</small></div>${stateBadge(row.is_active)}</div><div class="vendor-card-grid"><span>Available<strong>${number(row.remaining_quantity)}</strong></span><span>Limit<strong>${number(row.quantity_limit)}</strong></span><span>CPI cut<strong>${escapeHtml(row.effective_cpi_cut_percent)}%</strong></span><span>Type<strong>${escapeHtml(accountLabel(row.account_type))}</strong></span></div>${quantityMarkup(row)}${actionButton('client', row.id, canManageAllocations)}</article>`).join('');
  }

  function renderSurveyAllocations() {
    if (!$('#surveyAllocationRows')) return;
    $('#surveyAllocationRows').innerHTML = state.surveyAllocations.map((row) => `<tr><td><strong>${escapeHtml(row.vendor_name)}</strong></td><td><strong>${escapeHtml(row.survey_local_id)}</strong><br><small>#${escapeHtml(row.survey_source_id)} · ${escapeHtml(row.survey_name || 'Survey')}</small></td><td>${escapeHtml(row.client_name)}</td><td>${quantityMarkup(row)}</td><td>${cutMarkup(row, 'client policy')}</td><td>${actionButton('survey', row.id, canManageAllocations)}</td></tr>`).join('') || emptyRow(6, 'No projects allocated. This vendor cannot see or start any client project yet.');
    $('#surveyAllocationCards').innerHTML = state.surveyAllocations.map((row) => `<article class="vendor-card"><div class="vendor-card-head"><div><strong>${escapeHtml(row.survey_local_id)}</strong><br><small>${escapeHtml(row.vendor_name)} · ${escapeHtml(row.client_name)}</small></div>${stateBadge(row.is_active)}</div><div class="vendor-card-grid"><span>Survey ID<strong>${escapeHtml(row.survey_source_id)}</strong></span><span>CPI cut<strong>${escapeHtml(row.effective_cpi_cut_percent)}%</strong></span><span>Available<strong>${number(row.remaining_quantity)}</strong></span><span>Limit<strong>${number(row.quantity_limit)}</strong></span></div>${actionButton('survey', row.id, canManageAllocations)}</article>`).join('');
  }

  function renderApiKeys() {
    if (!$('#apiKeyRows')) return;
    $('#apiKeyRows').innerHTML = state.apiKeys.map((key) => `<tr><td><strong>${escapeHtml(key.vendor_name)}</strong><br>${typeBadge(key.account_type)}</td><td><div class="vendor-money"><strong>${escapeHtml(key.name)}</strong><small>${escapeHtml(key.masked_key)}</small></div></td><td>${dateTime(key.created_at)}</td><td>${key.last_used_at ? dateTime(key.last_used_at) : 'Never'}</td><td>${key.expires_at ? dateTime(key.expires_at) : 'No expiry'}</td><td>${key.is_active ? `<button class="vendor-action danger" type="button" data-revoke-api-key="${key.id}">Revoke</button>` : stateBadge(false)}</td></tr>`).join('') || emptyRow(6, 'No API keys issued yet.');
    $('#apiKeyCards').innerHTML = state.apiKeys.map((key) => `<article class="vendor-card"><div class="vendor-card-head"><div><strong>${escapeHtml(key.name)}</strong><br><small>${escapeHtml(key.vendor_name)}</small></div>${key.is_active ? stateBadge(true) : stateBadge(false)}</div><div class="vendor-card-grid"><span>Key<strong>${escapeHtml(key.masked_key)}</strong></span><span>Last used<strong>${key.last_used_at ? dateTime(key.last_used_at) : 'Never'}</strong></span><span>Created<strong>${dateTime(key.created_at)}</strong></span><span>Expires<strong>${key.expires_at ? dateTime(key.expires_at) : 'Never'}</strong></span></div>${key.is_active ? `<button class="vendor-action danger" type="button" data-revoke-api-key="${key.id}">Revoke key</button>` : ''}</article>`).join('');
  }

  function render() {
    renderOverview(); renderVendors(); renderClientAllocations(); renderSurveyAllocations(); renderApiKeys();
  }

  function option(value, label, selected = false) {
    return `<option value="${escapeHtml(value)}"${selected ? ' selected' : ''}>${escapeHtml(label)}</option>`;
  }

  function hydrateSelects() {
    const vendorOptions = state.vendors.map((vendor) => option(vendor.id, `${vendor.full_name} — ${accountLabel(vendor.account_type)}`)).join('');
    field('policy_vendor', 'policy').innerHTML = vendorOptions;
    field('client_vendor', 'client').innerHTML = `<option value="">Select vendor</option>${vendorOptions}`;
    field('client', 'client').innerHTML = `<option value="">Select client</option>${state.clients.map((client) => option(client.id, client.name)).join('')}`;
    field('client_allocation', 'survey').innerHTML = `<option value="">Select vendor and client</option>${state.clientAllocations.map((row) => option(row.id, `${row.vendor_name} — ${row.client_name}`)).join('')}`;
    field('api_vendor', 'api_key').innerHTML = `<option value="">Select API-enabled external vendor</option>${state.vendors.filter((vendor) => {
      const profile = state.profiles.find((item) => Number(item.vendor) === Number(vendor.id));
      return vendor.account_type === 'external_vendor' && ['api', 'both'].includes(profile?.delivery_mode || vendor.delivery_mode);
    }).map((vendor) => option(vendor.id, vendor.full_name)).join('')}`;
  }

  function updatePolicyRule() {
    const vendor = state.vendors.find((item) => String(item.id) === field('policy_vendor', 'policy').value);
    const internal = vendor?.account_type === 'internal_vendor';
    field('default_cpi_cut_percent', 'policy').disabled = internal;
    if (internal) field('default_cpi_cut_percent', 'policy').value = '0.00';
    field('delivery_mode', 'policy').disabled = internal;
    if (internal) field('delivery_mode', 'policy').value = 'panel';
    $('#policyRuleNote').textContent = internal ? 'Internal vendors always receive the full source CPI.' : 'External vendor payable CPI = source CPI minus this percentage.';
  }

  function updateClientRule() {
    const vendor = state.vendors.find((item) => String(item.id) === field('client_vendor', 'client').value);
    const internal = vendor?.account_type === 'internal_vendor';
    field('client_cpi_cut', 'client').disabled = internal;
    if (internal) field('client_cpi_cut', 'client').value = '';
  }

  function updateSurveyRule() {
    const parent = state.clientAllocations.find((item) => String(item.id) === field('client_allocation', 'survey').value);
    const internal = parent?.account_type === 'internal_vendor';
    field('survey_cpi_cut', 'survey').disabled = internal;
    if (internal) field('survey_cpi_cut', 'survey').value = '';
  }

  function resetForm(mode) {
    activeMode = mode;
    ({ form, modal } = modalConfigs[mode]);
    errorBox = $('[data-vendor-form-error]', form);
    form.reset();
    $$('input,select', form).forEach((control) => { control.disabled = false; });
    field('record_id').value = '';
    if (field('survey')) field('survey').value = '';
    if (field('is_active')) field('is_active').checked = true;
    state.selectedSurvey = null;
    errorBox.hidden = true;
    const results = $('#surveySearchResults');
    if (results) results.hidden = true;
    const issuedPanel = $('#issuedKeyPanel');
    if (issuedPanel) issuedPanel.hidden = true;
    const issuedValue = $('#issuedKeyValue');
    if (issuedValue) issuedValue.value = '';
    const submit = $('[data-vendor-submit]', form);
    submit.hidden = false;
    submit.disabled = false;
  }

  function showModal() {
    backdrop.hidden = false; modal.hidden = false;
    requestAnimationFrame(() => { backdrop.classList.add('open'); modal.classList.add('open'); });
    document.body.classList.add('vendor-modal-open');
    setTimeout(() => form.querySelector('input:not([type=hidden]):not([disabled]),select:not([disabled])')?.focus(), 140);
  }

  function closeModal() {
    const closingModal = modal;
    backdrop.classList.remove('open'); closingModal?.classList.remove('open');
    document.body.classList.remove('vendor-modal-open');
    setTimeout(() => { backdrop.hidden = true; if (closingModal) closingModal.hidden = true; }, 210);
  }

  function openPolicy(vendorId) {
    resetForm('policy');
    const vendor = state.vendors.find((item) => Number(item.id) === Number(vendorId));
    const profile = state.profiles.find((item) => Number(item.vendor) === Number(vendorId));
    field('record_id').value = profile?.id || '';
    field('policy_vendor').value = String(vendorId);
    field('policy_vendor').disabled = Boolean(profile);
    field('default_cpi_cut_percent').value = profile?.default_cpi_cut_percent || '0.00';
    field('currency').value = profile?.currency || 'USD';
    field('delivery_mode').value = profile?.delivery_mode || vendor?.delivery_mode || 'panel';
    field('is_active').checked = profile?.is_active ?? true;
    $('[data-modal-eyebrow]', modal).textContent = accountLabel(vendor?.account_type);
    $('[data-modal-title]', modal).textContent = profile ? 'Edit commercial policy' : 'Create commercial policy';
    $('[data-vendor-submit]', form).textContent = profile ? 'Save policy' : 'Create policy';
    updatePolicyRule(); showModal();
  }

  function openApiKey() {
    resetForm('api_key');
    $('[data-vendor-submit]', form).textContent = 'Generate secure key';
    showModal();
  }

  function openClientAllocation(recordId = null) {
    resetForm('client');
    const record = state.clientAllocations.find((item) => Number(item.id) === Number(recordId));
    if (record) {
      field('record_id').value = record.id;
      field('client_vendor').value = record.vendor;
      field('client').value = record.client;
      field('client_vendor').disabled = true; field('client').disabled = true;
      field('client_quantity_limit').value = record.quantity_limit;
      field('client_cpi_cut').value = record.cpi_cut_override_percent ?? '';
      field('client_starts_at').value = toInputDateTime(record.starts_at);
      field('client_ends_at').value = toInputDateTime(record.ends_at);
      field('is_active').checked = record.is_active;
    }
    $('[data-modal-title]', modal).textContent = record ? 'Edit client allocation' : 'Allocate a client';
    $('[data-vendor-submit]', form).textContent = record ? 'Save allocation' : 'Create allocation';
    updateClientRule(); showModal();
  }

  function openSurveyAllocation(recordId = null) {
    resetForm('survey');
    const record = state.surveyAllocations.find((item) => Number(item.id) === Number(recordId));
    if (record) {
      field('record_id').value = record.id;
      field('client_allocation').value = record.client_allocation;
      field('client_allocation').disabled = true;
      field('survey').value = record.survey;
      field('survey_search').value = `${record.survey_local_id} · #${record.survey_source_id} · ${record.survey_name || 'Survey'}`;
      field('survey_search').disabled = true;
      state.selectedSurvey = { id: record.survey };
      field('survey_quantity_limit').value = record.quantity_limit;
      field('survey_cpi_cut').value = record.cpi_cut_override_percent ?? '';
      field('survey_starts_at').value = toInputDateTime(record.starts_at);
      field('survey_ends_at').value = toInputDateTime(record.ends_at);
      field('is_active').checked = record.is_active;
    }
    $('[data-modal-title]', modal).textContent = record ? 'Edit project allocation' : 'Allocate a project';
    $('[data-vendor-submit]', form).textContent = record ? 'Save project allocation' : 'Create project allocation';
    updateSurveyRule(); showModal();
  }

  function surveyResultMarkup(survey) {
    return `<button type="button" data-select-survey="${survey.id}"><span><strong>${escapeHtml(survey.local_id)} · #${escapeHtml(survey.source_id)}</strong><small>${escapeHtml(survey.name || 'Survey')} · ${escapeHtml(survey.country_label || '')}</small></span><b>${escapeHtml(survey.cpi ?? '—')}</b></button>`;
  }

  async function searchSurveys() {
    const query = field('survey_search').value.trim();
    const parent = state.clientAllocations.find((item) => String(item.id) === field('client_allocation').value);
    const results = $('#surveySearchResults');
    if (!parent || query.length < 2) { results.hidden = true; return; }
    try {
      const data = await api(`/api/v1/surveys/?page_size=10&client=${encodeURIComponent(parent.client)}&search=${encodeURIComponent(query)}`);
      const surveys = data.results || data;
      results.innerHTML = surveys.length ? surveys.map(surveyResultMarkup).join('') : '<div class="vendor-empty">No matching survey</div>';
      results.hidden = false;
    } catch (error) { toast(error.message, true); }
  }

  async function reloadData() {
    const [options, vendors, profiles, clientAllocations, surveyAllocations, apiKeys] = await Promise.all([
      api('/api/v1/vendors/management-options/'),
      canViewVendors ? fetchAll('/api/v1/vendors/directory/') : Promise.resolve(null),
      canViewVendors ? fetchAll('/api/v1/vendors/commercial-profiles/') : Promise.resolve([]),
      canViewAllocations ? fetchAll('/api/v1/vendors/client-allocations/') : Promise.resolve([]),
      canViewAllocations ? fetchAll('/api/v1/vendors/survey-allocations/') : Promise.resolve([]),
      canManageVendors ? fetchAll('/api/v1/vendors/api-keys/') : Promise.resolve([]),
    ]);
    Object.assign(state, {
      vendors: vendors || options.vendors || [], profiles, clients: options.clients || [],
      clientAllocations, surveyAllocations, apiKeys,
    });
    hydrateSelects(); render();
  }

  $$('.vendor-tabs [data-vendor-tab]').forEach((button) => button.addEventListener('click', () => {
    $$('.vendor-tabs [data-vendor-tab]').forEach((item) => item.classList.toggle('active', item === button));
    $$('[data-vendor-panel]').forEach((panel) => {
      const active = panel.dataset.vendorPanel === button.dataset.vendorTab;
      panel.hidden = !active; panel.classList.toggle('active', active);
    });
  }));

  workspace.addEventListener('click', (event) => {
    const policy = event.target.closest('[data-edit-policy]');
    const client = event.target.closest('[data-edit-client]');
    const survey = event.target.closest('[data-edit-survey]');
    if (policy) openPolicy(policy.dataset.editPolicy);
    if (client) openClientAllocation(client.dataset.editClient);
    if (survey) openSurveyAllocation(survey.dataset.editSurvey);
    const revokeKey = event.target.closest('[data-revoke-api-key]');
    if (revokeKey && confirm('Revoke this API key permanently?')) {
      api(`/api/v1/vendors/api-keys/${revokeKey.dataset.revokeApiKey}/`, { method: 'DELETE' })
        .then(() => { toast('API key revoked.'); return reloadData(); })
        .catch((error) => toast(error.message, true));
    }
  });
  $('[data-create-allocation="client"]')?.addEventListener('click', () => openClientAllocation());
  $('[data-create-allocation="survey"]')?.addEventListener('click', () => openSurveyAllocation());
  $('[data-create-api-key]')?.addEventListener('click', openApiKey);
  $$('[data-close-vendor-modal]').forEach((button) => button.addEventListener('click', closeModal));
  backdrop.addEventListener('click', closeModal);
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && modal && !modal.hidden) closeModal(); });
  field('policy_vendor', 'policy').addEventListener('change', updatePolicyRule);
  field('client_vendor', 'client').addEventListener('change', updateClientRule);
  field('client_allocation', 'survey').addEventListener('change', () => {
    field('survey', 'survey').value = ''; field('survey_search', 'survey').value = ''; state.selectedSurvey = null;
    updateSurveyRule();
  });
  field('survey_search', 'survey').addEventListener('input', () => {
    field('survey', 'survey').value = ''; state.selectedSurvey = null;
    clearTimeout(state.searchTimer); state.searchTimer = setTimeout(searchSurveys, 260);
  });
  $('#surveySearchResults').addEventListener('click', (event) => {
    const button = event.target.closest('[data-select-survey]');
    if (!button) return;
    const label = button.querySelector('strong').textContent;
    const subtitle = button.querySelector('small').textContent.split(' · ')[0];
    field('survey').value = button.dataset.selectSurvey;
    field('survey_search').value = `${label} · ${subtitle}`;
    state.selectedSurvey = { id: Number(button.dataset.selectSurvey) };
    $('#surveySearchResults').hidden = true;
  });
  $('#copyIssuedKey').addEventListener('click', async () => {
    await navigator.clipboard.writeText($('#issuedKeyValue').value);
    toast('API key copied. Store it securely.');
  });

  async function submitVendorForm(event) {
    event.preventDefault();
    activeMode = event.currentTarget.dataset.vendorForm;
    ({ form, modal } = modalConfigs[activeMode]);
    errorBox = $('[data-vendor-form-error]', form);
    errorBox.hidden = true;
    const mode = activeMode;
    const id = field('record_id').value;
    let url; let payload;
    if (mode === 'policy') {
      url = `/api/v1/vendors/commercial-profiles/${id ? `${id}/` : ''}`;
      payload = {
        vendor: Number(field('policy_vendor').value),
        default_cpi_cut_percent: field('default_cpi_cut_percent').disabled ? '0.00' : field('default_cpi_cut_percent').value,
        currency: field('currency').value, delivery_mode: field('delivery_mode').value,
        is_active: field('is_active').checked,
      };
    } else if (mode === 'client') {
      url = `/api/v1/vendors/client-allocations/${id ? `${id}/` : ''}`;
      payload = {
        vendor: Number(field('client_vendor').value), client: Number(field('client').value),
        quantity_limit: Number(field('client_quantity_limit').value),
        cpi_cut_override_percent: field('client_cpi_cut').disabled ? null : nullableNumber(field('client_cpi_cut').value),
        starts_at: toApiDateTime(field('client_starts_at').value), ends_at: toApiDateTime(field('client_ends_at').value),
        is_active: field('is_active').checked,
      };
    } else if (mode === 'survey') {
      if (!field('survey').value) { errorBox.textContent = 'Select a survey from the search results.'; errorBox.hidden = false; return; }
      url = `/api/v1/vendors/survey-allocations/${id ? `${id}/` : ''}`;
      payload = {
        client_allocation: Number(field('client_allocation').value), survey: Number(field('survey').value),
        quantity_limit: Number(field('survey_quantity_limit').value),
        cpi_cut_override_percent: field('survey_cpi_cut').disabled ? null : nullableNumber(field('survey_cpi_cut').value),
        starts_at: toApiDateTime(field('survey_starts_at').value), ends_at: toApiDateTime(field('survey_ends_at').value),
        is_active: field('is_active').checked,
      };
    } else {
      url = '/api/v1/vendors/api-keys/';
      payload = {
        vendor: Number(field('api_vendor').value), name: field('api_key_name').value.trim(),
        expires_at: toApiDateTime(field('api_key_expires_at').value),
      };
    }
    try {
      const submit = $('[data-vendor-submit]', form);
      submit.disabled = true;
      const result = await api(url, { method: id ? 'PATCH' : 'POST', body: JSON.stringify(payload) });
      if (mode === 'api_key') {
        $('#issuedKeyValue').value = result.api_key;
        $('#issuedKeyPanel').hidden = false;
        $$('input,select', form).forEach((control) => { control.disabled = true; });
        submit.hidden = true;
        toast('API key generated. Copy it now.');
        await reloadData();
      } else {
        closeModal(); toast(id ? 'Changes saved.' : 'Configuration created.'); await reloadData();
      }
    } catch (error) { errorBox.textContent = error.message; errorBox.hidden = false; }
    finally { const submit = $('[data-vendor-submit]', form); if (submit) submit.disabled = false; }
  }
  Object.values(modalConfigs).forEach((config) => config.form.addEventListener('submit', submitVendorForm));

  reloadData().catch((error) => {
    ['vendorRows', 'clientAllocationRows', 'surveyAllocationRows'].forEach((id) => {
      const node = document.getElementById(id); if (node) node.innerHTML = emptyRow(6, error.message);
    });
    toast(error.message, true);
  });
})();
