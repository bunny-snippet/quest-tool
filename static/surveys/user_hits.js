(() => {
  const byId = (id) => document.getElementById(id);
  const elements = {
    search: byId('hitSearch'), from: byId('hitFromDate'), fromTime: byId('hitFromTime'),
    to: byId('hitToDate'), toTime: byId('hitToTime'), clear: byId('clearHitFilters'),
    pageSize: byId('hitPageSize'), rows: byId('hitRows'), cards: byId('hitCards'), summary: byId('hitSummary'),
    pageStatus: byId('hitPageStatus'), pageInput: byId('hitPageInput'), totalPages: byId('hitTotalPages'),
    first: byId('hitFirstPage'), prev: byId('hitPrevPage'), next: byId('hitNextPage'), last: byId('hitLastPage'),
    totalHits: byId('totalHitCount'), totalCompletes: byId('totalCompleteCount'), conversion: byId('conversionRate'),
    activeUsers: byId('activeUserCount'), dayCount: byId('hitDayCount'),
  };
  if (!elements.rows) return;

  const state = { page: 1, pages: 1, pageSize: 20, timer: null, controller: null };
  const icons = {
    desktop: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4"/></svg>',
    mobile: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="2" width="12" height="20" rx="2"/><path d="M10 18h4"/></svg>',
    tablet: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="2" width="14" height="20" rx="2"/><circle cx="12" cy="18" r=".7"/></svg>',
  };

  function escapeHtml(value) {
    const node = document.createElement('div');
    node.textContent = value == null ? '' : String(value);
    return node.innerHTML;
  }

  function number(value) { return Number(value || 0).toLocaleString('en-IN'); }

  function selectedValues(container) {
    return [...container.querySelectorAll('input:checked')].map((input) => input.value);
  }

  function updateMultiLabel(container) {
    const checked = [...container.querySelectorAll('input:checked')];
    const button = container.querySelector('.multi-trigger');
    const fallback = { branch: 'All branches', sub_branch: 'All sub-branches', user: 'All users' }[container.dataset.hitFilter];
    const label = checked.length === 0 ? fallback : checked.length === 1 ? checked[0].closest('label').innerText.trim() : `${checked.length} selected`;
    button.querySelector('span').textContent = label;
    button.classList.toggle('has-value', checked.length > 0);
  }

  function closeMultiSelects(except = null) {
    document.querySelectorAll('.user-hits-filters .multi-select.open').forEach((container) => {
      if (container === except) return;
      container.classList.remove('open');
      container.querySelector('.multi-menu').hidden = true;
      container.querySelector('.multi-trigger').setAttribute('aria-expanded', 'false');
    });
  }

  document.querySelectorAll('.user-hits-filters .multi-select').forEach((container) => {
    const trigger = container.querySelector('.multi-trigger');
    const menu = container.querySelector('.multi-menu');
    trigger.addEventListener('click', () => {
      const shouldOpen = !container.classList.contains('open');
      closeMultiSelects(container);
      container.classList.toggle('open', shouldOpen);
      menu.hidden = !shouldOpen;
      trigger.setAttribute('aria-expanded', String(shouldOpen));
    });
    menu.addEventListener('change', () => { updateMultiLabel(container); scheduleLoad(); });
  });

  function filterParams() {
    const params = new URLSearchParams({ page: state.page, page_size: state.pageSize });
    const search = elements.search.value.trim();
    if (search) params.set('search', search);
    document.querySelectorAll('.user-hits-filters [data-hit-filter]').forEach((container) => {
      const values = selectedValues(container);
      if (values.length) params.set(container.dataset.hitFilter, values.join(','));
    });
    if (elements.from.value) {
      params.set('from_date', elements.from.value);
      if (elements.fromTime.value) params.set('from_time', elements.fromTime.value);
    }
    if (elements.to.value) {
      params.set('to_date', elements.to.value);
      if (elements.toTime.value) params.set('to_time', elements.toTime.value);
    }
    return params;
  }

  function formatDate(value) {
    if (!value) return '—';
    return new Intl.DateTimeFormat('en-IN', {
      timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', year: 'numeric', weekday: 'short',
    }).format(new Date(`${value}T12:00:00Z`));
  }

  function deviceBreakdown(counts) {
    const unknown = Number(counts.unclassified || 0);
    return `<div class="device-metric">
      <strong>Total <b>${number(counts.total)}</b></strong>
      <div class="device-chips">
        <span class="desktop" title="Desktop"><i>${icons.desktop}</i><b>${number(counts.desktop)}</b><em class="sr-only">Desktop</em></span>
        <span class="mobile" title="Mobile"><i>${icons.mobile}</i><b>${number(counts.mobile)}</b><em class="sr-only">Mobile</em></span>
        <span class="tablet" title="Tablet"><i>${icons.tablet}</i><b>${number(counts.tablet)}</b><em class="sr-only">Tablet</em></span>
      </div>
      ${unknown ? `<small>${number(unknown)} unclassified</small>` : ''}
    </div>`;
  }

  function rowTemplate(row) {
    return `<tr>
      <td><strong class="hit-branch">${escapeHtml(row.branch || '—')}</strong></td>
      <td><span class="hit-sub-branch">${escapeHtml(row.sub_branch || '—')}</span></td>
      <td><div class="hit-user"><span>${escapeHtml(String(row.user_name || '?').charAt(0).toUpperCase())}</span><div><strong>${escapeHtml(row.user_name)}</strong><small>${escapeHtml(row.user_email || row.username)}</small></div></div></td>
      <td><time class="hit-date" datetime="${escapeHtml(row.date)}"><strong>${formatDate(row.date)}</strong><span>IST calendar day</span></time></td>
      <td>${deviceBreakdown(row.hits)}</td>
      <td>${deviceBreakdown(row.completes)}</td>
    </tr>`;
  }

  function cardTemplate(row) {
    return `<article class="survey-card user-hit-card">
      <div class="user-hit-card-head"><div class="hit-user"><span>${escapeHtml(String(row.user_name || '?').charAt(0).toUpperCase())}</span><div><strong>${escapeHtml(row.user_name)}</strong><small>${escapeHtml(row.user_email || row.username)}</small></div></div><time>${formatDate(row.date)}</time></div>
      ${row.branch ? `<div class="hit-location"><span>${escapeHtml(row.branch)}</span><i>→</i><span>${escapeHtml(row.sub_branch || row.branch)}</span></div>` : '<div class="hit-location"><span>External vendor · branch not applicable</span></div>'}
      <div class="hit-card-metrics"><section><label>Hits</label>${deviceBreakdown(row.hits)}</section><section><label>Completes</label>${deviceBreakdown(row.completes)}</section></div>
    </article>`;
  }

  function updateOverview(summary) {
    elements.totalHits.textContent = number(summary.hits.total);
    elements.totalCompletes.textContent = number(summary.completes.total);
    elements.conversion.textContent = `${Number(summary.conversion_rate || 0).toLocaleString('en-IN')}%`;
    elements.activeUsers.textContent = number(summary.active_users);
    elements.dayCount.textContent = `${number(summary.days)} selected ${Number(summary.days) === 1 ? 'day' : 'days'}`;
  }

  async function loadHits() {
    state.controller?.abort();
    state.controller = new AbortController();
    elements.rows.innerHTML = '<tr><td colspan="6"><div class="table-loader"><i></i><span>Building device-wise totals…</span></div></td></tr>';
    try {
      const response = await fetch(`/api/v1/user-hits/?${filterParams()}`, { signal: state.controller.signal });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
      const results = data.results || [];
      const count = Number(data.count || 0);
      state.pages = Math.max(1, Math.ceil(count / state.pageSize));
      if (state.page > state.pages) { state.page = state.pages; return loadHits(); }
      updateOverview(data.summary || { hits: {}, completes: {} });
      elements.summary.innerHTML = count ? `<strong>${count.toLocaleString('en-IN')}</strong> user-day ${count === 1 ? 'record' : 'records'} match these filters` : 'No user activity matches these filters';
      if (results.length) {
        elements.rows.innerHTML = results.map(rowTemplate).join('');
        elements.cards.innerHTML = results.map(cardTemplate).join('');
      } else {
        elements.rows.innerHTML = '<tr><td colspan="6"><div class="empty-state"><span>◎</span><strong>No user hits found</strong><small>Try clearing filters or start a new survey journey.</small></div></td></tr>';
        elements.cards.innerHTML = '<div class="empty-state"><span>◎</span><strong>No user hits found</strong><small>Try clearing the filters.</small></div>';
      }
      elements.pageInput.value = state.page;
      elements.pageInput.max = state.pages;
      elements.totalPages.textContent = `of ${state.pages.toLocaleString('en-IN')}`;
      elements.pageStatus.textContent = `Page ${state.page.toLocaleString('en-IN')} of ${state.pages.toLocaleString('en-IN')}`;
      elements.first.disabled = elements.prev.disabled = state.page <= 1;
      elements.next.disabled = elements.last.disabled = state.page >= state.pages;
    } catch (error) {
      if (error.name === 'AbortError') return;
      elements.rows.innerHTML = `<tr><td colspan="6"><div class="error-state"><strong>Could not load user hits</strong><span>${escapeHtml(error.message)}</span><button type="button" id="retryUserHits">Try again</button></div></td></tr>`;
      byId('retryUserHits')?.addEventListener('click', loadHits);
      elements.cards.innerHTML = '';
    }
  }

  function scheduleLoad() {
    clearTimeout(state.timer);
    state.timer = setTimeout(() => { state.page = 1; loadHits(); }, 260);
  }

  function go(page) {
    state.page = Math.min(state.pages, Math.max(1, Number(page) || 1));
    loadHits();
    document.querySelector('.user-hits-panel')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  elements.search.addEventListener('input', scheduleLoad);
  [elements.from, elements.fromTime, elements.to, elements.toTime].forEach((input) => input.addEventListener('change', scheduleLoad));
  elements.pageSize.addEventListener('change', () => { state.pageSize = Number(elements.pageSize.value); state.page = 1; loadHits(); });
  elements.clear.addEventListener('click', () => {
    elements.search.value = ''; elements.from.value = ''; elements.fromTime.value = '';
    elements.to.value = ''; elements.toTime.value = '';
    document.querySelectorAll('.user-hits-filters .multi-select').forEach((container) => {
      container.querySelectorAll('input').forEach((input) => { input.checked = false; });
      updateMultiLabel(container);
    });
    closeMultiSelects(); state.page = 1; loadHits();
  });
  elements.first.addEventListener('click', () => go(1));
  elements.prev.addEventListener('click', () => go(state.page - 1));
  elements.next.addEventListener('click', () => go(state.page + 1));
  elements.last.addEventListener('click', () => go(state.pages));
  elements.pageInput.addEventListener('change', () => go(elements.pageInput.value));
  document.addEventListener('click', (event) => { if (!event.target.closest('.user-hits-filters .multi-select')) closeMultiSelects(); });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeMultiSelects(); });

  loadHits();
})();
