"""Whale-as-primary-signal edge - for politics / econ / news markets.

Sports markets get the de-vig sportsbook fade (scanner.py): sportsbooks are sharp,
so the edge is mispricing vs their consensus. But sports outcomes aren't insider-
driven, so whale-following adds little there.

Politics / economics / current events are the opposite: there's no sportsbook to
de-vig, and they're exactly where informed/insider money has an edge. Here the
**smart-money positioning is itself the fair-value estimate** - we take a confident
whale signal as P(YES) and fade Kalshi when it diverges past the fee threshold.

Markets are opted in via the whale map (Kalshi ticker -> Polymarket condition_id);
populate it with politics/econ/news markets, not games.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .edge import Opportunity, find_edge
from .whales import WhaleSignal


@dataclass(frozen=True)
class WhaleEdge:
    opp: Opportunity
    signal: WhaleSignal
    label: str
    market: object = None   # the KalshiMarket (for web URL)


def find_whale_edges(cfg: Config, kalshi, whales) -> list[WhaleEdge]:
    """Edges where a confident whale signal is the fair value. Empty if disabled
    or the provider has no mapped markets."""
    market_map = getattr(whales, "market_map", None)
    if not cfg.whale_primary or not market_map:
        return []

    out: list[WhaleEdge] = []
    for ticker in market_map:
        sig = whales.signal_for(ticker)
        if sig is None or sig.confidence < cfg.whale_primary_min_conf:
            continue
        market = kalshi.market(ticker)
        if market is None:
            continue
        opp = find_edge(ticker, sig.prob, market.quote(), cfg)
        if opp is not None:
            out.append(WhaleEdge(opp, sig, market.title or ticker, market))

    out.sort(key=lambda w: w.opp.expected_value, reverse=True)
    return out
