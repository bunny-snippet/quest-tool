/* Persistent, server-side report export queue. */
(() => {
  const storageKey = 'exchange-export-jobs-v1';
  const read = () => {
    try { return JSON.parse(localStorage.getItem(storageKey) || '[]'); } catch (_) { return []; }
  };
  const write = (jobs) => localStorage.setItem(storageKey, JSON.stringify(jobs.slice(-12)));
  const csrf = () => document.cookie.match(/csrftoken=([^;]+)/)?.[1] || '';
  const notify = (message, kind = 'success', action = null) => {
    const region = document.getElementById('toastRegion');
    if (!region) return;
    const node = document.createElement('div'); node.className = `toast ${kind}`;
    const text = document.createElement('span'); text.textContent = message; node.append(text);
    if (action) {
      const link = document.createElement('a'); link.href = action.url; link.textContent = action.label || 'Download';
      link.className = 'toast-action'; node.append(link);
    }
    region.append(node); window.setTimeout(() => node.remove(), action ? 14000 : 5000);
  };
  async function check(job) {
    const response = await fetch(job.status_url, { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok) throw new Error('Could not check export status.');
    return response.json();
  }
  async function poll() {
    const jobs = read(); let changed = false;
    for (const job of jobs) {
      if (['completed', 'failed'].includes(job.status)) continue;
      try {
        const data = await check(job);
        job.status = data.status; job.download_url = data.download_url || job.download_url; job.error = data.error || '';
        changed = true;
        if (data.status === 'completed' && !job.notified) {
          job.notified = true;
          notify('Your export is ready.', 'success', { url: data.download_url, label: 'Download' });
        } else if (data.status === 'failed' && !job.notified) {
          job.notified = true; notify(data.error || 'Export could not be created.', 'error');
        }
      } catch (_) { /* The next poll retries transient network errors. */ }
    }
    if (changed) write(jobs);
  }
  async function enqueue(kind, query = '') {
    const suffix = query ? `?${String(query).replace(/^\?/, '')}` : '';
    const response = await fetch(`/api/v1/export-jobs/${encodeURIComponent(kind)}/${suffix}`, {
      method: 'POST', credentials: 'same-origin', headers: { 'X-CSRFToken': csrf() },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Could not queue export.');
    const jobs = read();
    jobs.push({ id: data.id, status: data.status, status_url: data.status_url, download_url: data.download_url || '', notified: false });
    write(jobs); notify('Export queued. You can safely continue working.', 'success'); poll();
    return data;
  }
  window.ExportQueue = { enqueue, poll };
  poll(); window.setInterval(poll, 5000);
})();
