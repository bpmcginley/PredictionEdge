"""Whale / smart-money flow - the supporting signal.

The plan: read public Polymarket on-chain order flow (Polygon), identify wallets
with persistent, out-of-sample positive PnL, and turn their recent positioning on
a matched market into a probability estimate plus a confidence. That estimate
nudges the de-vig fair value (see fairvalue.blended_fair_prob).

Legal note: international Polymarket is a *read-only* data source for a US person.
We never trade it - execution stays on Kalshi.

This module ships the interface and a neutral provider. Wiring it to live
on-chain data is a later phase; until a wallet-selection method is validated
out-of-sample (no look-ahead in picking "smart" wallets), whale_weight stays 0.
"""

from __future__ import annotations

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .polymarket import (
    LivePolymarketDataClient,
    MockPolymarketDataClient,
    PolymarketDataClient,
    WalletStat,
)

if TYPE_CHECKING:
    from .config import Config


@dataclass(frozen=True)
class WhaleSignal:
    """Smart-money read on a single market outcome."""
    prob: float          # smart-money implied probability of the YES outcome, 0..1
    confidence: float    # 0..1, how strongly to trust it (depth, recency, sample size)
    source: str = "polymarket-onchain"


class WhaleSignalProvider(Protocol):
    def signal_for(self, kalshi_ticker: str) -> WhaleSignal | None:
        """Return a whale signal for the matched market, or None if unavailable."""
        ...


class NeutralWhaleProvider:
    """Returns no signal. The safe default until the on-chain reader is validated."""

    def signal_for(self, kalshi_ticker: str) -> WhaleSignal | None:
        return None


@dataclass(frozen=True)
class SmartMoneyConfig:
    min_resolved_markets: int = 30   # demand a real sample, not a lucky streak
    min_realized_pnl: float = 5000.0
    min_win_rate: float = 0.0
    min_roi: float = 0.01            # PnL/volume floor - excludes churny market-makers
    max_wallets: int = 50


class SmartWalletScorer:
    """Selects 'smart money' wallets without look-ahead.

    Track records are measured on already-RESOLVED markets, so a wallet's score
    reflects only outcomes known before now. The sample-size floor guards against a
    small-n hot streak; the score rewards PnL scaled by sqrt(sample) and win rate,
    which down-weights one-big-bet luck and churny market-makers.
    """

    def __init__(self, cfg: SmartMoneyConfig | None = None):
        self.cfg = cfg or SmartMoneyConfig()

    def is_smart(self, w: WalletStat) -> bool:
        roi = w.realized_pnl / w.volume if w.volume > 0 else 0.0
        if w.realized_pnl < self.cfg.min_realized_pnl or roi < self.cfg.min_roi:
            return False
        # Sample-size / win-rate floors apply only when those metrics are known. The
        # Polymarket leaderboard gives PnL + volume; resolved-market count and win rate
        # need a per-wallet enrichment call, so 0 means "unknown", not "fails".
        if 0 < w.resolved_markets < self.cfg.min_resolved_markets:
            return False
        if 0 < w.win_rate < self.cfg.min_win_rate:
            return False
        return True

    def score(self, w: WalletStat) -> float:
        return w.realized_pnl * math.sqrt(max(w.resolved_markets, 1)) * (0.5 + w.win_rate)

    def smart_set(self, wallets: list[WalletStat]) -> set[str]:
        smart = sorted((w for w in wallets if self.is_smart(w)),
                       key=self.score, reverse=True)
        return {w.address for w in smart[: self.cfg.max_wallets]}


DATA_API = "https://data-api.polymarket.com"
_PROFILE_CACHE = "data/wallet_profiles.json"
# A wallet's book moves slowly and the board reruns every 15 minutes, so a stale-by-hours
# bankroll is fine; a per-run refetch is not (the endpoint costs ~0.8s per wallet and
# does NOT batch - verified 2026-08-06: repeated ?user= params return only the last one,
# and a comma-joined list 400s).
_PROFILE_TTL_S = 12 * 3600
_PROFILE_WORKERS = 8
_PROFILE_MAX_WALLETS = 300    # hard bound on one run's fan-out
# Two facts with very different shelf lives. A wallet's open book moves hourly; the
# timestamp of its FIRST trade is immutable once observed, so re-asking is pure waste.
_TTL_BY_KIND = {"value": _PROFILE_TTL_S, "first_seen": 30 * 24 * 3600}


def _default_fetch(url: str, params: dict):
    import requests
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


