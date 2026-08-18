/* PredictionEdge — shared client logic.
   board.json is a published snapshot, so anything time-based is recomputed at view time
   rather than trusted as generated. */

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
  research: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>',
  refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 11-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>',
  ext: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/><path d="M15 3h6v6M10 14L21 3"/></svg>',
  trial: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3h6M10 3v6.2L5.6 17A2 2 0 007.3 20h9.4a2 2 0 001.7-3L14 9.2V3"/><path d="M7 15h10"/></svg>',
};

/* The site is a case study with an appendix, not a dashboard with tabs. The method that
   used to have its own page is in `index.html`; the journal and the live board were
   operator tools - a private localStorage scratchpad and a what-to-buy-now screen - and
   neither belongs in a write-up about what already happened. The sizing rule, the
   capital box and the outcome logger went with the board, which was their only caller. */
const PAGES = [
  ['index.html', 'Case study', 'home'],
  ['trial.html', 'The record', 'trial'],
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
