"""Polymarket public data access - READ ONLY.

International Polymarket (Polygon) is geoblocked for US *trading*, but its data is
public, which is what makes whale-following possible: every wallet's trades and
positions are visible. We read it for signal only and execute on Kalshi.

This ships the data interface, a mock client (drives tests), and a live client
scaffold. The exact endpoints/paths are pending verification (research workflow);
the live client is written against known Polymarket conventions
(data-api / gamma-api) and flagged - confirm against docs.polymarket.com before
relying on live numbers. The smart-money logic in whales.py is endpoint-independent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class WalletStat:
    """A trader's track record, measured on RESOLVED markets only (no look-ahead)."""
    address: str
    realized_pnl: float       # USD, realised on already-resolved markets
    resolved_markets: int     # sample size - guards against luck
    volume: float             # USD traded
    win_rate: float           # 0..1 across resolved markets


@dataclass(frozen=True)
class MarketHolder:
    """A wallet's current position on one market's YES/NO, counted in CONTRACTS.

    Contracts, not dollars, because that is what the /holders endpoint reports and
    because a position has no price attached: unlike a trade, nothing here says what
    the wallet paid. Turning these into cash needs a price, and the two prices you
    could pick are different quantities - the wallet's own ``avgPrice`` from
    /positions gives cost basis (what they risked), the current market price gives
    mark-to-market value (what it is worth now). Multiplying by today's price and
    calling the result "notional" quietly reports the second while sounding like the
    first, so a caller that needs money must fetch /positions and say which it means.
    """
    address: str
    yes_size: float           # contracts held on YES (outcome token count)
    no_size: float            # contracts held on NO


@dataclass(frozen=True)
class ClosedPosition:
    """A wallet's settled position - used to derive sample size + win rate."""
    realized_pnl: float


@dataclass(frozen=True)
class Trade:
    """A single large trade off the public feed (the insider-flow signal)."""
    wallet: str
    name: str          # trader pseudonym, if any
    side: str          # "BUY" | "SELL"
    # CONTRACTS (outcome tokens), NOT dollars. The cash that changed hands is
    # ``size * price`` - at a 30c fill, 10,000 contracts cost $3,000. This field was
    # once commented "USDC", and every consumer that believed it overstated whale size
    # by 1/price; see copytrade.scan_smart_flow, which now keeps the two apart by name.
    # Confirmed against the live feed: /trades?filterType=CASH&filterAmount=5000 returns
    # trades priced at 23c whose `size` is 11,000+ - the bar binds on size*price.
    size: float        # contracts; multiply by `price` for USDC
    price: float       # 0..1, the probability paid per contract
    ts: int            # unix seconds
    title: str
    outcome: str       # which side they took (e.g. "Yes", a team, "Up")
    condition_id: str
    event_slug: str
    slug: str


class PolymarketDataClient(Protocol):
    def leaderboard(self, category: str = "OVERALL") -> list[WalletStat]: ...
    def market_holders(self, market_id: str) -> list[MarketHolder]: ...
    def closed_positions(self, address: str) -> list[ClosedPosition]: ...
    def market_price(self, market_id: str) -> tuple[float, float, float] | None: ...
    def recent_trades(self, min_usd: float = 25000, limit: int = 100) -> list["Trade"]: ...


class MockPolymarketDataClient:
    """Canned data for offline runs and tests."""

    def __init__(self, leaderboard: list[WalletStat] | None = None,
                 holders: dict[str, list[MarketHolder]] | None = None,
                 closed: dict[str, list[float]] | None = None,
                 prices: dict[str, tuple[float, float, float]] | None = None,
                 trades: list[Trade] | None = None):
        self._lb = leaderboard or [
            WalletStat("0xSHARP1", realized_pnl=120_000, resolved_markets=180,
                       volume=2_000_000, win_rate=0.61),
            WalletStat("0xSHARP2", realized_pnl=45_000, resolved_markets=90,
                       volume=600_000, win_rate=0.57),
            WalletStat("0xLUCKY", realized_pnl=8_000, resolved_markets=4,
                       volume=20_000, win_rate=1.0),    # tiny sample -> not "smart"
            WalletStat("0xMM", realized_pnl=30_000, resolved_markets=400,
                       volume=50_000_000, win_rate=0.50),  # market-maker-ish churn
        ]
        self._holders = holders or {}
        self._closed = closed or {}
        self._prices = prices or {}
        self._trades = trades or []

    def leaderboard(self, category: str = "OVERALL") -> list[WalletStat]:
        return list(self._lb)

    def market_holders(self, market_id: str) -> list[MarketHolder]:
        return list(self._holders.get(market_id, []))

    def closed_positions(self, address: str) -> list[ClosedPosition]:
        return [ClosedPosition(p) for p in self._closed.get(address, [])]

    def market_price(self, market_id: str) -> tuple[float, float, float] | None:
        return self._prices.get(market_id)

    def recent_trades(self, min_usd: float = 25000, limit: int = 100) -> list[Trade]:
        # Cash, like the live client: the API's filterType=CASH bar is size*price, so a
        # mock that compared the contract count admitted trades the real feed never
        # sends - a 6,000-contract fill at 25c is $1,500 and does not clear a $5k bar.
        # A mock whose filter is in different units from production cannot catch a
        # units bug, which is exactly how one lived here.
        return [t for t in self._trades if t.size * t.price >= min_usd][:limit]


