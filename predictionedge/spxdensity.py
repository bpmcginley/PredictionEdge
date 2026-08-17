"""Kalshi's daily S&P/Nasdaq range ladders priced off the 0DTE option chain.

Kalshi's KXINX (S&P 500) and KXNASDAQ100 range brackets settle to the official 4pm
index close. CBOE publishes, free and unauthenticated, a 15-minute-delayed quote file
for the whole SPX/NDX option chain - and the same-day-expiry SPXW/NDXP dailies span
exactly that 4pm close. Breeden-Litzenberger says the risk-neutral density of the
close is the second derivative of the call price in strike, so the chain IS a
probability distribution over the number the Kalshi ladder pays on.

This module does not difference raw quotes twice - that amplifies noise quadratically.
It reuses `deribit.py`'s machinery instead, which already learned this lesson on BTC:
fit an implied-vol smile through OTM mids (Black-76 on the forward), interpolate total
variance linearly in log-moneyness, and read P(S > K) analytically off the fitted
smile as N(d2) - vega * dsigma/dK. A bracket is then the difference of two digitals:

    P(K1 < S <= K2) = digital(K1) - digital(K2)

TWO HONESTY CORRECTIONS, both non-negotiable and both recorded on every ticket:

1. RE-CENTERING. The chain is 15 minutes delayed. The density's location is the
   payload's OWN delayed spot (`spot_used` = the same snapshot the quotes came from),
   so the model is internally consistent at one timestamp rather than mixing a fresh
   spot with stale vols. `cfg.spx_spot_source` names where the spot came from so a
   fresher source can be added later without silently changing what old rows meant,
   and `spot_used` / `chain_ts` ride on every ticket. Re-centering is sticky-
   moneyness: the smile is anchored in log-moneyness of the snapshot spot and slides
   with `spot_used`.

2. VRP SHRINK. Risk-neutral tails are fatter than real-world tails - option sellers
   are paid the variance risk premium precisely because implied exceeds realized.
   Reading implied probabilities as real-world ones systematically overprices the
   tails and would make every tail bracket look cheap. Implied sigma is multiplied
   by `cfg.spx_vrp_shrink` (default 0.92) before bracket probabilities are computed,
   and the shrink used is recorded so settled rows can eventually re-fit it.

TIME GATING. Delayed data near the close is toxic: at 3:50pm the chain describes
3:35pm, and fifteen minutes of drift right before settlement is most of a bracket.
No tickets are emitted at or after `cfg.spx_blackout_start_et` (default 15:00 ET).

FEES. Kalshi charges its S&P/Nasdaq markets at multiplier 0.035, not the general
0.07 (`fees.INDEX_FEE_MULTIPLIER`). The edge gate here and the paper-trial fee for
`spxindex` rows both use it; getting this wrong in either direction fabricates or
hides ~1.75c at mid-range prices.

*** NOTHING HERE IS VALIDATED. *** Tickets are paper-only by construction: they carry
venue="kalshi" and source="spxindex" into the existing paper trial and no order path
reads them. Everything fetched is public and unauthenticated.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .config import Config
from .deribit import _IV_CEIL, _IV_FLOOR, Smile, implied_vol
from .fees import INDEX_FEE_MULTIPLIER, fee_per_contract
# One parser for Kalshi's `YYMMMDD` + per-series-suffix event tickers, shared with the
# weather and gas sleeves. It used to be a private copy here that read the whole tail
# as a date, which rejected every real `KXINX-...H1600` and blinded this sleeve.
from .kalshi import (KALSHI_API, _default_fetch as kx_fetch, _price_to_dollars,
                     event_day as _event_day)

log = logging.getLogger(__name__)

_YEAR_SECONDS = 365.25 * 24 * 3600

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"

# Kalshi series -> (CBOE symbol, the option ROOT that carries the daily expiries).
# VERIFIED LIVE 2026-08-13 against cdn.cboe.com (do not trust remembered shapes):
# _SPX.json lists roots SPX (monthlies) and SPXW (dailies, every trading day);
# _NDX.json lists NDX (monthlies) and NDXP (dailies). The monthly roots are
# AM-settled and must never price a 4pm close, hence the explicit root filter.
SERIES: dict[str, tuple[str, str]] = {
    "KXINX": ("_SPX", "SPXW"),
    "KXNASDAQ100": ("_NDX", "NDXP"),
}

MODEL_TAG = "cboe-0dte-density"

# Board price band, same reasoning as weather: the smile is least trustworthy where
# the fitted wings are, and the fee on a 2c contract eats the position.
MIN_PRICE, MAX_PRICE = 0.05, 0.95

# "SPXW260813C06400000" -> root, YYMMDD, C|P, strike*1000.
_OPT = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


# ------------------------------------------------------------------ ET wall clock

def _us_dst(d: date) -> bool:
    """US daylight saving: second Sunday of March through first Sunday of November.

    Day-level granularity on purpose - the 2am transition instant is irrelevant to a
    sleeve that only acts between the open and 4pm - and computed by rule rather than
    zoneinfo because the Windows runner has no tz database installed.
    """
    if not 3 <= d.month <= 11:
        return False
    if 3 < d.month < 11:
        return True
    if d.month == 3:        # second Sunday
        second_sunday = 8 + (6 - date(d.year, 3, 1).weekday()) % 7
        return d.day >= second_sunday
    first_sunday = 1 + (6 - date(d.year, 11, 1).weekday()) % 7
    return d.day < first_sunday


def et_now(now: datetime) -> datetime:
    """The given UTC instant as an ET wall clock (tz-naive, offset already applied)."""
    guess = now.astimezone(timezone.utc).replace(tzinfo=None) - timedelta(hours=5)
    offset = 4 if _us_dst(guess.date()) else 5
    return now.astimezone(timezone.utc).replace(tzinfo=None) - timedelta(hours=offset)


def close_ts_utc(day: date) -> float:
    """Epoch seconds of 4:00pm ET on `day` - the instant every ladder settles on."""
    offset = 4 if _us_dst(day) else 5
    return datetime(day.year, day.month, day.day, 16 + offset,
                    tzinfo=timezone.utc).timestamp()


def _parse_blackout(hhmm: str) -> tuple[int, int]:
    try:
        h, m = hhmm.strip().split(":")
        return int(h), int(m)
    except (ValueError, AttributeError):
        return 15, 0        # a malformed knob fails SAFE: the default wall, not none


def in_blackout(cfg: Config, now: datetime) -> bool:
    et = et_now(now)
    h, m = _parse_blackout(cfg.spx_blackout_start_et)
    return (et.hour, et.minute) >= (h, m)


# ------------------------------------------------------------------------- fetch

@dataclass(frozen=True)
class CboeQuote:
    strike: float
    is_call: bool
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_frac(self) -> float:
        m = self.mid
        return (self.ask - self.bid) / m if m > 0 else float("inf")


@dataclass(frozen=True)
class CboeChain:
    symbol: str          # "_SPX"
    spot: float          # the payload's own delayed index level
    chain_ts: str        # the payload's own timestamp (UTC), verbatim
    quotes: tuple[CboeQuote, ...]


def parse_option_symbol(sym: str) -> tuple[str, str, bool, float] | None:
    """"SPXW260813C06400000" -> ("SPXW", "2026-08-13", True, 6400.0)."""
    m = _OPT.match(sym or "")
    if not m:
        return None
    root, ymd, cp, k = m.groups()
    return root, f"20{ymd[:2]}-{ymd[2:4]}-{ymd[4:6]}", cp == "C", int(k) / 1000.0


def parse_chain(payload: dict, symbol: str, root: str, expiry_ymd: str) -> CboeChain:
    """One expiry's two-sided quotes out of the delayed-quotes payload.

    Drops anything without a genuine bid: a zero-bid deep tail is the absence of a
    price, and fitting a smile through it would let the least reliable quotes set
    the shape of exactly the region the Kalshi tails pay on.
    """
    data = payload.get("data") or {}
    spot = float(data.get("current_price") or 0.0)
    quotes = []
    for o in data.get("options") or []:
        parsed = parse_option_symbol(o.get("option", ""))
        if parsed is None:
            continue
        r, ymd, is_call, strike = parsed
        if r != root or ymd != expiry_ymd:
            continue
        try:
            bid, ask = float(o.get("bid") or 0.0), float(o.get("ask") or 0.0)
        except (TypeError, ValueError):
            continue
        if bid <= 0 or ask <= bid:
            continue        # unquoted, zero-bid or crossed: no price here
        quotes.append(CboeQuote(strike=strike, is_call=is_call, bid=bid, ask=ask))
    return CboeChain(symbol=symbol, spot=spot,
                     chain_ts=str(payload.get("timestamp") or ""),
                     quotes=tuple(sorted(quotes, key=lambda q: q.strike)))


def _default_fetch(url: str, params: dict | None = None) -> dict:
    import requests
    r = requests.get(url, params=params or {}, timeout=25)
    r.raise_for_status()
    return r.json()


# ------------------------------------------------------------------------ smile

def build_smile(chain: CboeChain, *, expiry_ts: float, now_ts: float,
                vrp_shrink: float, min_strikes: int, max_spread_frac: float,
                spot_used: float | None = None) -> Smile | None:
    """The day's risk-neutral smile as a `deribit.Smile`, or None (fail closed).

    Black-76 with forward = the payload's own spot: over the hours to a same-day
    close, index carry is a fraction of a point on thousands, far inside the spread.
    OTM side only, same as deribit - ITM mids are dominated by intrinsic and their
    relative spreads are meaninglessly lenient.

    `spot_used` re-centers: nodes are stored in log-moneyness of the SNAPSHOT spot
    (the vols and the spot come from one timestamp), while `Smile.forward` is the
    location the density is evaluated around. Defaulting them equal is correction #1;
    passing a fresher spot later slides the smile sticky-moneyness style.

    `vrp_shrink` scales sigma before anything is priced (correction #2): total
    variance nodes are (sigma * shrink)^2 * tau.
    """
    tau = (expiry_ts - now_ts) / _YEAR_SECONDS
    if tau <= 0 or chain.spot <= 0:
        return None
    f0 = chain.spot
    by_strike: dict[float, dict[bool, CboeQuote]] = {}
    for q in chain.quotes:
        by_strike.setdefault(q.strike, {})[q.is_call] = q

    nodes: list[tuple[float, float]] = []      # (k, total variance)
    for strike in sorted(by_strike):
        q = by_strike[strike].get(strike >= f0)
        if q is None or q.spread_frac > max_spread_frac:
            continue
        iv = implied_vol(q.mid, f0, strike, tau, q.is_call)
        if iv is None or not (_IV_FLOOR <= iv <= _IV_CEIL):
            continue
        sigma = iv * vrp_shrink
        nodes.append((math.log(strike / f0), sigma * sigma * tau))

    if len(nodes) < min_strikes:
        return None
    return Smile(expiry_ts=expiry_ts, tau=tau,
                 forward=spot_used if spot_used and spot_used > 0 else f0,
                 k_nodes=tuple(k for k, _ in nodes),
                 w_nodes=tuple(w for _, w in nodes),
                 n_listed=len(nodes))


def bracket_prob(smile: Smile, floor_strike: float | None,
                 cap_strike: float | None) -> float | None:
    """P(close lands in this Kalshi bracket) off the fitted smile, or None.

    Every leg must price (the deribit lesson): a "between" bracket whose cap falls
    off the fitted ladder is not half-priceable - returning the floor leg alone
    would silently assert P(above cap) = 0.
    """
    if floor_strike is None and cap_strike is None:
        return None
    lo = smile.digital(float(floor_strike)) if floor_strike is not None else None
    hi = smile.digital(float(cap_strike)) if cap_strike is not None else None
    if floor_strike is not None and lo is None:
        return None
    if cap_strike is not None and hi is None:
        return None
    if floor_strike is None:
        return max(0.0, 1.0 - hi)
    if cap_strike is None:
        return lo
    return max(0.0, lo - hi)


def atm_sigma(smile: Smile) -> float:
    """The (already shrunk) at-the-money implied sigma, for the ticket record."""
    w = smile._w(0.0)
    return math.sqrt(w / smile.tau) if w > 0 and smile.tau > 0 else 0.0


# ----------------------------------------------------------------------- tickets

def _mid(bid: float, ask: float) -> float:
    return (bid + ask) / 2.0 if (bid > 0 and ask > 0) else (bid or ask)


def find_spx_edges(cfg: Config, *, cboe_fetch=None, kalshi_fetch=None,
                   now: datetime | None = None,
                   spot_override: float | None = None) -> list[dict]:
    """Every same-day index bracket whose fair value beats its price by fee + edge.

    Paper-only by construction: tickets are stamped venue="kalshi" and
    source="spxindex" for the trial, and nothing in the order path consumes this.
    Both fetchers are injectable so tests never touch the network.

    `spot_override` re-centers the density on a caller-supplied spot instead of the
    chain's own delayed one - the hook `cfg.spx_spot_source` exists for. Nothing
    passes it today; the default is the honest same-timestamp choice.
    """
    if not cfg.spx_enabled:
        return []
    now = now or datetime.now(timezone.utc)
    if in_blackout(cfg, now):
        log.info("spxindex: inside the pre-close blackout (>= %s ET) - no tickets",
                 cfg.spx_blackout_start_et)
        return []
    et = et_now(now)
    today = et.date()
    expiry_ts = close_ts_utc(today)
    if expiry_ts <= now.timestamp():
        return []                          # after the close: today is decided

    getter_cboe = cboe_fetch or _default_fetch
    getter_kx = kalshi_fetch or kx_fetch
    day_str = today.strftime("%Y-%m-%d")
    tickets: list[dict] = []

    for series, (symbol, root) in SERIES.items():
        try:
            payload = getter_cboe(CBOE_URL.format(symbol=symbol))
        except Exception:  # noqa: BLE001
            log.warning("cboe chain unavailable for %s - skipping", symbol)
            continue
        chain = parse_chain(payload, symbol, root, day_str)
        if not chain.quotes:
            continue                       # weekend / holiday / no dailies listed
        smile = build_smile(chain, expiry_ts=expiry_ts, now_ts=now.timestamp(),
                            vrp_shrink=cfg.spx_vrp_shrink,
                            min_strikes=cfg.spx_min_strikes,
                            max_spread_frac=cfg.spx_max_spread_frac,
                            spot_used=spot_override)
        if smile is None:
            log.info("spxindex: %s smile failed closed (thin or unpriceable)", symbol)
            continue
        sigma = atm_sigma(smile)

        try:
            data = getter_kx(f"{KALSHI_API}/markets",
                             {"series_ticker": series, "limit": 200})
        except Exception:  # noqa: BLE001
            log.warning("kalshi ladder unavailable for %s - skipping", series)
            continue

        for m in data.get("markets") or []:
            if m.get("status") not in ("active", "open"):
                continue
            if _event_day(m.get("event_ticker", "")) != day_str:
                continue                   # only the ladder that settles on THIS close
            bid = _price_to_dollars(m, "yes_bid")
            ask = _price_to_dollars(m, "yes_ask")
            if bid <= 0 and ask <= 0:
                continue                   # no book = no opinion, not a reference price
            fair = bracket_prob(smile, m.get("floor_strike"), m.get("cap_strike"))
            if fair is None:
                continue                   # bracket edge off the fitted ladder
            mid = _mid(bid, ask)

            # Two sides, one bar: the executable price must beat fair value by the
            # taker fee AT THAT PRICE plus the configured edge. Gating on the entry
            # rather than the mid is deliberate - the mid is not a price anyone fills.
            side = None
            if 0 < ask < 1 and MIN_PRICE <= ask <= MAX_PRICE:
                if fair - ask > fee_per_contract(
                        ask, multiplier=INDEX_FEE_MULTIPLIER) + cfg.spx_min_edge:
                    side = ("Yes", ask, fair)
            if side is None and bid > 0:
                no_entry = 1.0 - bid       # buying NO crosses the YES bid
                if MIN_PRICE <= no_entry <= MAX_PRICE and (1.0 - fair) - no_entry > \
                        fee_per_contract(no_entry,
                                         multiplier=INDEX_FEE_MULTIPLIER) + cfg.spx_min_edge:
                    side = ("No", no_entry, 1.0 - fair)
            if side is None:
                continue
            outcome, entry, model_prob = side
            tickets.append({
                "market_id": m.get("ticker", ""),
                "venue": "kalshi",
                "source": "spxindex",
                "outcome": outcome,
                "title": m.get("title", ""),
                "url": f"https://kalshi.com/markets/{m.get('event_ticker', '')}",
                "entry_price": round(entry, 4),
                "model_prob": round(model_prob, 4),
                "edge": round(model_prob - entry, 4),
                # Everything below is why the ticket exists and what re-fits the
                # model later; the trial carries it verbatim (papertrial.MODEL_FIELDS).
                "fair": round(fair, 4),
                "market_prob": round(mid, 4),
                "implied_sigma": round(sigma, 4),
                "spot_used": round(smile.forward, 2),
                "chain_ts": chain.chain_ts,
                "vrp_shrink": cfg.spx_vrp_shrink,
                "series": series,
                "model": MODEL_TAG,
                "validated": False,
                "end_iso": datetime.fromtimestamp(
                    expiry_ts, timezone.utc).isoformat(),
                "event_iso": datetime.fromtimestamp(
                    expiry_ts, timezone.utc).isoformat(),
            })

    tickets.sort(key=lambda t: t["edge"], reverse=True)
    return tickets
