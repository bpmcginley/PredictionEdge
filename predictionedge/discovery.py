"""Dynamic market discovery: turn live Kalshi sports markets into matched links.

Pulls open markets for the configured Kalshi sports series, parses each into its two
teams, and matches them to the current sportsbook events. Off by default
(``dynamic_discovery``) until the verified sports series tickers + title format land;
the matching itself is format-independent (see matching.py).
"""

from __future__ import annotations

from .matching import KalshiSportMarket, MarketLink, build_links, parse_sports_market
from .odds import SportEvent


def discover_links(cfg, kalshi, events: list[SportEvent]) -> list[MarketLink]:
    """Discover & match Kalshi sports markets to the given sportsbook events."""
    if not cfg.dynamic_discovery or not cfg.sports_series:
        return []
    parsed: list[KalshiSportMarket] = []
    for series in cfg.sports_series:
        try:
            markets = kalshi.list_markets(series_ticker=series, status="open")
        except Exception:  # noqa: BLE001 - a bad series must not sink the whole scan
            continue
        for m in markets:
            pm = parse_sports_market(m)
            if pm is not None:
                parsed.append(pm)
    return build_links(parsed, events)
