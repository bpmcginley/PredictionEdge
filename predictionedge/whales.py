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
