/**
 * app.js — CRM ↔ Zoho Books Integration UI
 *
 * Pure vanilla ES2020 JS. No frameworks.
 * All API calls target the FastAPI backend at /api/* endpoints.
 *
 * Modules (logical sections):
 *  1.  Config & State
 *  2.  Utility helpers
 *  3.  API layer
 *  4.  JSON syntax highlighter
 *  5.  Toast notifications
 *  6.  Tab navigation
 *  7.  Integration Log tab
 *  8.  Deals Dashboard tab
 *  9.  Collections tab
 * 10.  Jobs Queue tab
 * 11.  Auto-refresh manager
 * 12.  Bootstrap / init
 */

'use strict';

/* ============================================================
   1. Config & State
   ============================================================ */

/** Base URL for API calls — empty string = same origin */
const API_BASE = '';

const PAGE_SIZE = 50;

/** Shared application state */
const state = {
  /** Integration Log */
  logs: {
    items:       [],
    total:       0,
    page:        1,
    loading:     false,
    error:       null,
    filters:     { dealId: '', system: '', status: '', dateFrom: '', dateTo: '' },
    expandedRow: null,   // row index currently expanded
  },
  /** Deals Dashboard */
  deals: {
    items:   [],
    loading: false,
    error:   null,
    search:  '',
    filter:  '',
  },
  /** Collections */
  collections: {
    items:    [],
    loading:  false,
    error:    null,
    sortCol:  'total_outstanding',
    sortDir:  'desc',
  },
  /** Jobs Queue */
  jobs: {
    items:   [],
    loading: false,
    error:   null,
  },
  /** Auto-refresh timers */
  refresh: {
    logsEnabled: false,
    logsTimer:   null,
    jobsTimer:   null,   // always-on 10 s for jobs tab
  },
  /** Active tab */
  activeTab: 'logs',
};

/* ============================================================
   2. Utility helpers
   ============================================================ */

/**
 * Format an ISO timestamp as a human-readable local string.
 * @param {string} iso
 * @returns {string}
 */
function fmtDateTime(iso) {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
      month: '2-digit', day: '2-digit', year: 'numeric',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
  } catch { return iso; }
}

/**
 * Format a number as AED currency with commas.
 * @param {number|string} val
 * @returns {string}
 */
function fmtAED(val) {
  const n = parseFloat(val);
  if (isNaN(n)) return '—';
  return 'AED ' + n.toLocaleString('en-AE', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/**
 * Determine if a date string represents an overdue date.
 * @param {string} dateStr
 * @returns {boolean}
 */
function isOverdue(dateStr) {
  if (!dateStr) return false;
  return new Date(dateStr) < new Date();
}

/**
 * Clamp a numeric value between 0 and 100.
 * @param {number} n
 * @returns {number}
 */
function clamp100(n) {
  return Math.min(100, Math.max(0, n || 0));
}

/**
 * Escape HTML special characters (used before injecting text into innerHTML).
 * @param {string} str
 * @returns {string}
 */
function escHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/**
 * Build a query string from an object, omitting empty values.
 * @param {Record<string,string|number>} params
 * @returns {string}
 */
function buildQS(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== '' && v !== null && v !== undefined) p.set(k, String(v));
  }
  const s = p.toString();
  return s ? '?' + s : '';
}

/**
 * Deep-clone a plain JS object (no circular refs).
 * @template T
 * @param {T} obj
 * @returns {T}
 */
function clone(obj) { return JSON.parse(JSON.stringify(obj)); }

/**
 * Debounce a function.
 * @param {Function} fn
 * @param {number} ms
 * @returns {Function}
 */
function debounce(fn, ms) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

/* ============================================================
   3. API Layer
   ============================================================ */

/**
 * Generic fetch wrapper with error handling and JSON parsing.
 *
 * @param {string} path         - URL path (relative to API_BASE)
 * @param {RequestInit} [opts]  - fetch options
 * @returns {Promise<any>}      - parsed JSON response body
 * @throws {Error}              - enriched error with .status and .detail
 */
async function apiFetch(path, opts = {}) {
  const url = API_BASE + path;
  const resp = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...opts.headers },
    ...opts,
  });

  let body;
  try { body = await resp.json(); } catch { body = null; }

  if (!resp.ok) {
    const err = new Error(
      (body && (body.detail || body.message)) || `HTTP ${resp.status}`
    );
    err.status = resp.status;
    err.detail = body;
    throw err;
  }
  return body;
}

/**
 * Fetch integration logs with optional filters and pagination.
 *
 * @param {object} filters
 * @param {string} [filters.dealId]
 * @param {string} [filters.system]    - 'zoho' | 'hubspot' | ''
 * @param {string} [filters.status]    - 'success' | 'error' | ''
 * @param {string} [filters.dateFrom]
 * @param {string} [filters.dateTo]
 * @param {number} [filters.page]
 * @param {number} [filters.pageSize]
 * @returns {Promise<{items: object[], total: number}>}
 */
