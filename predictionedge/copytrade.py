"""Copy-trading signal - mirror historically-profitable Polymarket wallets.

Identify wallets with a strong track record (the per-category PnL leaderboards), watch
the recent large-trade feed for buys BY those wallets, and surface "smart money is
buying X" signals across *any* market (sports, politics, crypto, world events) - not
just game winners. This is the Polymarket-native edge: follow proven informed money.

Free data (Polymarket Data API). Execution is on Polymarket US (a market the smart
wallet bought is taken in the same direction, sized small under the caps).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .polymarket_us import BUY_NO, BUY_YES
from .whales import SmartWalletScorer


@dataclass(frozen=True)
class CopySignal:
    market_id: str         # Polymarket condition_id
    title: str
    outcome: str           # the outcome smart money bought (Yes/No/team)
    n_wallets: int         # how many distinct profitable wallets bought it
    total_usd: float       # their combined size
    avg_price: float       # their volume-weighted entry price
    minutes_ago: float     # recency of the latest such buy
    slug: str = ""         # market slug = the Polymarket US order key (marketSlug)
    event_slug: str = ""


def smart_universe(client, scorer: SmartWalletScorer, categories) -> set[str]:
    """Union of profitable wallets across the given leaderboard categories."""
    smart: set[str] = set()
    for cat in categories:
        try:
            smart |= scorer.smart_set(client.leaderboard(cat))
        except Exception:  # noqa: BLE001
            continue
    return smart


def find_copy_signals(client, scorer: SmartWalletScorer, *,
                      categories=("OVERALL", "SPORTS", "POLITICS", "CRYPTO"),
                      min_usd: float = 10000, limit: int = 500,
                      max_price: float = 0.90, min_wallets: int = 1,
                      now_ts: float | None = None) -> list[CopySignal]:
    """Recent large BUYS by profitable wallets, grouped into copy signals."""
    now = time.time() if now_ts is None else now_ts
    smart = smart_universe(client, scorer, categories)
    if not smart:
        return []

    groups: dict[tuple[str, str], dict] = {}
    for t in client.recent_trades(min_usd=min_usd, limit=limit):
        if t.side != "BUY" or t.wallet not in smart:
            continue
        g = groups.setdefault((t.condition_id, t.outcome), {
            "title": t.title, "event_slug": t.event_slug, "slug": t.slug,
            "wallets": set(), "usd": 0.0, "pxusd": 0.0, "latest": 0,
        })
        g["wallets"].add(t.wallet)
        g["usd"] += t.size
        g["pxusd"] += t.size * t.price
        g["latest"] = max(g["latest"], t.ts)

    out: list[CopySignal] = []
    for (cid, outcome), g in groups.items():
        if len(g["wallets"]) < min_wallets:
            continue
        avg = g["pxusd"] / g["usd"] if g["usd"] > 0 else 0.0
        if avg <= 0 or avg > max_price:
            continue  # no upside left if they're already near resolution
        out.append(CopySignal(cid, g["title"], outcome, len(g["wallets"]), g["usd"],
                              avg, max(0.0, (now - g["latest"]) / 60.0),
                              g["slug"], g["event_slug"]))
    out.sort(key=lambda s: s.total_usd, reverse=True)
    return out


def copy_order_params(signal: CopySignal, market, *, min_price: float = 0.05,
                      max_price: float = 0.90, size_usd: float = 5.0):
    """(intent, price, quantity) for a Polymarket US copy order, or None if not actionable.

    Side mirrors what the smart money bought; price comes from PM-US's own book (BUY YES
    at the YES ask; BUY NO at the NO ask = 1 - YES bid), capped to a sane band.
    """
    if market is None:
        return None
    buy_no = signal.outcome.strip().lower() == "no"
    intent = BUY_NO if buy_no else BUY_YES
    price = round(1.0 - market.yes_bid, 2) if buy_no else round(market.yes_ask, 2)
    if not (min_price <= price <= max_price):
        return None
    qty = int(size_usd / price)
    if qty < 1:
        return None
    return intent, price, qty
