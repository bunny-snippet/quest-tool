(() => {
  const byId = (id) => document.getElementById(id);
  const elements = {
    search: byId('studySearch'), userFilters: document.querySelector('[data-multi-filter="user"]'),
    statusFilters: document.querySelector('[data-multi-filter="status"]'), dateField: byId('studyDateField'),
    from: byId('studyFromDate'), fromTime: byId('studyFromTime'),
    to: byId('studyToDate'), toTime: byId('studyToTime'), clear: byId('clearStudyFilters'),
    export: byId('exportStudies'), pageSize: byId('studyPageSize'), rows: byId('studyRows'),
    cards: byId('studyCards'), summary: byId('studySummary'), pageStatus: byId('studyPageStatus'),
    pageInput: byId('studyPageInput'), totalPages: byId('studyTotalPages'), first: byId('studyFirstPage'),
    prev: byId('studyPrevPage'), next: byId('studyNextPage'), last: byId('studyLastPage'),
  };
  if (!elements.rows) return;

  const state = { page: 1, pages: 1, pageSize: 20, timer: null, controller: null };
  const statusTone = { initiated: 'initiate', redirected: 'initiate', '1': 'complete', '2': 'terminate', '3': 'quota', '4': 'quality' };

  function escapeHtml(value) {
    const node = document.createElement('div');
    node.textContent = value == null ? '' : String(value);
    return node.innerHTML;
  }

  function selectedValues(container) {
    return container ? [...container.querySelectorAll('input:checked')].map((input) => input.value) : [];
  }

  function updateMultiLabel(container) {
    const checked = [...container.querySelectorAll('input:checked')];
    const button = container.querySelector('.multi-trigger');
    const fallback = container.dataset.multiFilter === 'user' ? 'All users' : 'All statuses';
    const label = checked.length === 0 ? fallback : checked.length === 1 ? checked[0].closest('label').innerText.trim() : `${checked.length} selected`;
    button.querySelector('span').textContent = label;
    button.classList.toggle('has-value', checked.length > 0);
  }

  function closeMultiSelects(except = null) {
    document.querySelectorAll('.multi-select.open').forEach((container) => {
      if (container === except) return;
      container.classList.remove('open');
      container.querySelector('.multi-menu').hidden = true;
      container.querySelector('.multi-trigger').setAttribute('aria-expanded', 'false');
    });
  }

  document.querySelectorAll('.multi-select').forEach((container) => {
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

  function dateBoundary(date, selectedTime, endOfMinute = false) {
    if (!date) return '';
    const clock = selectedTime || (endOfMinute ? '23:59' : '00:00');
    const seconds = endOfMinute ? '59.999' : '00';
    return `${date}T${clock}:${seconds}+05:30`;
  }

  function filterParams(includePage = true) {
    const params = new URLSearchParams();
    const search = elements.search.value.trim();
    const users = selectedValues(elements.userFilters);
    const statuses = selectedValues(elements.statusFilters);
    if (search) params.set('search', search);
    if (users.length) params.set('user', users.join(','));
    if (statuses.length) params.set('status', statuses.join(','));
    if (elements.from.value) params.set(`${elements.dateField.value}_from`, dateBoundary(elements.from.value, elements.fromTime.value));
    if (elements.to.value) params.set(`${elements.dateField.value}_to`, dateBoundary(elements.to.value, elements.toTime.value, true));
    params.set('ordering', '-initiated_at');
    if (includePage) {
      params.set('page', state.page);
      params.set('page_size', state.pageSize);
    }
    return params;
  }

  function formatIst(value, split = false) {
    if (!value) return split ? { date: '—', time: '' } : '—';
    const parsed = new Date(value);
    const date = new Intl.DateTimeFormat('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', year: 'numeric' }).format(parsed);
    const time = new Intl.DateTimeFormat('en-IN', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true }).format(parsed);
    return split ? { date, time } : `${date}, ${time}`;
  }

  function formatLoi(seconds) {
    if (seconds == null) return '—';
    const total = Number(seconds);
    const minutes = Math.floor(total / 60);
    const remainder = total % 60;
    return minutes ? `${minutes}m ${remainder}s` : `${remainder}s`;
  }

  function statusPill(attempt) {
    const tone = statusTone[attempt.status] || 'neutral';
    const label = ['initiated', 'redirected'].includes(attempt.status) ? 'Initiated' : (attempt.status_label || attempt.status);
    return `<span class="attempt-status ${tone}"><i></i>${escapeHtml(label)}</span>`;
  }

  function rowTemplate(attempt) {
    const started = formatIst(attempt.initiated_at, true);
    return `<tr>
    <td><strong class="study-project-id">${escapeHtml(attempt.survey_local_id)}</strong></td>
      <td><strong class="study-survey-id">${escapeHtml(attempt.survey_source_id)}</strong></td>
      <td><strong class="respondent-id">${escapeHtml(attempt.rid)}</strong></td>
      <td><strong class="study-user-name">${escapeHtml(attempt.user_name)}</strong><small class="study-secondary">${escapeHtml(attempt.user_email || attempt.username || `User #${attempt.user_id}`)}</small></td>
      <td><div class="ip-pair"><span><i>IN</i>${escapeHtml(attempt.entry_ip || '—')}</span><span><i>OUT</i>${escapeHtml(attempt.exit_ip || 'Awaiting')}</span></div></td>
      <td><strong class="study-loi">${formatLoi(attempt.loi_seconds)}</strong><small class="study-secondary">${attempt.loi_seconds == null ? 'Awaiting callback' : 'Actual duration'}</small></td>
      <td>${statusPill(attempt)}</td>
      <td><div class="study-timestamp"><strong>${started.date}</strong><span>${started.time} IST</span></div></td>
    </tr>`;
  }

  function cardTemplate(attempt) {
    return `<article class="survey-card study-card">
      <div class="study-card-head"><div><strong>${escapeHtml(attempt.rid)}</strong><span>Respondent ID</span></div>${statusPill(attempt)}</div>
      <div class="study-card-survey"><span>Survey ${escapeHtml(attempt.survey_source_id)}</span><strong>${escapeHtml(attempt.survey_local_id)}</strong><small>${escapeHtml(attempt.survey_name || attempt.company_name)}</small></div>
      <div class="study-card-grid">
        <span><small>User</small><b>${escapeHtml(attempt.user_name)}</b></span>
        <span><small>LOI</small><b>${formatLoi(attempt.loi_seconds)}</b></span>
        <span><small>Entry IP</small><b>${escapeHtml(attempt.entry_ip || '—')}</b></span>
        <span><small>Exit IP</small><b>${escapeHtml(attempt.exit_ip || 'Awaiting')}</b></span>
      </div>
      <time>${formatIst(attempt.initiated_at)} IST</time>
    </article>`;
  }

  async function loadAttempts() {
    state.controller?.abort();
    state.controller = new AbortController();
    elements.rows.innerHTML = '<tr><td colspan="8"><div class="table-loader"><i></i><span>Fetching respondent activity…</span></div></td></tr>';
    try {
      const response = await fetch(`/api/v1/survey-attempts/?${filterParams()}`, { signal: state.controller.signal });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
      const results = data.results || [];
      const count = Number(data.count || 0);
      state.pages = Math.max(1, Math.ceil(count / state.pageSize));
      if (state.page > state.pages) { state.page = state.pages; return loadAttempts(); }
      elements.summary.innerHTML = count ? `<strong>${count.toLocaleString('en-IN')}</strong> filtered respondent ${count === 1 ? 'journey' : 'journeys'}` : 'No attempts match these filters';
      if (results.length) {
        elements.rows.innerHTML = results.map(rowTemplate).join('');
        elements.cards.innerHTML = results.map(cardTemplate).join('');
      } else {
        elements.rows.innerHTML = '<tr><td colspan="8"><div class="empty-state"><span>◎</span><strong>No study records found</strong><small>Try clearing the filters or start a survey attempt.</small></div></td></tr>';
        elements.cards.innerHTML = '<div class="empty-state"><span>◎</span><strong>No study records found</strong><small>Try clearing the filters.</small></div>';
      }
      elements.pageInput.value = state.page;
      elements.pageInput.max = state.pages;
      elements.totalPages.textContent = `of ${state.pages.toLocaleString('en-IN')}`;
      elements.pageStatus.textContent = `Page ${state.page.toLocaleString('en-IN')} of ${state.pages.toLocaleString('en-IN')}`;
      elements.first.disabled = elements.prev.disabled = state.page <= 1;
      elements.next.disabled = elements.last.disabled = state.page >= state.pages;
    } catch (error) {
      if (error.name === 'AbortError') return;
      elements.rows.innerHTML = `<tr><td colspan="8"><div class="error-state"><strong>Could not load studies</strong><span>${escapeHtml(error.message)}</span><button type="button" id="retryStudies">Try again</button></div></td></tr>`;
      byId('retryStudies')?.addEventListener('click', loadAttempts);
      elements.cards.innerHTML = '';
    }
  }

  function scheduleLoad() {
    clearTimeout(state.timer);
    state.timer = setTimeout(() => { state.page = 1; loadAttempts(); }, 280);
  }

  function go(page) {
    state.page = Math.min(state.pages, Math.max(1, Number(page) || 1));
    loadAttempts();
    document.querySelector('.studies-panel')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  elements.search.addEventListener('input', scheduleLoad);
  [elements.from, elements.fromTime, elements.to, elements.toTime].forEach((input) => input.addEventListener('input', scheduleLoad));
  elements.dateField.addEventListener('change', scheduleLoad);
  elements.pageSize.addEventListener('change', () => { state.pageSize = Number(elements.pageSize.value); state.page = 1; loadAttempts(); });
  elements.clear.addEventListener('click', () => {
    elements.search.value = ''; elements.dateField.value = 'initiated';
    elements.from.value = ''; elements.fromTime.value = ''; elements.to.value = ''; elements.toTime.value = '';
    document.querySelectorAll('.studies-filters .multi-select').forEach((container) => {
      container.querySelectorAll('input').forEach((input) => { input.checked = false; });
      updateMultiLabel(container);
    });
    closeMultiSelects(); state.page = 1; loadAttempts();
  });
  elements.first.addEventListener('click', () => go(1));
  elements.prev.addEventListener('click', () => go(state.page - 1));
  elements.next.addEventListener('click', () => go(state.page + 1));
  elements.last.addEventListener('click', () => go(state.pages));
  elements.pageInput.addEventListener('change', () => go(elements.pageInput.value));
  elements.export?.addEventListener('click', () => {
    elements.export.classList.add('exporting');
    window.location.assign(`/api/v1/survey-attempts/export/?${filterParams(false)}`);
    setTimeout(() => elements.export.classList.remove('exporting'), 1000);
  });
  document.addEventListener('click', (event) => { if (!event.target.closest('.multi-select')) closeMultiSelects(); });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeMultiSelects(); });

  loadAttempts();
})();
