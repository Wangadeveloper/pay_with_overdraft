/**
 * activity-grid.js
 * AG Grid Enterprise — PayFlow Financial Activity Table
 *
 * License key is set once at the top; all features used are Community-tier.
 * The Enterprise key simply removes the console watermark.
 */

/* ── License ──────────────────────────────────────────────────────────────── */
// Applied before any grid is created
const AG_LICENSE_KEY =
  'AG-148865_is_granted_for_evaluation_only___Use_in_production_is_not_permitted___Please_report_misuse_to_legal@ag-grid.com___For_help_with_purchasing_a_production_key_please_contact_info@ag-grid.com___You_are_granted_a_Single_Application_Developer_License_for_one_application_only___All_Front-End_JavaScript_developers_working_on_the_application_would_need_to_be_licensed___This_key_will_deactivate_on_3 December 2026____[v3]_[03C]_MTc5NjI1NjAwMDAwMA==ec3029f68ad5970284bf4b41d86a586e';

/* ── Helpers ──────────────────────────────────────────────────────────────── */

/**
 * Format an ISO date string to a readable local format.
 * @param {string} iso
 * @returns {string}
 */
function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleDateString('en-US', {
    month: 'short', day: 'numeric', year: 'numeric',
  }) + ' ' + d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });
}

/**
 * Format a number as USD currency.
 * @param {number} val
 * @returns {string}
 */
function fmtUSD(val) {
  if (val == null || val === 0) return '—';
  return '$' + Number(val).toFixed(2);
}

/* ── Cell Renderers ───────────────────────────────────────────────────────── */

/** Activity type badge */
function activityRenderer(params) {
  const map = {
    overdraft: { icon: '⬇️', label: 'Overdraft',  cls: 'badge-danger'   },
    repayment: { icon: '✅', label: 'Repayment',  cls: 'badge-success'  },
    incoming:  { icon: '⬆️', label: 'Incoming',   cls: 'badge-success'  },
    payment:   { icon: '💸', label: 'Payment',    cls: 'badge-warning'  },
    checkout:  { icon: '🛒', label: 'Checkout',   cls: 'badge-info'     },
  };
  const cfg = map[params.value] || { icon: '📄', label: params.value, cls: '' };
  return `<span class="ag-badge ${cfg.cls}">${cfg.icon} ${cfg.label}</span>`;
}

/** Status badge */
function statusRenderer(params) {
  const map = {
    ACTIVE:    { cls: 'badge-warning', dot: true  },
    REPAID:    { cls: 'badge-success', dot: false },
    COMPLETED: { cls: 'badge-success', dot: false },
    PENDING:   { cls: 'badge-info',    dot: true  },
    FAILED:    { cls: 'badge-danger',  dot: false },
  };
  const cfg = map[params.value] || { cls: '', dot: false };
  const dot = cfg.dot ? '<span class="badge-dot"></span>' : '';
  return `<span class="badge ${cfg.cls}">${dot}${params.value}</span>`;
}

/** Amount coloured by direction */
function amountRenderer(params) {
  const row = params.data;
  if (!row) return '—';
  const isIncoming = row.type === 'incoming';
  const colour = isIncoming ? 'var(--success)' : 'var(--text-primary)';
  const prefix = isIncoming ? '+' : '';
  return `<span style="font-family:'Outfit',sans-serif;font-weight:700;color:${colour}">${prefix}${fmtUSD(params.value)}</span>`;
}

/** Credit-used coloured amber when non-zero */
function creditRenderer(params) {
  if (!params.value || params.value === 0) return '<span style="color:var(--text-muted)">—</span>';
  return `<span style="font-weight:600;color:var(--warning)">${fmtUSD(params.value)}</span>`;
}

/** Fee in muted style */
function feeRenderer(params) {
  if (!params.value || params.value === 0) return '<span style="color:var(--text-muted)">—</span>';
  return `<span style="color:var(--text-secondary)">${fmtUSD(params.value)}</span>`;
}

/** Details expand button */
function detailsRenderer(params) {
  return `<button
    class="ag-detail-btn"
    onclick="showDetail(event, ${JSON.stringify(params.data).replace(/"/g, '&quot;')})"
    title="View full details"
  >Details ›</button>`;
}

/* ── Detail Panel ─────────────────────────────────────────────────────────── */

/**
 * Populate and reveal the detail panel below the grid.
 * @param {MouseEvent} e
 * @param {Object} row - the full row data object
 */