async function fetchLogs(filters = {}) {
  const qs = buildQS({
    deal_id:   filters.dealId   || '',
    system:    filters.system   || '',
    status:    filters.status   || '',
    date_from: filters.dateFrom || '',
    date_to:   filters.dateTo   || '',
    page:      filters.page     || 1,
    page_size: filters.pageSize || PAGE_SIZE,
  });
  return apiFetch(`/logs${qs}`);
}

/**
 * Fetch all integrated deals (optionally filter by search/status server-side).
 *
 * @returns {Promise<object[]>}
 */
async function fetchDeals() {
  return apiFetch('/deals');
}

/**
 * Fetch per-customer collections / aging data.
 *
 * @returns {Promise<object[]>}
 */
async function fetchCollections() {
  return apiFetch('/collections');
}

/**
 * Fetch the jobs queue.
 *
 * @returns {Promise<object[]>}
 */
async function fetchJobs() {
  return apiFetch('/jobs');
}

/**
 * Retry a failed job.
 *
 * @param {string|number} jobId
 * @returns {Promise<object>}
 */
async function retryJob(jobId) {
  return apiFetch(`/jobs/${encodeURIComponent(jobId)}/retry`, { method: 'POST' });
}

/**
 * Trigger an immediate sync for a specific deal.
 *
 * @param {string} dealId
 * @returns {Promise<object>}
 */
async function syncDeal(dealId) {
  return apiFetch(`/deals/${encodeURIComponent(dealId)}/sync`, { method: 'POST' });
}

/**
 * Trigger a global sync-all operation.
 *
 * @returns {Promise<object>}
 */
async function triggerSyncAll() {
  return apiFetch('/sync/all', { method: 'POST' });
}

/* ============================================================
   4. JSON Syntax Highlighter
   ============================================================ */

/**
 * Pretty-print a JS value and wrap tokens in <span> elements for syntax
 * colouring. Returns safe HTML string.
 *
 * @param {unknown} value  - already parsed JS value (or raw string)
 * @returns {string}       - HTML string
 */
function syntaxHighlight(value) {
  let obj = value;
  if (typeof value === 'string') {
    try { obj = JSON.parse(value); } catch { /* leave as string */ }
  }

  const raw = JSON.stringify(obj, null, 2) ?? 'null';

  return raw.replace(
    /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[{}[\],])/g,
    (match) => {
      if (/^"/.test(match)) {
        if (/:$/.test(match)) {
          // object key — strip the trailing colon, add it back plain
          return `<span class="json-key">${escHtml(match.slice(0, -1))}</span>:`;
        }
        return `<span class="json-string">${escHtml(match)}</span>`;
      }
      if (/true|false/.test(match)) return `<span class="json-bool">${match}</span>`;
      if (/null/.test(match))       return `<span class="json-null">null</span>`;
      if (/[{}[\]]/.test(match))    return `<span class="json-brace">${match}</span>`;
      return `<span class="json-number">${match}</span>`;
    }
  );
}

/**
 * Render a JSON viewer element containing highlighted JSON.
 *
 * @param {unknown} data
 * @param {string}  [title]
 * @returns {string} HTML
 */
function jsonViewerHtml(data, title) {
  const titleHtml = title ? `<h4>${escHtml(title)}</h4>` : '';
  return `
    <div class="json-section">
      ${titleHtml}
      <div class="json-viewer">${syntaxHighlight(data)}</div>
    </div>`;
}

/* ============================================================
   5. Toast Notifications
   ============================================================ */

/**
 * Show a transient toast notification.
 *
 * @param {string} message
 * @param {'success'|'error'|'info'} [type='info']
 * @param {number} [durationMs=4000]
 */
function showToast(message, type = 'info', durationMs = 4000) {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const icons = { success: '✓', error: '✗', info: 'ℹ' };
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.innerHTML = `<span style="font-size:1rem">${icons[type] ?? icons.info}</span>
    <span>${escHtml(message)}</span>`;
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateX(20px)';
    toast.style.transition = 'opacity 0.25s, transform 0.25s';
    setTimeout(() => toast.remove(), 260);
  }, durationMs);
}

/* ============================================================
   6. Tab Navigation
   ============================================================ */

/**
 * Switch to a named tab, update aria/active states, and load its data.
 * @param {string} tabName  - 'logs' | 'deals' | 'collections' | 'jobs'
 */
function switchTab(tabName) {
  state.activeTab = tabName;

  // Update nav buttons
  document.querySelectorAll('#tab-nav button').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tabName);
  });

  // Show/hide panels
  document.querySelectorAll('.tab-panel').forEach(panel => {
    panel.classList.toggle('active', panel.id === `panel-${tabName}`);
  });

  // Trigger data load for the newly visible tab
  switch (tabName) {
    case 'logs':        loadLogs();        break;
    case 'deals':       loadDeals();       break;
    case 'collections': loadCollections(); break;
    case 'jobs':        loadJobs();        break;
  }
}

/* ============================================================
   7. Integration Log Tab
   ============================================================ */

