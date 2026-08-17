"""Kalshi AAA national gas price markets, priced by persistence plus drift.

This is the weather sleeve's architecture pointed at a different settlement source,
and the resemblance is deliberate: Kalshi's KXAAAGASD (daily) settles to a single
free, public number - AAA's national average at gasprices.aaa.com - exactly as the
temperature ladders settle to one thermometer's CLI report. The mechanism differs in
one way that favours this market: the AAA number is a smoothed lagging average of
OPIS station prices, so day-over-day moves are tiny, strongly autocorrelated, and
trend with wholesale RBOB pass-through over one to three weeks. A persistence
forecast (today's print plus an EWMA drift) with a sigma measured from the print's
OWN observed daily deltas maps directly onto the half-cent bracket ladder.

WHAT "THE PRICE FOR DATE X" MEANS, because settlement timing is the trap here.
gasprices.aaa.com shows one national average per calendar day, stamped "Price as of
M/D/YY", refreshed in the early morning ET. The Kalshi daily market dated X settles
to AAA's published value FOR the settlement date X - the number the page displays
while it says "Price as of X" - even though Kalshi's determination may only happen
the NEXT morning once that print is up (the contract terms PDF ties settlement to
AAA's published value for the settlement date). Every ticket therefore records
`aaa_target_date`: the calendar day whose AAA print the model is forecasting. The
observation log (`gaslog.py`) keys its truth the same way, off the page's own
"Price as of" stamp and never off our clock.

*** NOTHING HERE IS VALIDATED. *** Tickets are paper-only by construction: they
carry `venue="kalshi"` into the existing paper trial and no order path reads them.

PRICING COPIES THE WEATHER SLEEVE'S HARD-WON DESIGN, not its code style but its
lesson: pricing brackets directly off a normal centred on the forecast compares two
different SHAPES as well as two different means, and even fed the market's own mean
it printed cents of fake "edge" on the tails. So the market's quoted mids are the
prior and the forecast only TILTS them - each bracket's mid is multiplied by the
likelihood ratio of that bracket under the forecast versus under the market-implied
mean, at a shared sigma, then rescaled back to the mass the market itself quotes.
When the forecast equals the market's implied mean every ratio is exactly 1 and no
bracket can gain, so agreement CANNOT emit a ticket. That is a structural guarantee,
asserted by test, not a threshold that happens to hold.

WHERE SIGMA COMES FROM, and why it differs from weather. The temperature sleeve
takes its width from the ladder because its forecast error is genuinely hard to
know a priori. Here the settlement series itself is observable every day for free,
so sigma is the standard deviation of the print's own recent daily deltas, measured
by `gaslog.py` - with a FLOOR (default 0.15c) so a few days of quiet history cannot
manufacture absurd confidence, and a minimum history requirement below which the
sleeve stays silent rather than guessing. No observed history, no opinion.

UNLIKE TEMPERATURE, NO DISCRETISATION CORRECTION. The CLI report states whole
degrees so weather's brackets each gain half a degree per side; the AAA average is
published to a tenth of a cent (e.g. $4.0715), which against a sigma of tenths of
cents makes the continuous CDF the honest reading. The ~0.1c gaps between adjacent
Kalshi strike bounds hold negligible mass, and the likelihood-ratio rescaling to
the market's own total absorbs what little there is.

TODO (deliberately out of scope for v1, measure persistence first):
  - RBOB wholesale tilt: lead the drift with futures pass-through at a 1-3 week lag.
  - KXAAAGASW weekly series: same design keyed to the week-end print.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timedelta, timezone

from .fees import fee_per_contract
# The one shared Kalshi ticker parser. This module used to carry its own copy, which
# was correct only because the AAA ladders happen to hang no suffix off the day.
from .kalshi import event_day as _event_day

log = logging.getLogger(__name__)

AAA_URL = "https://gasprices.aaa.com/"
USER_AGENT = "PredictionEdge/1.0 (github.com/bpmcginley/PredictionEdge)"

SERIES_DAILY = "KXAAAGASD"
# Weekly series exists (KXAAAGASW) - TODO once the daily sleeve has a measured record.

# Defaults; `Config` carries the tunable copies. Dollars throughout.
MIN_EDGE = 0.05
MAX_DAYS = 2.0
SIGMA_FLOOR = 0.0015          # 0.15c: a few quiet days must not imply certainty
MIN_DELTAS = 3                # observed day-over-day moves required before any opinion

# --- scraping the AAA page ---------------------------------------------------------
#
# Shape confirmed live 2026-08-13: a `table.table-mob` whose rows are labelled
# "Current Avg.", "Yesterday Avg.", "Week Ago Avg.", "Month Ago Avg.", "Year Ago
# Avg.", each label followed by the Regular-grade price first (`<td>$4.0715</td>`),
# and a "Price as of 8/13/26" stamp naming the day the current print belongs to.
# The regexes anchor on the label and take the FIRST dollar figure after it, which
# is the Regular column - the one the Kalshi contract settles on.

_PRICE = r"\$\s*(\d+\.\d{2,4})"
_ROW_RES = {
    "current": re.compile(r"Current\s+Avg[^<]*</td>\s*<td[^>]*>\s*" + _PRICE, re.I),
    "yesterday": re.compile(r"Yesterday\s+Avg[^<]*</td>\s*<td[^>]*>\s*" + _PRICE, re.I),
    "week_ago": re.compile(r"Week\s+Ago\s+Avg[^<]*</td>\s*<td[^>]*>\s*" + _PRICE, re.I),
    "month_ago": re.compile(r"Month\s+Ago\s+Avg[^<]*</td>\s*<td[^>]*>\s*" + _PRICE, re.I),
}
_AS_OF_RE = re.compile(
    r"Price\s+as\s+of\s*(?:<br\s*/?>\s*)?(\d{1,2})/(\d{1,2})/(\d{2,4})", re.I)

# A national average outside this band is a mis-parse (an ad price, a map legend),
# not a gas price. Rejecting it beats logging it as truth.
_SANE_LO, _SANE_HI = 1.0, 10.0


def _default_fetch(url: str) -> str:
    import requests
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    r.raise_for_status()
    return r.text


def parse_national(html: str) -> dict | None:
    """The national-average table as floats, or None if the page shape changed.

    `current` is required; the trailing averages are recorded when present because
    `yesterday` lets the log backfill one missing day after an outage. `as_of` is
    the PAGE'S OWN date for the current print - observations are keyed on it, never
    on our clock, so a run straddling AAA's early-morning refresh cannot file
    today's number under the wrong day.
    """
    out: dict = {}
    for key, rx in _ROW_RES.items():
        m = rx.search(html)
        if m:
            v = float(m.group(1))
            if _SANE_LO <= v <= _SANE_HI:
                out[key] = v
    if "current" not in out:
        return None
    m = _AS_OF_RE.search(html)
    if m:
        mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yy < 100:
            yy += 2000
        try:
            out["as_of"] = f"{yy:04d}-{mm:02d}-{dd:02d}"
        except ValueError:
            pass
    return out


def fetch_national(*, fetch=None) -> dict | None:
    """Scrape gasprices.aaa.com. Any failure is None - no data means no tickets,
    never a crash and never a guess."""
    getter = fetch or _default_fetch
    try:
        html = getter(AAA_URL)
    except Exception:  # noqa: BLE001
        log.warning("AAA national average unavailable", exc_info=True)
        return None
    snap = parse_national(html or "")
    if snap is None:
        log.warning("AAA page fetched but did not parse - shape changed?")
    return snap


# --- distribution over the ladder --------------------------------------------------

def _cdf(x: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


def bracket_probability(floor_strike: float | None, cap_strike: float | None,
                        mu: float, sigma: float) -> float | None:
    """P(the AAA print lands in this bracket), or None if the bracket is unreadable.

    Continuous CDF, no half-unit inflation - see the module docstring for why this
    is the opposite of the temperature sleeve's most important line of code.
    """
    if floor_strike is None and cap_strike is None:
        return None
    if floor_strike is not None and cap_strike is not None:
        lo, hi = float(floor_strike), float(cap_strike)
        if hi <= lo:
            return None
        return max(0.0, _cdf(hi, mu, sigma) - _cdf(lo, mu, sigma))
    if cap_strike is not None:
        return max(0.0, _cdf(float(cap_strike), mu, sigma))
    return max(0.0, 1.0 - _cdf(float(floor_strike), mu, sigma))


def _interior_width(ladder: list[dict]) -> float:
    widths = sorted(float(m["cap_strike"]) - float(m["floor_strike"])
                    for m in ladder
                    if m.get("floor_strike") is not None
                    and m.get("cap_strike") is not None)
    return widths[len(widths) // 2] if widths else 0.005


def _bracket_centre(floor_strike, cap_strike, half_width: float) -> float | None:
    """A representative price for a bracket, for reading the market's own view.

    Open-ended tails get half an interior bracket past the edge - a deliberate
    understatement of how far a tail can run, which biases the implied sigma
    CONSERVATIVE (smaller), and the implied mean is all this number is used for.
    """
    if floor_strike is not None and cap_strike is not None:
        return (float(floor_strike) + float(cap_strike)) / 2.0
    if cap_strike is not None:
        return float(cap_strike) - half_width
    if floor_strike is not None:
        return float(floor_strike) + half_width
    return None


def market_distribution(ladder: list[dict]) -> tuple[float, float] | None:
    """Mean and standard deviation implied by the ladder's own mids, or None.

    Same lesson as `weather.market_distribution` and `odds.require_complete_market`:
    a ladder that does not form a usable partition (missing legs, stale book, mids
    summing far from 1) is not a distribution and reading it as one invents edge.
    """
    half = _interior_width(ladder) / 2.0
    pts: list[tuple[float, float]] = []
    for m in ladder:
        price = m.get("mid")
        centre = _bracket_centre(m.get("floor_strike"), m.get("cap_strike"), half)
        if price is None or centre is None:
            continue
        pts.append((centre, float(price)))
    total = sum(p for _, p in pts)
    if len(pts) < 3 or not 0.90 <= total <= 1.10:
        return None
    mean = sum(c * p for c, p in pts) / total
    var = sum(p * (c - mean) ** 2 for c, p in pts) / total
    return mean, math.sqrt(var)


# A ratio between two normal tail probabilities can run to millions when the forecast
# sits far outside the ladder. Capping it bounds how much one leg can dominate the
# renormalisation; it never binds on a forecast the market would recognise.
_MAX_TILT = 1000.0


def price_ladder(ladder: list[dict], forecast: float, sigma: float, *,
                 min_edge: float = MIN_EDGE, fee_multiplier: float = 0.07,
                 min_price: float = 0.05, max_price: float = 0.95) -> list[dict]:
    """Score one day's brackets against the forecast. Returns candidate tickets.

    THE MARKET'S PRICES ARE THE PRIOR; the forecast only TILTS them (module
    docstring). The bar a bracket must clear is `taker fee + min_edge`, because a
    gas entry takes the ask the moment the model fires - claimed edge that does not
    survive the fee is not edge. Brackets with no book (either side missing) are
    skipped: an absent quote is not a free trade, it is no information.
    """
    market = market_distribution(ladder)
    if market is None:
        return []                     # incomplete or stale ladder: no opinion, no ticket
    market_mu, market_sigma = market

    tilted: list[tuple[dict, float, float]] = []
    for m in ladder:
        mid, strikes = m.get("mid"), (m.get("floor_strike"), m.get("cap_strike"))
        if mid is None:
            continue                  # no book on this bracket: skip, never impute
        under_forecast = bracket_probability(*strikes, forecast, sigma)
        under_market = bracket_probability(*strikes, market_mu, sigma)
        if under_forecast is None or under_market is None:
            continue
        ratio = min(under_forecast / under_market, _MAX_TILT) \
            if under_market > 0 else (_MAX_TILT if under_forecast > 0 else 0.0)
        tilted.append((m, float(mid) * ratio, ratio))

    quoted = sum(float(m["mid"]) for m, _, _ in tilted)
    shifted = sum(p for _, p, _ in tilted)
    if shifted <= 0 or quoted <= 0:
        return []                     # the forecast is off every bracket: unpriceable
    scale = quoted / shifted          # redistribute mass, never create it

    out: list[dict] = []
    for m, raw, ratio in tilted:
        ask = m.get("ask")
        if ask is None or not min_price <= ask <= max_price:
            continue
        p = raw * scale
        edge = p - ask
        if edge < fee_per_contract(ask, multiplier=fee_multiplier) + min_edge:
            continue
        out.append({
            "market_id": m["ticker"],
            "venue": "kalshi",
            "source": "gas",
            "outcome": "Yes",
            "title": m.get("title", ""),
            "url": m.get("url", ""),
            "entry_price": round(ask, 4),
            "model_prob": round(p, 4),
            "edge": round(edge, 4),
            # Everything below exists so the model can be re-fit later against what
            # actually printed, and so a reader can tell WHY a ticket appeared.
            "forecast": round(forecast, 4),
            "sigma": round(sigma, 5),
            "lr_tilt": round(ratio, 4),
            "market_mu": round(market_mu, 4),
            "market_sigma": round(market_sigma, 5),
            "market_prob": round(float(m["mid"]), 4),
            "model": "kalshi-ladder-tilted-by-aaa-persistence",
            "validated": False,
        })
    return out


# --- wiring: AAA print + observed history + Kalshi ladders -> tickets ---------------

def _mid(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2.0


def find_gas_edges(*, gaslog: dict, kalshi_fetch=None, aaa_fetch=None,
                   series: str = SERIES_DAILY, max_days: float = MAX_DAYS,
                   min_edge: float = MIN_EDGE, fee_multiplier: float = 0.07,
                   ewma_window: int = 5, sigma_window: int = 10,
                   sigma_floor: float = SIGMA_FLOOR, min_deltas: int = MIN_DELTAS,
                   now: datetime | None = None) -> list[dict]:
    """Every bracket whose tilted fair beats its ask by fee + `min_edge`.

    Paper-only by construction: tickets are stamped `venue="kalshi"` for the trial
    and nothing in the order path consumes this function.

    `gaslog` is the persisted observation log from `gaslog.py`, mutated in place
    (the caller owns saving it): today's AAA print is recorded first, then drift and
    sigma are computed from the log's OBSERVED history, and every forecast issued is
    written back so the model's error becomes a measured number. Fail-closed at
    every step: no AAA print, or fewer than `min_deltas` observed daily moves, means
    no tickets - silence, not a default opinion.
    """
    from . import gaslog as gl
    from .kalshi import KALSHI_API, _default_fetch as kx_fetch, _price_to_dollars

    now = now or datetime.now(timezone.utc)
    snap = fetch_national(fetch=aaa_fetch)
    if snap is None:
        return []
    today = snap.get("as_of") or now.strftime("%Y-%m-%d")
    gl.record_snapshot(gaslog, snap, fallback_day=today)
    gl.resolve(gaslog)

    ds = gl.drift_sigma(gaslog, ewma_window=ewma_window, sigma_window=sigma_window,
                        sigma_floor=sigma_floor, min_deltas=min_deltas)
    if ds is None:
        log.info("gas sleeve silent: %d observed delta(s), need %d",
                 len(gl.consecutive_deltas(gaslog)), min_deltas)
        return []
    drift, sigma = ds
    aaa_today = float(snap["current"])
    today_dt = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    getter = kalshi_fetch or kx_fetch
    try:
        # No `status` filter, for the reason `kalshi.market_meta` documents at
        # length: pinning it hides markets and the hiding is silent.
        data = getter(f"{KALSHI_API}/markets",
                      {"series_ticker": series, "limit": 200})
    except Exception:  # noqa: BLE001
        log.warning("kalshi ladder unavailable for %s - skipping", series)
        return []

    by_event: dict[str, list[dict]] = {}
    for m in data.get("markets") or []:
        if m.get("status") not in ("active", "open"):
            continue
        bid, ask = _price_to_dollars(m, "yes_bid"), _price_to_dollars(m, "yes_ask")
        by_event.setdefault(m.get("event_ticker", ""), []).append({
            "ticker": m.get("ticker", ""),
            "title": m.get("title", ""),
            "url": f"https://kalshi.com/markets/{m.get('event_ticker', '')}",
            "floor_strike": m.get("floor_strike"),
            "cap_strike": m.get("cap_strike"),
            "bid": bid, "ask": ask, "mid": _mid(bid, ask),
            # Prefer expected_expiration_time: `expiration_time` is the padded
            # LATEST settlement deadline, not when the outcome is determined.
            "close_time": (m.get("expected_expiration_time")
                           or m.get("close_time") or ""),
        })

    tickets: list[dict] = []
    for event, ladder in by_event.items():
        day = _event_day(event)
        if not day:
            continue
        # Steps from today's print to the print being forecast, in whole AAA days -
        # the settlement series ticks once per calendar day, so the model walks in
        # the same unit. Past days (already printed) are not forecastable.
        steps = (datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                 - today_dt).days
        if steps < 0:
            continue
        days_ahead = (datetime.strptime(day, "%Y-%m-%d").replace(
            tzinfo=timezone.utc) - now).total_seconds() / 86400.0
        if days_ahead > max_days:
            continue
        forecast = aaa_today + drift * steps
        if steps >= 1:
            # steps == 0 is priced off the already-published print, which is a real
            # (and the easiest) trade, but logging it as a "forecast" would salt the
            # calibration sample with days whose answer was known when it was made.
            gl.record_forecast(gaslog, day, forecast=forecast, aaa_today=aaa_today,
                               drift=drift, sigma=sigma,
                               made_at=now.timestamp())
        for t in price_ladder(ladder, forecast, sigma, min_edge=min_edge,
                              fee_multiplier=fee_multiplier):
            t["aaa_today"] = round(aaa_today, 4)
            t["drift"] = round(drift, 5)
            t["days_ahead"] = round(max(0.0, days_ahead), 2)
            # The calendar day whose AAA print settles this market - the market
            # itself may only be DETERMINED the next morning, once that print is
            # up. See the module docstring's date convention.
            t["aaa_target_date"] = day
            t["settles_by"] = AAA_URL
            t["end_iso"] = (datetime.strptime(day, "%Y-%m-%d").replace(
                tzinfo=timezone.utc) + timedelta(days=1)).isoformat()
            t["event_iso"] = t["end_iso"]
            tickets.append(t)

    tickets.sort(key=lambda t: t["edge"], reverse=True)
    return tickets
