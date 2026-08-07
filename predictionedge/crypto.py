"""Crypto statistical edge - no sportsbook, no paid feed.

Kalshi lists terminal price-threshold markets ("ETH >= 5,000 on Jan 1"). Their fair
value is a probability we can compute from public data and compare against Kalshi's
price. There are two ways to compute it, and they are not equally good:

1. **Deribit's option chain (BTC/ETH only).** The digital P(S_T > K) read off the
   option curve as -dC/dK. This is the market's own risk-neutral distribution, priced
   by the deepest crypto vol market that exists, and it is what a threshold market
   actually pays. See `deribit.py`.

2. **A zero-drift lognormal on realized volatility (everything else).** Kept because
   Deribit has no meaningful SOL/XRP chain, but it is known to be misspecified:
   realized vol is a backward-looking property of the price path, not the forward
   risk-neutral distribution, and the gap between them is the entire variance risk
   premium plus the entire skew.

The two are NEVER blended. Each edge carries the `method` that produced it, and for
BTC/ETH a Deribit failure means the market is skipped, not quietly re-priced with the
model we already know is wrong.

Crypto stays paper (`cfg.crypto_live`) either way. This module produces a number worth
judging; it does not decide that the number has earned real money.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import NormalDist
from typing import Protocol

from .config import Config
from .edge import Opportunity, find_edge

_ND = NormalDist()
_YEAR_SECONDS = 365.25 * 24 * 3600

METHOD_DERIBIT = "deribit-rnd"
METHOD_LOGNORMAL = "lognormal-realized-vol"

# Kalshi series ticker prefix -> Coinbase asset symbol.
_ASSET_PREFIX = {"KXETH": "ETH", "KXBTC": "BTC", "KXBCH": "BCH", "KXSOL": "SOL",
                 "KXLTC": "LTC", "KXDOGE": "DOGE", "KXXRP": "XRP"}


def asset_for(series_or_ticker: str) -> str | None:
    s = series_or_ticker.upper()
    for prefix, asset in _ASSET_PREFIX.items():
        if s.startswith(prefix):
            return asset
    return None


def prob_above(spot: float, strike: float, vol: float, tau: float) -> float:
    """Lognormal P(S_T >= strike), zero drift."""
    if spot <= 0 or strike <= 0:
        return 0.0
    if tau <= 0 or vol <= 0:
        return 1.0 if spot >= strike else 0.0
    d = (math.log(spot / strike) - 0.5 * vol * vol * tau) / (vol * math.sqrt(tau))
    return _ND.cdf(d)


def model_prob_yes(market, spot: float, vol: float, tau: float) -> float | None:
    """P(YES) for a Kalshi threshold market from the lognormal model."""
    st, floor, cap = market.strike_type, market.floor_strike, market.cap_strike
    if st == "greater" and floor is not None:
        return prob_above(spot, floor, vol, tau)
    if st == "less" and cap is not None:
        return 1.0 - prob_above(spot, cap, vol, tau)
    if st == "between" and floor is not None and cap is not None:
        return max(0.0, prob_above(spot, floor, vol, tau) - prob_above(spot, cap, vol, tau))
    return None


def deribit_prob_yes(market, pricer, asset: str, expiry: datetime,
                     now_ts: float | None = None) -> float | None:
    """P(YES) for a threshold market from Deribit's implied distribution.

    Every leg must price. A "between" market whose upper strike falls off the quoted
    ladder is not half-priceable - returning the lower leg alone would silently assert
    P(above cap) = 0, which is exactly the kind of invented number this replaces.
    """
    st, floor, cap = market.strike_type, market.floor_strike, market.cap_strike
    if st == "greater" and floor is not None:
        return pricer.prob_above(asset, float(floor), expiry, now_ts)
    if st == "less" and cap is not None:
        p = pricer.prob_above(asset, float(cap), expiry, now_ts)
        return None if p is None else 1.0 - p
    if st == "between" and floor is not None and cap is not None:
        lo = pricer.prob_above(asset, float(floor), expiry, now_ts)
        hi = pricer.prob_above(asset, float(cap), expiry, now_ts)
        if lo is None or hi is None:
            return None
        return max(0.0, lo - hi)
    return None


def annualized_vol(daily_closes: list[float]) -> float | None:
    """Annualized realized volatility from a series of daily closes."""
    closes = [c for c in daily_closes if c and c > 0]
    if len(closes) < 5:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    return math.sqrt(var) * math.sqrt(365.25)


class CryptoData(Protocol):
    def spot(self, asset: str) -> float | None: ...
    def vol(self, asset: str) -> float | None: ...


@dataclass(frozen=True)
class CryptoEdge:
    opp: Opportunity
    asset: str
    model_prob: float
    spot: float
    market: object = None
    # Which distribution produced model_prob. Never presented without it: the two
    # methods are different objects and a reader must be able to tell them apart.
    method: str = METHOD_LOGNORMAL

    @property
    def label(self) -> str:
        return f"{self.asset} ({self.method})"


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def event_time(market) -> datetime | None:
    """When the market's outcome is decided.

    NOT `expiration_time`. On Kalshi crypto series that field is the latest settlement
    deadline and sits a full week after the event: a KXBTCD market closing
    2026-08-06T23:00Z reports expiration_time 2026-08-13T23:00Z. Using it made every
    hours-out market look like a 7-day option, which inflates any vol-based
    probability towards 0.5 and was a large part of the model's disagreement with
    liquid prices. Falls back to expiration_time only when close_time is absent.
    """
    return (_parse_iso(getattr(market, "close_time", "") or "")
            or _parse_iso(getattr(market, "expiration_time", "") or ""))


def _tau_years(market, now: datetime) -> float | None:
    exp = event_time(market)
    if exp is None:
        return None
    return (exp - now).total_seconds() / _YEAR_SECONDS


def find_crypto_edges(cfg: Config, kalshi, data: CryptoData,
                      now: datetime | None = None, pricer=None) -> list[CryptoEdge]:
    """Model-vs-market edges across the configured Kalshi crypto series.

    BTC/ETH price off Deribit's implied distribution; anything else falls back to the
    realized-vol lognormal and is labelled as such. `pricer` is injectable so tests
    (and mock runs) never touch the network.
    """
    if not cfg.crypto_enabled or not cfg.crypto_series:
        return []
    now = now or datetime.now(timezone.utc)
    now_ts = now.timestamp()
    max_tau = cfg.crypto_max_days / 365.25
    min_tau = cfg.crypto_min_hours / (24 * 365.25)
    out: list[CryptoEdge] = []

    for series in cfg.crypto_series:
        asset = asset_for(series)
        if asset is None:
            continue
        use_deribit = cfg.deribit_enabled and asset in cfg.deribit_assets
        if use_deribit and pricer is None:
            from .deribit import DeribitPricer
            pricer = DeribitPricer(cfg)

        spot = data.spot(asset) if data is not None else None
        vol = data.vol(asset) if data is not None else None
        if use_deribit:
            spot = spot or (pricer.spot(asset) or 0.0)
        elif not spot or not vol:
            continue    # lognormal needs both; no inputs means no number

        try:
            markets = kalshi.list_markets(series_ticker=series, status="open")
        except Exception:  # noqa: BLE001
            continue
        for m in markets:
            tau = _tau_years(m, now)
            if tau is None or not (min_tau <= tau <= max_tau):
                continue  # short-term only: skip year-out and near-instant markets
            if use_deribit:
                exp = event_time(m)
                fair = (deribit_prob_yes(m, pricer, asset, exp, now_ts)
                        if exp is not None else None)
                method = METHOD_DERIBIT
            else:
                fair = model_prob_yes(m, spot, vol, tau)
                method = METHOD_LOGNORMAL
            if fair is None:
                continue    # BTC/ETH fails closed here rather than dropping back to
                            # the lognormal - mixing the two under one number is worse
                            # than having no number at all
            opp = find_edge(m.ticker, fair, m.quote(), cfg)
            if opp is not None:
                out.append(CryptoEdge(opp, asset, fair, spot or 0.0, m, method))
    out.sort(key=lambda c: c.opp.expected_value, reverse=True)
    return out


class CoinbaseData:
    """Free spot + realized vol from Coinbase public endpoints (cached per process)."""

    _cache: dict = {}

    def spot(self, asset: str) -> float | None:
        import requests
        try:
            r = requests.get(f"https://api.coinbase.com/v2/prices/{asset}-USD/spot", timeout=10)
            r.raise_for_status()
            return float(r.json()["data"]["amount"])
        except Exception:  # noqa: BLE001
            return None

    def vol(self, asset: str, days: int = 45) -> float | None:
        import time
        hit = self._cache.get(asset)
        if hit is not None and time.time() - hit[0] < 3600:  # vol changes slowly
            return hit[1]
        import requests
        try:
            r = requests.get(f"https://api.exchange.coinbase.com/products/{asset}-USD/candles",
                             params={"granularity": 86400}, timeout=12)
            r.raise_for_status()
            # candles: [time, low, high, open, close, volume], newest first
            closes = [row[4] for row in r.json()][:days][::-1]
            v = annualized_vol(closes)
            if v is not None:
                self._cache[asset] = (time.time(), v)
            return v
        except Exception:  # noqa: BLE001
            return None