/** Render the stats bar above the logs table. */
function renderLogStats(items, total) {
  const successCount = items.filter(l => l.success).length;
  const successRate  = items.length ? Math.round((successCount / items.length) * 100) : 0;
  const avgDuration  = items.length
    ? Math.round(items.reduce((s, l) => s + (l.duration_ms || 0), 0) / items.length)
    : 0;

  document.getElementById('stat-total-calls').textContent  = total.toLocaleString();
  document.getElementById('stat-success-rate').textContent = successRate + '%';
  document.getElementById('stat-avg-rt').textContent       = avgDuration + ' ms';
  document.getElementById('stat-errors').textContent       =
    items.filter(l => !l.success).length.toLocaleString();
}

/**
 * Build a single log row. Returns an HTMLTableRowElement.
 * @param {object} log
 * @param {number} index
 * @returns {HTMLTableRowElement}
 */
function buildLogRow(log, index) {
  const tr = document.createElement('tr');
  tr.dataset.index = index;

  const systemBadge = log.system === 'zoho'
    ? `<span class="badge badge-zoho">Zoho</span>`
    : `<span class="badge badge-hubspot">HubSpot</span>`;

  const dirBadge = log.direction === 'outbound'
    ? `<span class="badge badge-primary">↑ Out</span>`
    : `<span class="badge badge-gray">↓ In</span>`;

  const statusIcon = log.success
    ? `<span class="status-ok" title="Success">✓</span>`
    : `<span class="status-fail" title="Error ${escHtml(String(log.status_code || ''))}">✗</span>`;

  const dealLink = log.deal_id
    ? `<a href="/deals/${encodeURIComponent(log.deal_id)}" onclick="event.stopPropagation();"
         title="View deal">${escHtml(log.deal_id)}</a>`
    : '<span class="text-muted">—</span>';

  const duration = log.duration_ms != null
    ? `<span class="${log.duration_ms > 2000 ? 'text-warn' : ''}">${log.duration_ms}</span>`
    : '—';

  tr.innerHTML = `
    <td class="mono muted">${escHtml(fmtDateTime(log.timestamp))}</td>
    <td>${dirBadge}</td>
    <td>${systemBadge}</td>
    <td class="mono">${escHtml(log.method || '—')}</td>
    <td class="mono" style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"
        title="${escHtml(log.endpoint || '')}">${escHtml(log.endpoint || '—')}</td>
    <td>${statusIcon}</td>
    <td>${dealLink}</td>
    <td class="text-right">${duration}</td>
    <td class="text-muted" style="font-size:0.8rem">▶</td>
  `;

  tr.addEventListener('click', () => toggleLogDetail(log, tr, index));
  return tr;
}

/**
 * Toggle inline expansion of a log row showing request/response JSON.
 *
 * @param {object} log
 * @param {HTMLTableRowElement} tr
 * @param {number} index
 */
function toggleLogDetail(log, tr, index) {
  // Collapse any already-open row
  const existing = document.querySelector('tr.detail-row');
  if (existing) {
    existing.remove();
    const prevExpanded = document.querySelector('tr.expanded');
    if (prevExpanded) prevExpanded.classList.remove('expanded');
    if (state.logs.expandedRow === index) {
      state.logs.expandedRow = null;
      return; // toggle off
    }
  }

  state.logs.expandedRow = index;
  tr.classList.add('expanded');

  const detailTr = document.createElement('tr');
  detailTr.className = 'detail-row';

  const colCount = tr.cells.length;
  const td = document.createElement('td');
  td.colSpan = colCount;

  td.innerHTML = `
    <div class="detail-inner">
      <div style="display:flex;align-items:center;gap:0.75rem;margin-bottom:0.75rem;flex-wrap:wrap;">
        <strong style="font-size:0.82rem;">Log Detail</strong>
        ${log.system ? `<span class="badge badge-${log.system === 'zoho' ? 'zoho' : 'hubspot'}">${escHtml(log.system)}</span>` : ''}
        ${log.status_code ? `<span class="badge badge-${log.success ? 'success' : 'error'}">HTTP ${log.status_code}</span>` : ''}
        <span class="text-muted" style="font-size:0.78rem;">
          ${log.duration_ms != null ? log.duration_ms + ' ms' : ''}
        </span>
      </div>
      <div class="log-detail-grid">
        ${jsonViewerHtml(log.request_body  ?? log.request  ?? null, 'Request')}
        ${jsonViewerHtml(log.response_body ?? log.response ?? null, 'Response')}
      </div>
    </div>
  `;

  detailTr.appendChild(td);
  tr.insertAdjacentElement('afterend', detailTr);
}

