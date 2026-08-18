/* PredictionEdge — shared client logic.
   Capital and journal live in localStorage and are never uploaded. board.json is a
   published snapshot, so anything time-based is recomputed at view time rather than
   trusted as generated. */

const KEY = 'pe_journal_v1', CAPKEY = 'pe_capital_v1';
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const money = n => '$' + Number(n || 0).toLocaleString(undefined, { maximumFractionDigits: 0 });
const signed = n => (n >= 0 ? '+' : '−') + '$' +
  Math.abs(Number(n || 0)).toLocaleString(undefined, { maximumFractionDigits: 0 });

let DATA = { tickets: [] };

/* --- icons (SVG, never emoji) ------------------------------------------ */
const ICON = {
  logo: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l6-6 4 4 8-8"/><path d="M17 7h4v4"/></svg>',
  home: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 10l9-7 9 7v10a1 1 0 01-1 1h-5v-6H9v6H4a1 1 0 01-1-1z"/></svg>',
  board: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/></svg>',
  journal: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 016.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15A2.5 2.5 0 016.5 2z"/></svg>',
  method: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.1 9a3 3 0 015.8 1c0 2-3 3-3 3"/><path d="M12 17h.01"/></svg>',
  research: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>',
  refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 11-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>',
  ext: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/><path d="M15 3h6v6M10 14L21 3"/></svg>',
  trial: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3h6M10 3v6.2L5.6 17A2 2 0 007.3 20h9.4a2 2 0 001.7-3L14 9.2V3"/><path d="M7 15h10"/></svg>',
  down: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><path d="M7 10l5 5 5-5M12 15V3"/></svg>',
};

/* The site is a case study with an appendix, not a dashboard with tabs. `index.html`
   carries the method that used to live on its own page; the journal was a private
   localStorage scratchpad and has no place in a public write-up, so both are gone. */
const PAGES = [
  ['index.html', 'Case study', 'home'],
  ['trial.html', 'The record', 'trial'],
  ['board.html', 'Board', 'board'],
  ['research.html', 'Research', 'research'],
];

function mountNav(current) {
  const tabs = PAGES.map(([href, label, ic]) =>
    `<a href="${href}"${href === current ? ' aria-current="page"' : ''}>${ICON[ic]}${label}</a>`
  ).join('');
  document.body.insertAdjacentHTML('afterbegin', `<header class="topbar"><div class="inner">
      <a class="logo" href="index.html">${ICON.logo}Prediction<span>Edge</span></a>
      <nav class="tabs" aria-label="Sections">${tabs}</nav>
      <div class="grow"></div>
      <span class="muted" id="updated"></span>
      <button onclick="load()" aria-label="Refresh data">${ICON.refresh}Refresh</button>
    </div></header>`);
}

/* --- storage ------------------------------------------------------------ */
const jread = () => { try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) { return {}; } };
const jwrite = o => localStorage.setItem(KEY, JSON.stringify(o));
const defaultCap = () => Number(DATA.account_size) || 100000;
function capital() {
  const v = parseFloat(localStorage.getItem(CAPKEY));
  return (isFinite(v) && v > 0) ? v : defaultCap();
}
function setCapital(v) {
  const n = parseFloat(v);
  if (isFinite(n) && n > 0) localStorage.setItem(CAPKEY, String(n));
  if (typeof render === 'function') render(true);
}
function resetCapital() { localStorage.removeItem(CAPKEY); if (typeof render === 'function') render(); }

/* --- time --------------------------------------------------------------- */
function fmtDur(h) {
  if (h == null || isNaN(h)) return '—';
  if (h < 1) return Math.max(0, Math.round(h * 60)) + 'm';
  if (h < 48) return h.toFixed(0) + 'h';
  return (h / 24).toFixed(1) + 'd';
}
/* Polymarket rows write "2026-08-11 23:15:00+00" - a space separator and a two-digit
   offset. V8 accepts both; the spec requires neither, so other engines can return NaN
   and every countdown silently reads "—". Kalshi rows are already proper ISO, where
   both replacements are no-ops. */
const parseIso = s => {
  if (!s) return null;
  const ms = Date.parse(String(s).replace(' ', 'T').replace(/([+-]\d{2})$/, '$1:00'));
  return isNaN(ms) ? null : ms;
};
/* event_iso (kickoff) FIRST. end_iso is a settlement deadline and Polymarket pads it
   to a tournament-wide date, so recomputing from it printed "7d" on a game being
   played tonight. Fall back to the deadline only where there is no kickoff. */
