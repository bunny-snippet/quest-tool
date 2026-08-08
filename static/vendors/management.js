(() => {
  const workspace = document.querySelector('#vendorWorkspace');
  if (!workspace) return;

  const form = document.querySelector('#vendorManagementForm');
  const modal = document.querySelector('#vendorModal');
  const backdrop = document.querySelector('#vendorModalBackdrop');
  const errorBox = document.querySelector('#vendorFormError');
  const canViewVendors = workspace.dataset.viewVendors === 'true';
  const canViewAllocations = workspace.dataset.viewAllocations === 'true';
  const canManageVendors = workspace.dataset.manageVendors === 'true';
  const canManageAllocations = workspace.dataset.manageAllocations === 'true';
  const state = {
    vendors: [], profiles: [], clients: [], clientAllocations: [], surveyAllocations: [],
    selectedSurvey: null, searchTimer: null,
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const field = (name) => form.elements[name];

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
      return `<tr><td>${vendorIdentity(vendor)}</td><td>${typeBadge(vendor.account_type)}</td><td><div class="vendor-money"><strong>${escapeHtml(cut)}%</strong><small>${escapeHtml(profile?.currency || vendor.currency || 'USD')} policy</small></div></td><td>${number(vendor.allocation_count)}</td><td>${stateBadge(vendor.is_active && (profile?.is_active ?? true))}</td><td>${actionButton('policy', vendor.id, canManageVendors)}</td></tr>`;
    }).join('') || emptyRow(6, 'No internal or external vendors have been created yet.');
    $('#vendorRows').innerHTML = rows;
    $('#vendorCards').innerHTML = state.vendors.map((vendor) => {
      const profile = profiles.get(Number(vendor.id));
      const cut = vendor.account_type === 'internal_vendor' ? '0.00' : (profile?.default_cpi_cut_percent ?? '0.00');
      return `<article class="vendor-card"><div class="vendor-card-head">${vendorIdentity(vendor)}${typeBadge(vendor.account_type)}</div><div class="vendor-card-grid"><span>Default CPI cut<strong>${escapeHtml(cut)}%</strong></span><span>Client grants<strong>${number(vendor.allocation_count)}</strong></span><span>Currency<strong>${escapeHtml(profile?.currency || 'USD')}</strong></span><span>Status<strong>${vendor.is_active && (profile?.is_active ?? true) ? 'Active' : 'Inactive'}</strong></span></div>${actionButton('policy', vendor.id, canManageVendors)}</article>`;
    }).join('');
  }

  function renderClientAllocations() {
    if (!$('#clientAllocationRows')) return;
    $('#clientAllocationRows').innerHTML = state.clientAllocations.map((row) => `<tr><td><strong>${escapeHtml(row.vendor_name)}</strong><br>${typeBadge(row.account_type)}</td><td><strong>${escapeHtml(row.client_name)}</strong><br><small>${stateBadge(row.is_active)}</small></td><td>${quantityMarkup(row)}</td><td>${cutMarkup(row, 'vendor default')}</td><td><div class="vendor-window"><span>${dateTime(row.starts_at)}</span><span>to ${dateTime(row.ends_at)}</span></div></td><td>${actionButton('client', row.id, canManageAllocations)}</td></tr>`).join('') || emptyRow(6, 'No client allocations yet.');
    $('#clientAllocationCards').innerHTML = state.clientAllocations.map((row) => `<article class="vendor-card"><div class="vendor-card-head"><div><strong>${escapeHtml(row.vendor_name)}</strong><br><small>${escapeHtml(row.client_name)}</small></div>${stateBadge(row.is_active)}</div><div class="vendor-card-grid"><span>Available<strong>${number(row.remaining_quantity)}</strong></span><span>Limit<strong>${number(row.quantity_limit)}</strong></span><span>CPI cut<strong>${escapeHtml(row.effective_cpi_cut_percent)}%</strong></span><span>Type<strong>${escapeHtml(accountLabel(row.account_type))}</strong></span></div>${quantityMarkup(row)}${actionButton('client', row.id, canManageAllocations)}</article>`).join('');
  }

  function renderSurveyAllocations() {
    if (!$('#surveyAllocationRows')) return;
    $('#surveyAllocationRows').innerHTML = state.surveyAllocations.map((row) => `<tr><td><strong>${escapeHtml(row.vendor_name)}</strong></td><td><strong>${escapeHtml(row.survey_local_id)}</strong><br><small>#${escapeHtml(row.survey_source_id)} · ${escapeHtml(row.survey_name || 'Survey')}</small></td><td>${escapeHtml(row.client_name)}</td><td>${quantityMarkup(row)}</td><td>${cutMarkup(row, 'client policy')}</td><td>${actionButton('survey', row.id, canManageAllocations)}</td></tr>`).join('') || emptyRow(6, 'No survey-specific overrides. Client policies apply automatically.');
    $('#surveyAllocationCards').innerHTML = state.surveyAllocations.map((row) => `<article class="vendor-card"><div class="vendor-card-head"><div><strong>${escapeHtml(row.survey_local_id)}</strong><br><small>${escapeHtml(row.vendor_name)} · ${escapeHtml(row.client_name)}</small></div>${stateBadge(row.is_active)}</div><div class="vendor-card-grid"><span>Survey ID<strong>${escapeHtml(row.survey_source_id)}</strong></span><span>CPI cut<strong>${escapeHtml(row.effective_cpi_cut_percent)}%</strong></span><span>Available<strong>${number(row.remaining_quantity)}</strong></span><span>Limit<strong>${number(row.quantity_limit)}</strong></span></div>${actionButton('survey', row.id, canManageAllocations)}</article>`).join('');
  }

  function render() {
    renderOverview(); renderVendors(); renderClientAllocations(); renderSurveyAllocations();
  }

  function option(value, label, selected = false) {
    return `<option value="${escapeHtml(value)}"${selected ? ' selected' : ''}>${escapeHtml(label)}</option>`;
  }

  function hydrateSelects() {
    const vendorOptions = state.vendors.map((vendor) => option(vendor.id, `${vendor.full_name} — ${accountLabel(vendor.account_type)}`)).join('');
    field('policy_vendor').innerHTML = vendorOptions;
    field('client_vendor').innerHTML = `<option value="">Select vendor</option>${vendorOptions}`;
    field('client').innerHTML = `<option value="">Select client</option>${state.clients.map((client) => option(client.id, client.name)).join('')}`;
    field('client_allocation').innerHTML = `<option value="">Select vendor and client</option>${state.clientAllocations.map((row) => option(row.id, `${row.vendor_name} — ${row.client_name}`)).join('')}`;
  }

  function updatePolicyRule() {
    const vendor = state.vendors.find((item) => String(item.id) === field('policy_vendor').value);
    const internal = vendor?.account_type === 'internal_vendor';
    field('default_cpi_cut_percent').disabled = internal;
    if (internal) field('default_cpi_cut_percent').value = '0.00';
    $('#policyRuleNote').textContent = internal ? 'Internal vendors always receive the full source CPI.' : 'External vendor payable CPI = source CPI minus this percentage.';
  }

  function updateClientRule() {
    const vendor = state.vendors.find((item) => String(item.id) === field('client_vendor').value);
    const internal = vendor?.account_type === 'internal_vendor';
    field('client_cpi_cut').disabled = internal;
    if (internal) field('client_cpi_cut').value = '';
  }

  function updateSurveyRule() {
    const parent = state.clientAllocations.find((item) => String(item.id) === field('client_allocation').value);
    const internal = parent?.account_type === 'internal_vendor';
    field('survey_cpi_cut').disabled = internal;
    if (internal) field('survey_cpi_cut').value = '';
  }

  function resetForm(mode) {
    form.reset();
    $$('input,select', form).forEach((control) => { control.disabled = false; });
    field('record_id').value = '';
    field('form_mode').value = mode;
    field('survey').value = '';
    field('is_active').checked = true;
    state.selectedSurvey = null;
    errorBox.hidden = true;
    $('#surveySearchResults').hidden = true;
    $$('[data-form-section]', form).forEach((section) => { section.hidden = section.dataset.formSection !== mode; });
  }

  function showModal() {
    backdrop.hidden = false; modal.hidden = false;
    requestAnimationFrame(() => { backdrop.classList.add('open'); modal.classList.add('open'); });
    document.body.classList.add('vendor-modal-open');
    setTimeout(() => form.querySelector('[data-form-section]:not([hidden]) input:not([type=hidden]),[data-form-section]:not([hidden]) select')?.focus(), 140);
  }

  function closeModal() {
    backdrop.classList.remove('open'); modal.classList.remove('open');
    document.body.classList.remove('vendor-modal-open');
    setTimeout(() => { backdrop.hidden = true; modal.hidden = true; }, 210);
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
    field('is_active').checked = profile?.is_active ?? true;
    $('#vendorModalEyebrow').textContent = accountLabel(vendor?.account_type);
    $('#vendorModalTitle').textContent = profile ? 'Edit commercial policy' : 'Create commercial policy';
    $('#vendorSubmitButton').textContent = profile ? 'Save policy' : 'Create policy';
    updatePolicyRule(); showModal();
  }

  function openClientAllocation(recordId = null) {
    resetForm('client'); field('policy_vendor').disabled = false;
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
    $('#vendorModalEyebrow').textContent = 'Client visibility & quantity';
    $('#vendorModalTitle').textContent = record ? 'Edit client allocation' : 'Allocate a client';
    $('#vendorSubmitButton').textContent = record ? 'Save allocation' : 'Create allocation';
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
    $('#vendorModalEyebrow').textContent = 'Optional survey rule';
    $('#vendorModalTitle').textContent = record ? 'Edit survey override' : 'Add survey override';
    $('#vendorSubmitButton').textContent = record ? 'Save override' : 'Create override';
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
    const [options, vendors, profiles, clientAllocations, surveyAllocations] = await Promise.all([
      api('/api/v1/vendors/management-options/'),
      canViewVendors ? fetchAll('/api/v1/vendors/directory/') : Promise.resolve(null),
      canViewVendors ? fetchAll('/api/v1/vendors/commercial-profiles/') : Promise.resolve([]),
      canViewAllocations ? fetchAll('/api/v1/vendors/client-allocations/') : Promise.resolve([]),
      canViewAllocations ? fetchAll('/api/v1/vendors/survey-allocations/') : Promise.resolve([]),
    ]);
    Object.assign(state, {
      vendors: vendors || options.vendors || [], profiles, clients: options.clients || [],
      clientAllocations, surveyAllocations,
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
  });
  $('[data-create-allocation="client"]')?.addEventListener('click', () => openClientAllocation());
  $('[data-create-allocation="survey"]')?.addEventListener('click', () => openSurveyAllocation());
  $$('[data-close-vendor-modal]').forEach((button) => button.addEventListener('click', closeModal));
  backdrop.addEventListener('click', closeModal);
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && !modal.hidden) closeModal(); });
  field('policy_vendor').addEventListener('change', updatePolicyRule);
  field('client_vendor').addEventListener('change', updateClientRule);
  field('client_allocation').addEventListener('change', () => {
    field('survey').value = ''; field('survey_search').value = ''; state.selectedSurvey = null;
    updateSurveyRule();
  });
  field('survey_search').addEventListener('input', () => {
    field('survey').value = ''; state.selectedSurvey = null;
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

  form.addEventListener('submit', async (event) => {
    event.preventDefault(); errorBox.hidden = true;
    const mode = field('form_mode').value;
    const id = field('record_id').value;
    let url; let payload;
    if (mode === 'policy') {
      url = `/api/v1/vendors/commercial-profiles/${id ? `${id}/` : ''}`;
      payload = {
        vendor: Number(field('policy_vendor').value),
        default_cpi_cut_percent: field('default_cpi_cut_percent').disabled ? '0.00' : field('default_cpi_cut_percent').value,
        currency: field('currency').value, is_active: field('is_active').checked,
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
    } else {
      if (!field('survey').value) { errorBox.textContent = 'Select a survey from the search results.'; errorBox.hidden = false; return; }
      url = `/api/v1/vendors/survey-allocations/${id ? `${id}/` : ''}`;
      payload = {
        client_allocation: Number(field('client_allocation').value), survey: Number(field('survey').value),
        quantity_limit: Number(field('survey_quantity_limit').value),
        cpi_cut_override_percent: field('survey_cpi_cut').disabled ? null : nullableNumber(field('survey_cpi_cut').value),
        starts_at: toApiDateTime(field('survey_starts_at').value), ends_at: toApiDateTime(field('survey_ends_at').value),
        is_active: field('is_active').checked,
      };
    }
    try {
      $('#vendorSubmitButton').disabled = true;
      await api(url, { method: id ? 'PATCH' : 'POST', body: JSON.stringify(payload) });
      closeModal(); toast(id ? 'Changes saved.' : 'Allocation created.'); await reloadData();
    } catch (error) { errorBox.textContent = error.message; errorBox.hidden = false; }
    finally { $('#vendorSubmitButton').disabled = false; }
  });

  reloadData().catch((error) => {
    ['vendorRows', 'clientAllocationRows', 'surveyAllocationRows'].forEach((id) => {
      const node = document.getElementById(id); if (node) node.innerHTML = emptyRow(6, error.message);
    });
    toast(error.message, true);
  });
})();