/** Render pagination controls for the logs table. */
function renderLogPagination(total) {
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const current    = state.logs.page;

  const wrap = document.getElementById('logs-pagination');
  if (!wrap) return;

  const from = (current - 1) * PAGE_SIZE + 1;
  const to   = Math.min(current * PAGE_SIZE, total);

  // Build page number buttons (show at most 7)
  const pageBtns = [];
  const range = pageRange(current, totalPages);
  for (const p of range) {
    if (p === '…') {
      pageBtns.push(`<span style="padding:0 4px;color:var(--text-muted)">…</span>`);
    } else {
      pageBtns.push(`
        <button class="page-btn ${p === current ? 'active' : ''}"
                data-page="${p}">${p}</button>`);
    }
  }

  wrap.innerHTML = `
    <span class="pagination-info">
      Showing ${from}–${to} of ${total.toLocaleString()} records
    </span>
    <div class="pagination-controls">
      <button class="page-btn" data-page="${current - 1}" ${current <= 1 ? 'disabled' : ''}>‹</button>
      ${pageBtns.join('')}
      <button class="page-btn" data-page="${current + 1}" ${current >= totalPages ? 'disabled' : ''}>›</button>
    </div>
  `;

  wrap.querySelectorAll('[data-page]').forEach(btn => {
    btn.addEventListener('click', () => {
      const p = parseInt(btn.dataset.page, 10);
      if (!isNaN(p) && p >= 1 && p <= totalPages) {
        state.logs.page = p;
        loadLogs();
      }
    });
  });
}

/**
 * Generate a concise page number range with ellipsis.
 * @param {number} current
 * @param {number} total
 * @returns {(number|string)[]}
 */
function pageRange(current, total) {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const pages = new Set([1, total, current - 1, current, current + 1].filter(p => p >= 1 && p <= total));
  const sorted = [...pages].sort((a, b) => a - b);
  const result = [];
  let prev = null;
  for (const p of sorted) {
    if (prev !== null && p - prev > 1) result.push('…');
    result.push(p);
    prev = p;
  }
  return result;
}

