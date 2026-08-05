"""Turn live inputs into ranked opportunities (shared by the CLI and the runner)."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .discovery import discover_links
from .edge import Opportunity, find_edge
from .fairvalue import blended_fair_prob
from .matching import DEFAULT_LINKS, MarketLink, orient
from .odds import SportEvent, consensus_fair_prob


@dataclass(frozen=True)
class ScanResult:
    opp: Opportunity
    fair_yes: float     # P(YES) used for the decision
    label: str
    link: MarketLink
    market: object = None   # the KalshiMarket (for team names + web URL)


def resolve_links(cfg: Config, kalshi, events: list[SportEvent]) -> list[MarketLink]:
    """Dynamic auto-matched links when enabled (and any found), else the static set."""
    if cfg.dynamic_discovery and cfg.sports_series:
        links = discover_links(cfg, kalshi, events)
        if links:
            return links[: cfg.max_scan_markets]   # bound per-market API calls
    return DEFAULT_LINKS


def find_opportunities(cfg: Config, odds, kalshi, whales) -> list[ScanResult]:
    """Scan all linked markets, return positive-EV opportunities, best EV first."""
    events_list = odds.events()
    events = {e.event_id: e for e in events_list}
    results: list[ScanResult] = []
    for link in resolve_links(cfg, kalshi, events_list):
        event = events.get(link.event_id)
        if event is None:
            continue
        p0 = consensus_fair_prob(event, cfg.devig_method)
        if p0 is None:
            continue
        fair_yes = orient(p0, link)
        whale = whales.signal_for(link.kalshi_ticker)
        fair = blended_fair_prob(fair_yes, whale, cfg.whale_weight)

        market = kalshi.market(link.kalshi_ticker)
        if market is None:
            continue
        opp = find_edge(market.ticker, fair, market.quote(), cfg)
        if opp is not None:
            results.append(ScanResult(opp, fair, event.label, link, market))

    results.sort(key=lambda r: r.opp.expected_value, reverse=True)
    return results


def event_prob_for_side(fair_yes: float, side: str) -> float:
    """Probability of the event the order is ON (YES side -> P(YES), NO side -> P(NO))."""
    return fair_yes if side == "yes" else 1.0 - fair_yes