class WalletProfiler:
    """Per-wallet facts the leaderboard does not carry, batched and cached.

    Every lookup here is one HTTP round trip per wallet, so a naive per-signal call
    across a full feed would blow the cron budget. Two defences: an in-process memo
    plus a TTL'd disk cache (the smart-wallet set barely changes run to run, so the
    steady state is ~zero calls), and a bounded thread pool for the cold path.

    Unknown is a first-class answer. Any failure - network, empty body, zero value -
    yields ``None``, and callers must degrade to their prior behaviour or reject,
    never substitute a guess.
    """

    def __init__(self, fetch=None, *, base: str = DATA_API,
                 cache_path: str | None = _PROFILE_CACHE, ttl_s: float | None = None,
                 max_wallets: int = _PROFILE_MAX_WALLETS, now_fn=time.time):
        self.fetch = fetch or _default_fetch
        self.base = base.rstrip("/")
        self.cache_path = cache_path
        self.ttl_override = ttl_s
        self.max_wallets = max_wallets
        self._now = now_fn
        # (kind, address) -> (value_or_None, fetched_at). "Looked and found nothing" is
        # cached too, so a wallet the API cannot answer for is not re-asked all day.
        self._mem: dict[tuple[str, str], tuple[float | None, float]] = {}
        self._loaded = False
        self.calls = 0            # visible so a run can report its own API cost

    # -- cache -------------------------------------------------------------
    def _ttl(self, kind: str) -> float:
        return self.ttl_override if self.ttl_override is not None else _TTL_BY_KIND[kind]

    def _load_cache(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.cache_path:
            return
        try:
            raw = json.loads(Path(self.cache_path).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a corrupt cache is a cold start, not an error
            return
        if not isinstance(raw, dict):
            return
        for kind, rows in raw.items():
            if kind not in _TTL_BY_KIND or not isinstance(rows, dict):
                continue
            for addr, rec in rows.items():
                try:
                    val = rec.get("v")
                    self._mem[(kind, addr)] = (None if val is None else float(val),
                                               float(rec.get("t") or 0.0))
                except Exception:  # noqa: BLE001
                    continue

    def _save_cache(self) -> None:
        if not self.cache_path:
            return
        try:
            out: dict[str, dict] = {k: {} for k in _TTL_BY_KIND}
            for (kind, addr), (val, ts) in self._mem.items():
                out.setdefault(kind, {})[addr] = {"v": val, "t": ts}
            p = Path(self.cache_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(out), encoding="utf-8")
        except Exception:  # noqa: BLE001 - caching is an optimisation, never a failure
            pass

    def _cached(self, kind: str, addr: str):
        rec = self._mem.get((kind, addr))
        if rec is None:
            return None
        return rec if (self._now() - rec[1]) < self._ttl(kind) else None

    def _lookup(self, kind: str, addresses, fetch_one) -> dict[str, float]:
        """Cached, bounded, parallel fan-out. Missing key in the result = unknown."""
        self._load_cache()
        wanted = list(dict.fromkeys(a for a in addresses if a))
        todo = [a for a in wanted if self._cached(kind, a) is None][: self.max_wallets]
        if todo:
            now = self._now()
            with ThreadPoolExecutor(max_workers=_PROFILE_WORKERS) as pool:
                for addr, val in zip(todo, pool.map(fetch_one, todo)):
                    self._mem[(kind, addr)] = (val, now)
            self._save_cache()
        out: dict[str, float] = {}
        for a in wanted:
            rec = self._cached(kind, a)
            if rec is not None and rec[0]:
                out[a] = rec[0]
        return out

    # -- fetch -------------------------------------------------------------
    def _fetch_bankroll(self, addr: str) -> float | None:
        """Open-position value for one wallet. None on anything unexpected."""
        try:
            self.calls += 1
            data = self.fetch(f"{self.base}/value", {"user": addr})
        except Exception:  # noqa: BLE001
            return None
        rows = data if isinstance(data, list) else [data]
        for r in rows:
            if not isinstance(r, dict):
                continue
            try:
                v = float(r.get("value") or 0.0)
            except Exception:  # noqa: BLE001
                continue
            # A wallet with nothing open tells us nothing about its size, and dividing
            # by it would read as "they bet infinity percent of themselves".
            return v if v > 0 else None
        return None

    def _fetch_first_seen(self, addr: str) -> float | None:
        """Timestamp of the wallet's oldest recorded activity, or None.

        ``sortDirection=ASC`` is what makes this one call instead of paging a whole
        history: the API hands back the earliest event directly.
        """
        try:
            self.calls += 1
            data = self.fetch(f"{self.base}/activity",
                              {"user": addr, "limit": 1, "sortDirection": "ASC",
                               "sortBy": "TIMESTAMP"})
        except Exception:  # noqa: BLE001
            return None
        rows = data if isinstance(data, list) else data.get("data") or []
        for r in rows:
            if not isinstance(r, dict):
                continue
            try:
                ts = float(r.get("timestamp") or 0.0)
            except Exception:  # noqa: BLE001
                return None
            return ts if ts > 0 else None
        # An empty history is not "brand new" - it is an address the endpoint would not
        # answer for. Saying "age zero" here would make every unreadable wallet look
        # like the freshest possible signal, which is exactly backwards.
        return None

    def bankrolls(self, addresses) -> dict[str, float]:
        """{address: bankroll} for the wallets we could price. Missing key = unknown."""
        return self._lookup("value", addresses, self._fetch_bankroll)

    def first_seen(self, addresses) -> dict[str, float]:
        """{address: unix ts of first activity}. Missing key = unknown, not new."""
        return self._lookup("first_seen", addresses, self._fetch_first_seen)


def build_wallet_profiler(cfg: "Config", client=None, fetch=None) -> WalletProfiler | None:
    """A profiler when bankroll weighting is on, else None (= keep flat dollar sizing).

    Returns None for the mock client too: offline runs must stay offline, and a mock
    wallet has no bankroll to look up. Callers that want to exercise the weighting
    against canned data inject their own profiler.
    """
    if not getattr(cfg, "whale_bankroll_weighting", False):
        return None
    if isinstance(client, MockPolymarketDataClient):
        return None
    return WalletProfiler(fetch=fetch)


class PolymarketWhaleProvider:
    """Turns smart-money positioning on a matched Polymarket market into a signal.

    ``market_map`` pairs a Kalshi ticker to the Polymarket market id that resolves on
    the same event (the cross-venue overlap step). The signal's probability is the
    smart wallets' YES share of notional; confidence grows with how many smart
    wallets and how much size stand behind it.
    """

    def __init__(self, client: PolymarketDataClient, scorer: SmartWalletScorer,
                 market_map: dict[str, str], source: str = "polymarket-onchain",
                 enrich: bool = True):
        self.client = client
        self.scorer = scorer
        self.market_map = market_map
        self.source = source
        self.enrich = enrich
        self._smart: set[str] | None = None

    def _smart_addresses(self) -> set[str]:
        if self._smart is None:
            wallets = self.client.leaderboard()
            if self.enrich:
                wallets = [self._enriched(w) for w in wallets]
            self._smart = self.scorer.smart_set(wallets)
        return self._smart

    def _enriched(self, w: WalletStat) -> WalletStat:
        """Fill resolved-market count + win rate from the wallet's closed positions.

        The leaderboard gives PnL + volume only; this adds the sample-size and
        win-rate the scorer needs to reject lucky streaks. Best-effort - if the
        enrichment call fails or is empty, the wallet is left as-is.
        """
        try:
            closed = self.client.closed_positions(w.address)
        except Exception:  # noqa: BLE001
            closed = []
        if not closed:
            return w
        resolved = len(closed)
        wins = sum(1 for c in closed if c.realized_pnl > 0)
        return replace(w, resolved_markets=resolved,
                       win_rate=wins / resolved if resolved else 0.0)

    def signal_for(self, kalshi_ticker: str) -> WhaleSignal | None:
        """The informed Polymarket price as P(YES) - the fair value to fade Kalshi.

        Polymarket is where whales/insiders trade, so its *price* is the smart-money
        consensus probability (holders' position split is NOT a probability - using it
        as one wrongly implied longshots at ~0.46). Confidence scales with the market's
        liquidity (deep book -> trust the price) and is boosted when wallets from the
        profitable-trader leaderboard are among the holders.
        """
        market_id = self.market_map.get(kalshi_ticker)
        if not market_id:
            return None
        quote = self.client.market_price(market_id)
        if quote is None:
            return None
        yes_price, _volume, liquidity = quote
        if not 0.0 < yes_price < 1.0:
            return None
        holders = self.client.market_holders(market_id)
        n_smart = sum(1 for h in holders if h.address in self._smart_addresses())
        depth = 1.0 - math.exp(-liquidity / 50000.0)    # deep book -> trust the price
        smart_boost = 0.6 + 0.4 * min(1.0, n_smart / 3.0)
        confidence = min(1.0, depth * smart_boost)
        return WhaleSignal(prob=yes_price, confidence=confidence, source=self.source)


def _load_market_map(path: str) -> dict[str, str]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()}
    except Exception:  # noqa: BLE001
        return {}


def build_whale_provider(cfg: "Config", mock: bool = False, enrich: bool | None = None):
    """Factory: a real Polymarket provider when configured, else the neutral stub.

    ``enrich`` overrides cfg.whale_enrich - the dashboard passes False so its preview
    stays fast (no per-wallet calls); the trading runner uses the full enriched filter.
    """
    if getattr(cfg, "whale_source", "none") != "polymarket":
        return NeutralWhaleProvider()
    client = MockPolymarketDataClient() if mock else LivePolymarketDataClient()
    scorer = SmartWalletScorer(SmartMoneyConfig(
        min_resolved_markets=cfg.whale_min_resolved,
        min_realized_pnl=cfg.whale_min_pnl,
        max_wallets=cfg.whale_max_wallets,
    ))
    use_enrich = getattr(cfg, "whale_enrich", True) if enrich is None else enrich
    return PolymarketWhaleProvider(client, scorer, _load_market_map(cfg.whale_map_path),
                                   enrich=use_enrich)
