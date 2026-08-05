"""Crypto statistical edge - no sportsbook, no paid feed.

Kalshi lists terminal price-threshold markets ("ETH >= 5,000 on Jan 1"). Their fair
value is computable from public data: current spot + volatility, via a lognormal
projection P(S_T >= K) = N( (ln(S0/K) - 0.5*v^2*tau) / (v*sqrt(tau)) ). We compare that
model probability to Kalshi's price and fade the divergence - a genuine statistical
signal that runs on free Coinbase data, independent of The Odds API.

Drift is assumed zero (martingale) - a neutral, defensible base case; predicting drift
is the risk, not the edge. Volatility is realized vol from recent daily closes.
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


def _tau_years(expiration_time: str, now: datetime) -> float | None:
    if not expiration_time:
        return None
    try:
        exp = datetime.fromisoformat(expiration_time.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (exp - now).total_seconds() / _YEAR_SECONDS


def find_crypto_edges(cfg: Config, kalshi, data: CryptoData,
                      now: datetime | None = None) -> list[CryptoEdge]:
    """Lognormal-model edges across the configured Kalshi crypto series."""
    if not cfg.crypto_enabled or not cfg.crypto_series:
        return []
    now = now or datetime.now(timezone.utc)
    max_tau = cfg.crypto_max_days / 365.25
    min_tau = cfg.crypto_min_hours / (24 * 365.25)
    out: list[CryptoEdge] = []
    for series in cfg.crypto_series:
        asset = asset_for(series)
        if asset is None:
            continue
        spot, vol = data.spot(asset), data.vol(asset)
        if not spot or not vol:
            continue
        try:
            markets = kalshi.list_markets(series_ticker=series, status="open")
        except Exception:  # noqa: BLE001
            continue
        for m in markets:
            tau = _tau_years(m.expiration_time, now)
            if tau is None or not (min_tau <= tau <= max_tau):
                continue  # short-term only: skip year-out and near-instant markets
            fair = model_prob_yes(m, spot, vol, tau)
            if fair is None:
                continue
            opp = find_edge(m.ticker, fair, m.quote(), cfg)
            if opp is not None:
                out.append(CryptoEdge(opp, asset, fair, spot, m))
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