function showDetail(e, row) {
  e.stopPropagation();
  const panel = document.getElementById('activity-detail');
  if (!panel) return;

  const d = row.detail || {};
  const typeLabel = {
    overdraft: '⬇️ Overdraft',
    repayment: '✅ Repayment',
    incoming:  '⬆️ Incoming Payment',
    payment:   '💸 Outgoing Payment',
    checkout:  '🛒 Checkout',
  }[row.type] || row.type;

  panel.innerHTML = `
    <div class="detail-panel-inner">
      <div class="detail-panel-header">
        <div>
          <span class="detail-title">${typeLabel}</span>
          <span class="detail-ref">${row.reference}</span>
        </div>
        <button class="detail-close" onclick="closeDetail()">✕</button>
      </div>

      <div class="detail-grid">
        <div class="detail-item">
          <div class="detail-label">Date</div>
          <div class="detail-value">${fmtDate(row.date)}</div>
        </div>
        <div class="detail-item">
          <div class="detail-label">Amount</div>
          <div class="detail-value" style="color:var(--warning);font-weight:700">${fmtUSD(row.amount)}</div>
        </div>
        <div class="detail-item">
          <div class="detail-label">Credit Used</div>
          <div class="detail-value">${row.credit_used ? fmtUSD(row.credit_used) : '—'}</div>
        </div>
        <div class="detail-item">
          <div class="detail-label">Fee</div>
          <div class="detail-value">${row.fee ? fmtUSD(row.fee) : '—'}</div>
        </div>
        ${d.total_owed ? `
        <div class="detail-item">
          <div class="detail-label">Total Owed</div>
          <div class="detail-value" style="color:var(--danger)">${fmtUSD(d.total_owed)}</div>
        </div>` : ''}
        <div class="detail-item">
          <div class="detail-label">Status</div>
          <div class="detail-value">${statusRenderer({ value: row.status, data: row })}</div>
        </div>
        ${d.internal_status ? `
        <div class="detail-item">
          <div class="detail-label">Lifecycle State</div>
          <div class="detail-value"><code style="color:var(--info);font-size:0.78rem">${d.internal_status}</code></div>
        </div>` : ''}
        ${d.due_date ? `
        <div class="detail-item">
          <div class="detail-label">Due Date</div>
          <div class="detail-value ${d.days_overdue > 0 ? 'text-danger' : ''}">${fmtDate(d.due_date)}${d.days_overdue > 0 ? ` <span class="badge badge-danger">${d.days_overdue}d overdue</span>` : ''}</div>
        </div>` : ''}
        ${d.repaid_at ? `
        <div class="detail-item">
          <div class="detail-label">Repaid At</div>
          <div class="detail-value" style="color:var(--success)">${fmtDate(d.repaid_at)}</div>
        </div>` : ''}
        ${d.payout_batch_id && d.payout_batch_id !== '—' ? `
        <div class="detail-item detail-item--full">
          <div class="detail-label">PayPal Payout Batch ID</div>
          <div class="detail-value"><code style="color:var(--info);font-size:0.75rem;word-break:break-all">${d.payout_batch_id}</code></div>
        </div>` : ''}
        ${d.order_id && d.order_id !== '—' ? `
        <div class="detail-item detail-item--full">
          <div class="detail-label">PayPal Order ID</div>
          <div class="detail-value"><code style="color:var(--info);font-size:0.75rem;word-break:break-all">${d.order_id}</code></div>
        </div>` : ''}
        ${d.description && d.description !== '—' ? `
        <div class="detail-item detail-item--full">
          <div class="detail-label">Description</div>
          <div class="detail-value">${d.description}</div>
        </div>` : ''}
      </div>
    </div>
  `;

  panel.classList.add('visible');
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function closeDetail() {
  const panel = document.getElementById('activity-detail');
  if (panel) panel.classList.remove('visible');
}

/* ── Grid Initialisation ──────────────────────────────────────────────────── */

/** Column definitions */
const columnDefs = [
  {
    field: 'date',
    headerName: 'Date',
    sortable: true,
    filter: true,
    minWidth: 160,
    valueFormatter: p => fmtDate(p.value),
    sort: 'desc',
  },
  {
    field: 'type',
    headerName: 'Activity',
    sortable: true,
    filter: true,
    minWidth: 130,
    cellRenderer: activityRenderer,
  },
  {
    field: 'reference',
    headerName: 'Reference',
    sortable: true,
    filter: true,
    minWidth: 160,
    cellStyle: { fontFamily: "'Outfit', monospace", fontSize: '0.82rem', color: 'var(--text-secondary)' },
  },
  {
    field: 'amount',
    headerName: 'Amount',
    sortable: true,
    filter: 'agNumberColumnFilter',
    minWidth: 110,
    cellRenderer: amountRenderer,
  },
  {
    field: 'credit_used',
    headerName: 'Credit Used',
    sortable: true,
    filter: 'agNumberColumnFilter',
    minWidth: 120,
    cellRenderer: creditRenderer,
  },
  {
    field: 'fee',
    headerName: 'Fee',
    sortable: true,
    filter: 'agNumberColumnFilter',
    minWidth: 90,
    cellRenderer: feeRenderer,
  },
  {
    field: 'status',
    headerName: 'Status',
    sortable: true,
    filter: true,
    minWidth: 120,
    cellRenderer: statusRenderer,
  },
  {
    field: 'id',
    headerName: '',
    sortable: false,
    filter: false,
    minWidth: 100,
    maxWidth: 110,
    cellRenderer: detailsRenderer,
    cellStyle: { display: 'flex', alignItems: 'center', justifyContent: 'center' },
  },
];

/** Default column settings */
const defaultColDef = {
  resizable: true,
  suppressMovable: false,
  cellStyle: { display: 'flex', alignItems: 'center' },
};

/* ── Mount ────────────────────────────────────────────────────────────────── */

let gridApi = null;
let allRows  = [];

/**
 * Fetch activity data from the Flask endpoint and load into the grid.
 */
async function loadActivityData() {
  const loadingEl = document.getElementById('activity-grid-loading');
  const emptyEl   = document.getElementById('activity-grid-empty');

  try {
    const res  = await fetch('/api/activity');
    const data = await res.json();

    if (!data.success) throw new Error(data.error || 'API error');

    allRows = data.rows || [];

    if (gridApi) {
      gridApi.setGridOption('rowData', allRows);
      if (allRows.length === 0 && emptyEl) emptyEl.classList.remove('hidden');
    }
  } catch (err) {
    console.error('[PayFlow AG Grid] Failed to load activity:', err);
    if (loadingEl) {
      loadingEl.innerHTML = `<span style="color:var(--danger)">⚠ Failed to load activity data. ${err.message}</span>`;
    }
  } finally {
    if (loadingEl) loadingEl.classList.add('hidden');
  }
}

/**
 * Apply a quick status filter from the tab bar.
 * @param {string} status  - 'ALL' | 'ACTIVE' | 'REPAID' | 'COMPLETED' | 'PENDING' | 'FAILED'
 * @param {HTMLElement} btn - the clicked tab button
 */
function applyStatusFilter(status, btn) {
  // Update active tab styling
  document.querySelectorAll('.ag-filter-tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');

  closeDetail();

  if (!gridApi) return;

  if (status === 'ALL') {
    gridApi.setGridOption('rowData', allRows);
  } else {
    gridApi.setGridOption('rowData', allRows.filter(r => r.status === status));
  }
}

/**
 * Wire the search input to AG Grid's quick-filter.
 */
function setupSearch() {
  const input = document.getElementById('activity-search');
  if (!input || !gridApi) return;
  input.addEventListener('input', () => {
    gridApi.setGridOption('quickFilterText', input.value);
  });
}

/**
 * Initialise the AG Grid instance.
 */
function initActivityGrid() {
  const container = document.getElementById('activity-grid');
  if (!container || typeof agGrid === 'undefined') {
    console.warn('[PayFlow AG Grid] Container or agGrid not found.');
    return;
  }

  // Apply Enterprise license (removes watermark; we use only Community features)
  if (agGrid.LicenseManager) {
    agGrid.LicenseManager.setLicenseKey(AG_LICENSE_KEY);
  }

  const gridOptions = {
    columnDefs,
    defaultColDef,
    rowData: [],
    pagination: true,
    paginationPageSize: 15,
    animateRows: true,
    rowHeight: 52,
    headerHeight: 44,
    suppressCellFocus: true,
    rowSelection: 'single',
    suppressRowClickSelection: false,
    domLayout: 'autoHeight',
    onGridReady(params) {
      gridApi = params.api;
      setupSearch();
      loadActivityData();
      // Auto-size columns after a short delay
      setTimeout(() => {
        try { params.api.sizeColumnsToFit(); } catch (_) {}
      }, 200);
    },
    onGridSizeChanged(params) {
      try { params.api.sizeColumnsToFit(); } catch (_) {}
    },
  };

  agGrid.createGrid(container, gridOptions);
}

/* ── Bootstrap ────────────────────────────────────────────────────────────── */
// Wait for the DOM + AG Grid CDN script to be ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initActivityGrid);
} else {
  initActivityGrid();
}