/** Load and render the integration logs. */
async function loadLogs() {
  if (state.logs.loading) return;
  state.logs.loading = true;

  const tbody  = document.getElementById('logs-tbody');
  const errDiv = document.getElementById('logs-error');
  if (!tbody) return;

  // Show skeleton
  tbody.innerHTML = Array(8).fill(
    `<tr><td colspan="9"><div class="skeleton skeleton-row"></div></td></tr>`
  ).join('');
  if (errDiv) errDiv.classList.add('hidden');

  try {
    const res = await fetchLogs({
      ...state.logs.filters,
      page:     state.logs.page,
      pageSize: PAGE_SIZE,
    });

    const items = Array.isArray(res) ? res : (res.items || []);
    const total = typeof res.total === 'number' ? res.total : items.length;

    state.logs.items = items;
    state.logs.total = total;

    tbody.innerHTML = '';
    if (items.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9">
        <div class="empty-state">
          <div class="empty-icon">📭</div>
          <p>No log entries match your filters.</p>
        </div>
      </td></tr>`;
    } else {
      items.forEach((log, i) => tbody.appendChild(buildLogRow(log, i)));
    }

    renderLogStats(items, total);
    renderLogPagination(total);
    updateConnectionStatus(true);
  } catch (err) {
    state.logs.error = err;
    tbody.innerHTML = '';
    if (errDiv) {
      errDiv.classList.remove('hidden');
      errDiv.querySelector('.err-msg').textContent  = err.message;
      errDiv.querySelector('.err-detail').textContent =
        err.detail ? JSON.stringify(err.detail) : '';
    }
    updateConnectionStatus(false);
  } finally {
    state.logs.loading = false;
  }
}

/** Wire up log filter controls. */
function initLogFilters() {
  const applyFilters = debounce(() => {
    state.logs.page = 1;
    state.logs.expandedRow = null;
    loadLogs();
  }, 350);

  const dealInput   = document.getElementById('filter-deal-id');
  const sysSelect   = document.getElementById('filter-system');
  const statSelect  = document.getElementById('filter-status');
  const dateFrom    = document.getElementById('filter-date-from');
  const dateTo      = document.getElementById('filter-date-to');
  const clearBtn    = document.getElementById('filter-clear');

  [dealInput, sysSelect, statSelect, dateFrom, dateTo].forEach(el => {
    if (!el) return;
    el.addEventListener('input', () => {
      state.logs.filters.dealId   = dealInput?.value   ?? '';
      state.logs.filters.system   = sysSelect?.value   ?? '';
      state.logs.filters.status   = statSelect?.value  ?? '';
      state.logs.filters.dateFrom = dateFrom?.value    ?? '';
      state.logs.filters.dateTo   = dateTo?.value      ?? '';
      applyFilters();
    });
  });

  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      [dealInput, sysSelect, statSelect, dateFrom, dateTo].forEach(el => { if (el) el.value = ''; });
      state.logs.filters = { dealId: '', system: '', status: '', dateFrom: '', dateTo: '' };
      state.logs.page    = 1;
      loadLogs();
    });
  }

  // Auto-refresh toggle
  const toggle = document.getElementById('logs-autorefresh');
  if (toggle) {
    toggle.addEventListener('change', () => {
      state.refresh.logsEnabled = toggle.checked;
      if (toggle.checked) {
        state.refresh.logsTimer = setInterval(loadLogs, 30_000);
        showToast('Auto-refresh enabled (every 30s)', 'info', 2500);
      } else {
        clearInterval(state.refresh.logsTimer);
        state.refresh.logsTimer = null;
      }
    });
  }
}

/* ============================================================
   8. Deals Dashboard Tab
   ============================================================ */

/**
 * Build HTML for a single deal card.
 * @param {object} deal
 * @returns {string}
 */
function buildDealCard(deal) {
  const pctInvoiced  = clamp100(deal.pct_invoiced  || 0);
  const pctCollected = clamp100(deal.pct_collected || 0);
  const unpaid       = parseFloat(deal.amount_unpaid || 0);
  const overdue      = isOverdue(deal.next_due_date);

  const syncBadge = (() => {
    switch (deal.sync_status) {
      case 'ok':      return `<span class="badge badge-success">✓ OK</span>`;
      case 'pending': return `<span class="badge badge-warning">↻ Pending</span>`;
      case 'error':   return `<span class="badge badge-error">✗ Error</span>`;
      default:        return `<span class="badge badge-gray">${escHtml(deal.sync_status || '?')}</span>`;
    }
  })();

  const unpaidClass = unpaid > 0 ? 'overdue' : '';
  const dueDateClass = overdue ? 'overdue' : '';

  const hubspotUrl = deal.hubspot_id
    ? `https://app.hubspot.com/contacts/${encodeURIComponent(deal.hubspot_id)}`
    : null;
  const zohoUrl = deal.zoho_so_number
    ? `https://books.zoho.com/app#/salesorders`
    : null;

  return `
    <div class="deal-card">
      <div class="card-header">
        <div>
          <div class="deal-name">${escHtml(deal.deal_name || deal.name || '—')}</div>
          <div class="deal-customer">${escHtml(deal.customer || deal.contact_name || '—')}</div>
        </div>
        ${syncBadge}
      </div>

      <div style="display:flex;justify-content:space-between;align-items:baseline;gap:0.5rem;flex-wrap:wrap;">
        <div class="deal-amount">${fmtAED(deal.amount)} <span>total</span></div>
        ${deal.zoho_so_number
          ? `<div class="so-number">#${escHtml(deal.zoho_so_number)}</div>`
          : '<div class="so-number text-muted">No SO</div>'
        }
      </div>

      <div class="metrics">
        <div class="metric-row">
          <span class="metric-label">% Invoiced</span>
          <div class="progress-wrap" style="flex:1">
            <div class="progress-bar">
              <div class="progress-fill green" style="width:${pctInvoiced}%"></div>
            </div>
            <span class="progress-pct">${pctInvoiced}%</span>
          </div>
        </div>
        <div class="metric-row">
          <span class="metric-label">% Collected</span>
          <div class="progress-wrap" style="flex:1">
            <div class="progress-bar">
              <div class="progress-fill blue" style="width:${pctCollected}%"></div>
            </div>
            <span class="progress-pct">${pctCollected}%</span>
          </div>
        </div>
        <div class="metric-row">
          <span class="metric-label">Unpaid</span>
          <span class="unpaid-amount ${unpaidClass}">${fmtAED(unpaid)}</span>
        </div>
      </div>

      <div class="meta-row">
        <span class="due-date ${dueDateClass}">
          ${deal.next_due_date
            ? `${overdue ? '⚠ ' : ''}Due: ${escHtml(deal.next_due_date)}`
            : 'No due date'
          }
        </span>
        <span>Synced: ${escHtml(fmtDateTime(deal.last_synced))}</span>
      </div>

      <div class="card-footer">
        <button class="btn btn-primary btn-sm" onclick="handleSyncDeal('${escHtml(deal.deal_id || deal.id || '')}', this)">
          ↻ Sync
        </button>
        ${hubspotUrl
          ? `<a href="${escHtml(hubspotUrl)}" target="_blank" rel="noopener" class="btn btn-secondary btn-sm">HubSpot ↗</a>`
          : ''
        }
        ${zohoUrl
          ? `<a href="${escHtml(zohoUrl)}" target="_blank" rel="noopener" class="btn btn-secondary btn-sm">Zoho SO ↗</a>`
          : ''
        }
      </div>
    </div>
  `;
}

/**
 * Handle "Sync Now" button click for a deal card.
 * @param {string} dealId
 * @param {HTMLButtonElement} btn
 */
async function handleSyncDeal(dealId, btn) {
  if (!dealId) return;
  btn.disabled = true;
  btn.textContent = '…';
  try {
    await syncDeal(dealId);
    showToast(`Deal ${dealId} sync triggered`, 'success');
    setTimeout(loadDeals, 1500);
  } catch (err) {
    showToast(`Sync failed: ${err.message}`, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = '↻ Sync';
  }
}

/** Load and render deals. */
async function loadDeals() {
  if (state.deals.loading) return;
  state.deals.loading = true;

  const container = document.getElementById('deals-container');
  const errDiv    = document.getElementById('deals-error');
  if (!container) return;

  // Skeleton
  container.innerHTML = Array(6).fill(
    `<div class="skeleton skeleton-card"></div>`
  ).join('');
  if (errDiv) errDiv.classList.add('hidden');

  try {
    const items = await fetchDeals();
    state.deals.items = Array.isArray(items) ? items : (items.items || []);
    renderDeals();
  } catch (err) {
    state.deals.error = err;
    container.innerHTML = '';
    if (errDiv) {
      errDiv.classList.remove('hidden');
      errDiv.querySelector('.err-msg').textContent = err.message;
      errDiv.querySelector('.err-detail').textContent =
        err.detail ? JSON.stringify(err.detail) : '';
    }
  } finally {
    state.deals.loading = false;
  }
}

/** Filter and render deals based on current search/filter state. */
function renderDeals() {
  const container = document.getElementById('deals-container');
  if (!container) return;

  const search = (state.deals.search || '').toLowerCase();
  const filter = state.deals.filter || '';

  let items = state.deals.items;
  if (search) {
    items = items.filter(d =>
      (d.deal_name || d.name || '').toLowerCase().includes(search) ||
      (d.customer  || d.contact_name || '').toLowerCase().includes(search)
    );
  }
  if (filter) {
    items = items.filter(d => d.sync_status === filter);
  }

  if (items.length === 0) {
    container.innerHTML = `
      <div class="empty-state" style="grid-column:1/-1">
        <div class="empty-icon">🔍</div>
        <p>No deals match your search.</p>
      </div>`;
    return;
  }

  container.innerHTML = items.map(buildDealCard).join('');

  // Update count badge
  const countEl = document.getElementById('deals-count');
  if (countEl) countEl.textContent = `${items.length} deal${items.length !== 1 ? 's' : ''}`;
}

/** Wire up deals filter/search controls. */
function initDealsFilters() {
  const searchInput = document.getElementById('deals-search');
  const filterSel   = document.getElementById('deals-filter-status');

  if (searchInput) {
    searchInput.addEventListener('input', debounce(() => {
      state.deals.search = searchInput.value;
      renderDeals();
    }, 250));
  }
  if (filterSel) {
    filterSel.addEventListener('change', () => {
      state.deals.filter = filterSel.value;
      renderDeals();
    });
  }
}

/* ============================================================
   9. Collections Tab
   ============================================================ */

/**
 * Build an aging amount cell with colour coding.
 * @param {number} amount
 * @param {'ok'|'warn'|'overdue'} severity
 * @returns {string}
 */
function agingCell(amount, severity = 'ok') {
  if (amount === 0 || amount == null) return `<td class="aging-cell text-muted">0.00</td>`;
  return `<td class="aging-cell ${severity}">${fmtAED(amount)}</td>`;
}

/**
 * Build a table row for a collection record.
 * @param {object} row
 * @returns {string}
 */
function buildCollectionRow(row) {
  const deals = Array.isArray(row.associated_deals)
    ? row.associated_deals.map(d => `<span class="badge badge-gray" style="margin:1px">${escHtml(d)}</span>`).join('')
    : escHtml(row.associated_deals || '—');

  return `
    <tr>
      <td class="fw-600">${escHtml(row.customer || '—')}</td>
      ${agingCell(row.total_outstanding, row.total_outstanding > 0 ? 'overdue' : 'ok')}
      ${agingCell(row.current_due,    'ok')}
      ${agingCell(row.days_1_30,      row.days_1_30    > 0 ? 'warn'   : 'ok')}
      ${agingCell(row.days_31_60,     row.days_31_60   > 0 ? 'overdue': 'ok')}
      ${agingCell(row.days_61_90,     row.days_61_90   > 0 ? 'overdue': 'ok')}
      ${agingCell(row.days_90_plus,   row.days_90_plus > 0 ? 'overdue': 'ok')}
      <td class="mono muted" style="font-size:0.78rem">${escHtml(row.oldest_invoice || '—')}</td>
      <td>${deals}</td>
    </tr>
  `;
}

/** Load and render collections. */
async function loadCollections() {
  if (state.collections.loading) return;
  state.collections.loading = true;

  const tbody  = document.getElementById('collections-tbody');
  const errDiv = document.getElementById('collections-error');
  if (!tbody) return;

  tbody.innerHTML = Array(5).fill(
    `<tr><td colspan="9"><div class="skeleton skeleton-row"></div></td></tr>`
  ).join('');
  if (errDiv) errDiv.classList.add('hidden');

  try {
    const items = await fetchCollections();
    state.collections.items = Array.isArray(items) ? items : (items.items || []);
    renderCollections();
  } catch (err) {
    state.collections.error = err;
    tbody.innerHTML = '';
    if (errDiv) {
      errDiv.classList.remove('hidden');
      errDiv.querySelector('.err-msg').textContent = err.message;
      errDiv.querySelector('.err-detail').textContent =
        err.detail ? JSON.stringify(err.detail) : '';
    }
  } finally {
    state.collections.loading = false;
  }
}

/** Sort and render collections table. */
function renderCollections() {
  const tbody = document.getElementById('collections-tbody');
  if (!tbody) return;

  const { sortCol, sortDir } = state.collections;
  const items = [...state.collections.items].sort((a, b) => {
    const av = a[sortCol] ?? 0;
    const bv = b[sortCol] ?? 0;
    const cmp = typeof av === 'string' ? av.localeCompare(bv) : av - bv;
    return sortDir === 'asc' ? cmp : -cmp;
  });

  if (items.length === 0) {
    tbody.innerHTML = `<tr><td colspan="9">
      <div class="empty-state">
        <div class="empty-icon">📋</div>
        <p>No outstanding collections found.</p>
      </div>
    </td></tr>`;
    return;
  }

  tbody.innerHTML = items.map(buildCollectionRow).join('');

  // Update summary totals
  const totalOutstanding = items.reduce((s, r) => s + (parseFloat(r.total_outstanding) || 0), 0);
  const summaryEl = document.getElementById('collections-summary');
  if (summaryEl) {
    summaryEl.textContent = `Total outstanding across all customers: ${fmtAED(totalOutstanding)}`;
  }
}

/** Wire up sortable column headers in the collections table. */
function initCollectionsSorting() {
  document.querySelectorAll('#collections-table th.sortable').forEach(th => {
    th.addEventListener('click', () => {
      const col = th.dataset.col;
      if (state.collections.sortCol === col) {
        state.collections.sortDir = state.collections.sortDir === 'asc' ? 'desc' : 'asc';
      } else {
        state.collections.sortCol = col;
        state.collections.sortDir = 'desc';
      }
      // Update header classes
      document.querySelectorAll('#collections-table th.sortable').forEach(t => {
        t.classList.remove('sort-asc', 'sort-desc');
      });
      th.classList.add(`sort-${state.collections.sortDir}`);
      renderCollections();
    });
  });
}

/** Export collections table to CSV and trigger download. */
function exportCollectionsCSV() {
  const items = state.collections.items;
  if (!items.length) { showToast('No data to export', 'info'); return; }

  const headers = [
    'Customer', 'Total Outstanding', 'Current', '1-30 Days',
    '31-60 Days', '61-90 Days', '90+ Days', 'Oldest Invoice', 'Associated Deals',
  ];

  const rows = items.map(r => [
    r.customer           || '',
    r.total_outstanding  ?? 0,
    r.current_due        ?? 0,
    r.days_1_30          ?? 0,
    r.days_31_60         ?? 0,
    r.days_61_90         ?? 0,
    r.days_90_plus       ?? 0,
    r.oldest_invoice     || '',
    Array.isArray(r.associated_deals) ? r.associated_deals.join('; ') : (r.associated_deals || ''),
  ].map(v => `"${String(v).replace(/"/g, '""')}"`).join(','));

  const csv  = [headers.join(','), ...rows].join('\r\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href     = url;
  a.download = `collections_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
  showToast('CSV exported', 'success');
}

/* ============================================================
   10. Jobs Queue Tab
   ============================================================ */

/**
 * Return HTML for a job status badge.
 * @param {string} status
 * @returns {string}
 */
function jobStatusBadge(status) {
  switch (status) {
    case 'pending':  return `<span class="badge badge-warning">● Pending</span>`;
    case 'running':  return `<span class="badge badge-running">Running</span>`;
    case 'done':     return `<span class="badge badge-success">✓ Done</span>`;
    case 'failed':   return `<span class="badge badge-error">✗ Failed</span>`;
    case 'skipped':  return `<span class="badge badge-gray">— Skipped</span>`;
    default:         return `<span class="badge badge-gray">${escHtml(status || '?')}</span>`;
  }
}

/**
 * Build a jobs table row.
 * @param {object} job
 * @returns {string}
 */
function buildJobRow(job) {
  const retryBtn = job.status === 'failed'
    ? `<button class="btn btn-danger btn-sm" onclick="handleRetryJob('${escHtml(String(job.id || ''))}', this)">
         ↻ Retry
       </button>`
    : '';

  const errorHtml = job.error
    ? `<span class="job-error-text" title="${escHtml(job.error)}">${escHtml(job.error)}</span>`
    : '<span class="text-muted">—</span>';

  return `
    <tr>
      <td class="mono" style="font-size:0.78rem">${escHtml(String(job.id || '—'))}</td>
      <td>${escHtml(job.type || '—')}</td>
      <td>
        ${job.deal_id
          ? `<a href="#" onclick="switchTab('logs');document.getElementById('filter-deal-id').value='${escHtml(job.deal_id)}';loadLogs();return false;">${escHtml(job.deal_id)}</a>`
          : '<span class="text-muted">—</span>'
        }
      </td>
      <td>${jobStatusBadge(job.status)}</td>
      <td class="text-center">${job.retry_count ?? 0}</td>
      <td class="mono muted" style="font-size:0.78rem">${escHtml(fmtDateTime(job.created_at || job.created))}</td>
      <td class="mono muted" style="font-size:0.78rem">${escHtml(fmtDateTime(job.next_retry))}</td>
      <td>${errorHtml}</td>
      <td>${retryBtn}</td>
    </tr>
  `;
}

/**
 * Handle "Retry" click for a failed job.
 * @param {string} jobId
 * @param {HTMLButtonElement} btn
 */
async function handleRetryJob(jobId, btn) {
  btn.disabled    = true;
  btn.textContent = '…';
  try {
    await retryJob(jobId);
    showToast(`Job ${jobId} queued for retry`, 'success');
    setTimeout(loadJobs, 1000);
  } catch (err) {
    showToast(`Retry failed: ${err.message}`, 'error');
    btn.disabled    = false;
    btn.textContent = '↻ Retry';
  }
}

/** Load and render jobs. */
async function loadJobs() {
  if (state.jobs.loading) return;
  state.jobs.loading = true;

  const tbody  = document.getElementById('jobs-tbody');
  const errDiv = document.getElementById('jobs-error');
  if (!tbody) return;

  tbody.innerHTML = Array(5).fill(
    `<tr><td colspan="9"><div class="skeleton skeleton-row"></div></td></tr>`
  ).join('');
  if (errDiv) errDiv.classList.add('hidden');

  try {
    const items = await fetchJobs();
    state.jobs.items = Array.isArray(items) ? items : (items.items || []);

    tbody.innerHTML = '';
    if (state.jobs.items.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9">
        <div class="empty-state">
          <div class="empty-icon">✅</div>
          <p>Job queue is empty — all caught up!</p>
        </div>
      </td></tr>`;
    } else {
      tbody.innerHTML = state.jobs.items.map(buildJobRow).join('');
    }

    // Update counter badges
    const pending = state.jobs.items.filter(j => j.status === 'pending').length;
    const running = state.jobs.items.filter(j => j.status === 'running').length;
    const failed  = state.jobs.items.filter(j => j.status === 'failed').length;

    setTextSafe('jobs-count-pending', String(pending));
    setTextSafe('jobs-count-running', String(running));
    setTextSafe('jobs-count-failed',  String(failed));

    updateConnectionStatus(true);
  } catch (err) {
    state.jobs.error = err;
    tbody.innerHTML = '';
    if (errDiv) {
      errDiv.classList.remove('hidden');
      errDiv.querySelector('.err-msg').textContent = err.message;
      errDiv.querySelector('.err-detail').textContent =
        err.detail ? JSON.stringify(err.detail) : '';
    }
    updateConnectionStatus(false);
  } finally {
    state.jobs.loading = false;
  }
}