const hoursLeft = t => {
  for (const iso of [t.event_iso, t.end_iso]) {
    const ms = parseIso(iso);
    if (ms != null) return (ms - Date.now()) / 3600000;
  }
  return null;
};
const ageMin = t => t.signal_ts ? (Date.now() / 1000 - t.signal_ts) / 60 : null;

/* --- sizing (mirrors the backend sizing rule; constants come from board.json's
   "sizing" key - "omen" is the pre-rename key, read so a cached board keeps sizing) - */
function sizeFor(price, account, conviction) {
  const o = DATA.sizing || DATA.omen || { min_contract_price: 0.01, contracts_per_market_per_1k: 19, per_trade_fraction: 0.01 };
  if (!(price >= o.min_contract_price && price < 1) || !(account > 0)) return null;
  const c = Math.max(0, Math.min(1, conviction || 0));
  if (c <= 0) return null;
  const want = Math.floor(account * o.per_trade_fraction * c / price);
  if (want < 1) return null;
  const cap = Math.floor(account / 1000 * o.contracts_per_market_per_1k);
  const contracts = Math.min(want, cap);
  if (contracts < 1) return null;
  return { contracts, cost: contracts * price, max_payout: contracts,
           capped: want > cap ? 'per-market-limit' : 'risk-budget' };
}

/* --- money derived from logged outcomes --------------------------------- */
function ledger() {
  const rows = Object.values(jread());
  const won = rows.filter(r => r.status === 'won'), lost = rows.filter(r => r.status === 'lost');
  const open = rows.filter(r => r.status === 'taken');
  const settledCost = [...won, ...lost].reduce((a, r) => a + (+r.cost || 0), 0);
  const returned = won.reduce((a, r) => a + (+r.contracts || 0), 0);
  const realized = returned - settledCost;
  const openExposure = open.reduce((a, r) => a + (+r.cost || 0), 0);
  const start = capital(), settled = won.length + lost.length;
  const dayAgo = Date.now() / 1000 - 86400;
  const today = [...won, ...lost].filter(r => (+r.ts || 0) >= dayAgo);
  const todayPnl = today.filter(r => r.status === 'won').reduce((a, r) => a + (+r.contracts || 0), 0)
                 - today.reduce((a, r) => a + (+r.cost || 0), 0);
  return { start, realized, openExposure, account: start + realized,
           power: start + realized - openExposure,
           taken: rows.filter(r => ['taken', 'won', 'lost'].includes(r.status)).length,
           openCount: open.length, won: won.length, lost: lost.length,
           skipped: rows.filter(r => r.status === 'skipped').length,
           hit: settled ? won.length / settled : null, todayPnl, settled };
}

function mark(id, status) {
  const j = jread();
  if (!status) { delete j[id]; }
  else {
    const t = (DATA.tickets || []).find(x => x.market_id + ':' + x.outcome === id) || {};
    const prior = j[id] || {};
    // Freeze size when placed: later capital edits must not rewrite a past trade.
    const z = (prior.contracts != null) ? null : sizeFor(t.entry_price, ledger().account, t.conviction);
    j[id] = Object.assign({}, prior, { status, ts: Date.now() / 1000,
      title: t.title, side_label: t.side_label, entry_price: t.entry_price },
      z ? { contracts: z.contracts, cost: z.cost, conviction: t.conviction } : {});
  }
  jwrite(j);
  if (typeof render === 'function') render();
}

function exportJournal() {
  const payload = { capital: capital(), ledger: ledger(), journal: jread() };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'predictionedge-journal.json';
  a.click();
}

/* --- board data --------------------------------------------------------- */
async function load() {
  try {
    const r = await fetch('board.json?t=' + Date.now());
    if (!r.ok) throw new Error('board.json ' + r.status);
    DATA = await r.json();
  } catch (e) {
    const u = $('updated'); if (u) u.textContent = 'error: ' + e.message;
    if (typeof onLoadError === 'function') onLoadError(e);
    return;
  }
  if (typeof render === 'function') render();
}

function dataAgeMin() {
  return DATA.generated_at ? (Date.now() - DATA.generated_at * 1000) / 60000 : null;
}

function stampUpdated() {
  const a = dataAgeMin(), u = $('updated');
  if (u) u.textContent = a == null ? '' : 'data ' + fmtDur(a / 60) + ' old';
}

/* Tickets whose market has moved inside the min-hours window since generation. */
function splitTickets() {
  const minH = (DATA.filters && DATA.filters.min_hours) || 0;
  const live = [], expired = [];
  (DATA.tickets || []).forEach(t => {
    const h = hoursLeft(t);
    (h != null && h < minH ? expired : live).push(t);
  });
  return { live, expired };
}