class LivePolymarketDataClient:
    """Live read client using the verified Polymarket Data API (public, no auth).

    Returns [] on any error so a data hiccup never crashes a trading cycle. Note:
    ``market_id`` must be a Polymarket **condition_id** (0x + 64 hex) - slugs/token
    ids are silently ignored by the Data API (resolve slug -> condition_id via the
    Gamma API first). Wallet addresses are **proxy** wallets (the ERC1155 holders).
    """

    def __init__(self, data_base: str = "https://data-api.polymarket.com"):
        self.data_base = data_base.rstrip("/")

    def leaderboard(self, category: str = "OVERALL", time_period: str = "ALL",
                    limit: int = 50) -> list[WalletStat]:
        """Top wallets by PnL. ``time_period`` ALL captures career track record;
        shorter windows (MONTH/WEEK) surface who is hot *now*, which all-time PnL
        hides behind whales who made their money years ago."""
        import requests
        try:
            resp = requests.get(f"{self.data_base}/v1/leaderboard",
                                params={"orderBy": "PNL", "timePeriod": time_period,
                                        "category": category, "limit": limit},
                                timeout=15)
            resp.raise_for_status()
            rows = resp.json()
            rows = rows if isinstance(rows, list) else rows.get("data", [])
            out: list[WalletStat] = []
            for r in rows:
                addr = r.get("proxyWallet") or r.get("address", "")
                if not addr:
                    continue
                # Leaderboard exposes PnL + volume only; resolved-market count and win
                # rate (0 here = unknown) would come from a per-wallet enrichment call.
                out.append(WalletStat(
                    address=addr,
                    realized_pnl=float(r.get("pnl") or 0.0),
                    resolved_markets=0,
                    volume=float(r.get("vol") or r.get("volume") or 0.0),
                    win_rate=0.0,
                ))
            return out
        except Exception:  # noqa: BLE001 - data source must never break a cycle
            return []

    def market_holders(self, market_id: str) -> list[MarketHolder]:
        import requests
        try:
            resp = requests.get(f"{self.data_base}/holders",
                                params={"market": market_id, "limit": 20}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            # Shape: [{token, holders:[{proxyWallet, amount, outcomeIndex}]}] (one
            # group per outcome token). outcomeIndex 0 = YES, 1 = NO. `amount` is a
            # CONTRACT count: the same wallet+market read back off /positions returns
            # an identical `size`, alongside the avgPrice/initialValue that this
            # endpoint does not carry (checked live 2026-08-16: amount 71704.105913,
            # /positions size 71704.1059, avgPrice 0.6274, initialValue 44993.54).
            groups = data if isinstance(data, list) else [data]
            out: list[MarketHolder] = []
            for g in groups:
                for h in g.get("holders", []):
                    addr = h.get("proxyWallet") or h.get("address", "")
                    if not addr:
                        continue
                    size = float(h.get("amount") or 0.0)
                    if int(h.get("outcomeIndex", 0)) == 0:
                        out.append(MarketHolder(addr, yes_size=size, no_size=0.0))
                    else:
                        out.append(MarketHolder(addr, yes_size=0.0, no_size=size))
            return out
        except Exception:  # noqa: BLE001
            return []

    def closed_positions(self, address: str) -> list[ClosedPosition]:
        import requests
        try:
            resp = requests.get(f"{self.data_base}/closed",
                                params={"user": address, "limit": 500}, timeout=15)
            resp.raise_for_status()
            rows = resp.json()
            rows = rows if isinstance(rows, list) else rows.get("data", [])
            out: list[ClosedPosition] = []
            for r in rows:
                pnl = r.get("realizedPnl")
                if pnl is None:
                    pnl = r.get("cashPnl", 0)
                out.append(ClosedPosition(float(pnl or 0.0)))
            return out
        except Exception:  # noqa: BLE001
            return []

    def market_price(self, market_id: str) -> tuple[float, float, float] | None:
        """The informed market's YES probability + depth, via Gamma. condition_id key.

        Returns (yes_price, volume, liquidity). The price is the smart-money consensus
        probability (whales dominate Polymarket) - the right fair value to fade Kalshi
        against; volume/liquidity feed signal confidence.
        """
        import json as _json
        import requests
        try:
            r = requests.get("https://gamma-api.polymarket.com/markets",
                             params={"condition_ids": market_id}, timeout=15)
            r.raise_for_status()
            rows = r.json()
            mk = rows[0] if rows else None
            if not mk:
                return None
            prices = mk.get("outcomePrices")
            if isinstance(prices, str):
                prices = _json.loads(prices)
            if not prices:
                return None
            return (float(prices[0]), float(mk.get("volume") or 0.0),
                    float(mk.get("liquidity") or 0.0))
        except Exception:  # noqa: BLE001
            return None

    def recent_trades(self, min_usd: float = 25000, limit: int = 100) -> list[Trade]:
        """Recent global trades >= min_usd (the large-bet / insider-flow feed)."""
        import requests
        try:
            r = requests.get(f"{self.data_base}/trades",
                             params={"takerOnly": "true", "filterType": "CASH",
                                     "filterAmount": int(min_usd), "limit": int(limit)},
                             timeout=15)
            r.raise_for_status()
            out: list[Trade] = []
            for t in r.json():
                out.append(Trade(
                    wallet=t.get("proxyWallet", ""),
                    name=t.get("pseudonym") or t.get("name") or "",
                    side=(t.get("side") or "").upper(),
                    size=float(t.get("size") or 0.0),
                    price=float(t.get("price") or 0.0),
                    ts=int(t.get("timestamp") or 0),
                    title=t.get("title") or "",
                    outcome=t.get("outcome") or "",
                    condition_id=t.get("conditionId") or "",
                    event_slug=t.get("eventSlug") or "",
                    slug=t.get("slug") or "",
                ))
            return out
        except Exception:  # noqa: BLE001
            return []