/** Safe textContent setter. */
function setTextSafe(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

/* ============================================================
   11. Auto-Refresh Manager
   ============================================================ */

/** Start the always-on 10 s jobs auto-refresh. */
function startJobsAutoRefresh() {
  if (state.refresh.jobsTimer) return;
  state.refresh.jobsTimer = setInterval(() => {
    if (state.activeTab === 'jobs') loadJobs();
  }, 10_000);
}

/* ============================================================
   12. Shared UI Helpers
   ============================================================ */

/** Update the connection status indicator in the topbar. */
function updateConnectionStatus(online) {
  const el = document.getElementById('connection-status');
  if (!el) return;
  el.className = online ? 'online' : 'error';
  const span = el.querySelector('span');
  if (span) span.textContent = online ? 'Connected' : 'Offline';
}

/**
 * Build a reusable error banner HTML string.
 * @returns {string}
 */
function errorBannerHtml(idPrefix) {
  return `
    <div id="${idPrefix}-error" class="error-banner hidden" role="alert">
      <span class="error-icon">⚠</span>
      <div>
        <strong>Failed to load data</strong>
        <div class="err-msg"></div>
        <div class="err-detail error-detail"></div>
      </div>
    </div>
  `;
}

/* ============================================================
   13. Bootstrap / Init
   ============================================================ */

/** Attach all event listeners and kick off first data load. */
function init() {
  // Tab navigation
  document.querySelectorAll('#tab-nav button').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  // Log filters
  initLogFilters();

  // Deals filters
  initDealsFilters();

  // Collections sorting + export
  initCollectionsSorting();
  const exportBtn = document.getElementById('export-collections-csv');
  if (exportBtn) exportBtn.addEventListener('click', exportCollectionsCSV);

  // Sync All button
  const syncAllBtn = document.getElementById('btn-sync-all');
  if (syncAllBtn) {
    syncAllBtn.addEventListener('click', async () => {
      syncAllBtn.disabled = true;
      syncAllBtn.textContent = '…';
      try {
        await triggerSyncAll();
        showToast('Global sync triggered — jobs queued', 'success');
        setTimeout(loadJobs, 1500);
      } catch (err) {
        showToast(`Sync All failed: ${err.message}`, 'error');
      } finally {
        syncAllBtn.disabled    = false;
        syncAllBtn.textContent = '⚡ Trigger Sync All';
      }
    });
  }

  // Jobs refresh button
  const jobsRefreshBtn = document.getElementById('btn-refresh-jobs');
  if (jobsRefreshBtn) jobsRefreshBtn.addEventListener('click', loadJobs);

  // Collections refresh
  const colRefreshBtn = document.getElementById('btn-refresh-collections');
  if (colRefreshBtn) colRefreshBtn.addEventListener('click', loadCollections);

  // Start always-on jobs auto-refresh
  startJobsAutoRefresh();

  // Load initial tab data
  loadLogs();

  // Update connection status initially
  updateConnectionStatus(false);
}

// Run after DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
