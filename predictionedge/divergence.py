"""Report Polymarket-vs-Kalshi price divergence for mapped markets.

    python -m predictionedge.divergence

For each market in whale_map.json, prints the informed Polymarket price next to the
Kalshi price; a large gap is a candidate whale-driven edge (and, conversely, agreement
means no edge - which is why the World Cup map shows ~0). Use this to vet a pair before
trusting it, and to decide which political/econ markets are worth keeping in the map.
"""

from __future__ import annotations

from .config import Config
from .kalshi import LiveKalshiReadOnlyClient
from .whales import build_whale_provider


def main(argv: list[str] | None = None) -> int:
    cfg = Config.from_env()
    kalshi = LiveKalshiReadOnlyClient(cfg.kalshi_base_url, cfg.kalshi_key_id,
                                      cfg.kalshi_private_key_path)
    whales = build_whale_provider(cfg, enrich=False)
    market_map = getattr(whales, "market_map", {})
    if not market_map:
        print("whale_map empty - populate data/whale_map.json (kalshi_ticker -> condition_id)")
        return 0

    rows = []
    for ticker in market_map:
        sig = whales.signal_for(ticker)
        market = kalshi.market(ticker)
        if sig is None or market is None:
            continue
        gap = abs(sig.prob - market.yes_ask)
        rows.append((gap, ticker, sig.prob, market.yes_ask, market.title))

    rows.sort(reverse=True)
    print(f"{'gap':>6}  {'PM':>6} {'Kalshi':>7}  ticker  title")
    for gap, ticker, pm, kalshi_price, title in rows:
        print(f"{gap * 100:5.1f}c  {pm:6.3f} {kalshi_price:7.3f}  {ticker}  {title[:44]}")
    if not rows:
        print("(no mapped markets returned both a Polymarket price and a Kalshi market)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
