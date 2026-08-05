"""Kalshi market-data client - READ ONLY.

This client fetches markets and order books. It deliberately does not place,
modify, or cancel orders: live execution is a separate, gated phase. The RSA-PSS
request signing needed for authenticated endpoints is scaffolded here so the live
reader works, but no order endpoints are exposed.

Kalshi quotes prices in whole cents (1..99); we expose them in dollars (0.01..0.99).
Always confirm the base URL against https://docs.kalshi.com.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Protocol

from .edge import Quote


@dataclass(frozen=True)
class KalshiMarket:
    ticker: str
    title: str
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    status: str = ""        # "open" | "closed" | "settled" | ...
    result: str = ""        # "yes" | "no" once settled
    yes_sub_title: str = ""  # the YES team name (authoritative - trust over the ticker)
    no_sub_title: str = ""   # the NO team name
    event_ticker: str = ""   # parent event (used to build the web URL)
    # Scalar/threshold markets (e.g. crypto "price >= X at T"):
    strike_type: str = ""    # "greater" | "less" | "between"
    floor_strike: float | None = None
    cap_strike: float | None = None
    expiration_time: str = ""  # ISO; for time-to-expiry in the crypto model

    def quote(self) -> Quote:
        return Quote(yes_ask=self.yes_ask, no_ask=self.no_ask)

    def web_url(self) -> str:
        return market_url(self.event_ticker or self.ticker)


def market_url(event_ticker: str) -> str:
    """Public Kalshi web page for an event/market."""
    return f"https://kalshi.com/markets/{event_ticker}"


def _price_to_dollars(d: dict, key: str) -> float:
    """Read a Kalshi price field as dollars, handling the cents/dollars dual schema.

    Kalshi is mid-migration: a field may arrive as integer CENTS (``yes_bid``) or as
    a fixed-point dollar STRING (``yes_bid_dollars``). Prefer the dollar field if
    present, else treat the bare field as cents.
    """
    dollars = d.get(key + "_dollars")
    if dollars is not None:
        try:
            return float(dollars)
        except (TypeError, ValueError):
            pass
    cents = d.get(key)
    return (cents / 100.0) if cents is not None else 0.0


class KalshiClient(Protocol):
    def market(self, ticker: str) -> KalshiMarket | None:
        ...


class MockKalshiClient:
    """Canned markets so the full pipeline runs with no network or credentials."""

    def __init__(self) -> None:
        self._markets = {
            # YES underpriced (~0.46) vs sportsbook consensus (~0.54): a YES edge.
            "KXNBA-LALBOS": KalshiMarket("KXNBA-LALBOS", "Lakers beat Celtics",
                                         yes_bid=0.44, yes_ask=0.46, no_bid=0.54, no_ask=0.56),
            # Priced in line with consensus (~0.72): correctly rejected, no edge.
            "KXFED-CUT": KalshiMarket("KXFED-CUT", "Fed cuts at next meeting",
                                      yes_bid=0.69, yes_ask=0.71, no_bid=0.29, no_ask=0.31),
        }

    def market(self, ticker: str) -> KalshiMarket | None:
        return self._markets.get(ticker)


class LiveKalshiReadOnlyClient:
    """Read-only REST client. Lazily imports requests / cryptography."""

    def __init__(self, base_url: str, key_id: str = "", private_key_path: str = ""):
        self.base_url = base_url.rstrip("/")
        self.key_id = key_id
        self.private_key_path = private_key_path
        self._pk = None

    def _private_key(self):
        if self._pk is None:
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            with open(self.private_key_path, "rb") as f:
                self._pk = load_pem_private_key(f.read(), password=None)
        return self._pk

    def _headers(self, method: str, path: str) -> dict:
        if not (self.key_id and self.private_key_path):
            return {}
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        ts = str(int(time.time() * 1000))
        message = (ts + method.upper() + path).encode()
        signature = self._private_key().sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    @staticmethod
    def _parse_market(m: dict) -> KalshiMarket:
        return KalshiMarket(
            ticker=m.get("ticker", ""),
            title=m.get("title", ""),
            yes_bid=_price_to_dollars(m, "yes_bid"),
            yes_ask=_price_to_dollars(m, "yes_ask"),
            no_bid=_price_to_dollars(m, "no_bid"),
            no_ask=_price_to_dollars(m, "no_ask"),
            status=m.get("status", ""),
            result=m.get("result", ""),
            yes_sub_title=m.get("yes_sub_title", ""),
            no_sub_title=m.get("no_sub_title", ""),
            event_ticker=m.get("event_ticker", ""),
            strike_type=m.get("strike_type", ""),
            floor_strike=m.get("floor_strike"),
            cap_strike=m.get("cap_strike"),
            expiration_time=m.get("expiration_time", ""),
        )

    def market(self, ticker: str) -> KalshiMarket | None:
        import requests

        # Market data is public - no signing required (verified contract).
        resp = requests.get(self.base_url + f"/markets/{ticker}", timeout=15)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        m = resp.json().get("market")
        return self._parse_market(m) if m else None

    def list_markets(self, *, series_ticker: str | None = None,
                     event_ticker: str | None = None, status: str = "open",
                     limit: int = 200, cursor: str | None = None) -> list[KalshiMarket]:
        """List markets (public). Filter by series_ticker for a sport, status=open."""
        import requests

        params: dict = {"limit": min(limit, 1000), "status": status}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(self.base_url + "/markets", params=params, timeout=15)
        resp.raise_for_status()
        return [self._parse_market(m) for m in resp.json().get("markets", [])]

    def orderbook(self, ticker: str, depth: int = 1) -> dict:
        """Raw top-of-book (public, no signing). ``depth=1`` is enough for BBO.

        Response shape (VERIFY against https://docs.kalshi.com before trusting):
        ``{"orderbook": {"yes": [[price_cents, size], ...], "no": [[price_cents, size], ...]}}``
        with each side's price levels typically sorted best-first.
        """
        import requests

        resp = requests.get(self.base_url + f"/markets/{ticker}/orderbook",
                            params={"depth": depth}, timeout=15)
        resp.raise_for_status()
        return resp.json().get("orderbook", {})


class LiveKalshiTradingClient(LiveKalshiReadOnlyClient):
    """Read-only client PLUS order write paths. Constructed only when live_trading=True.

    VERIFY before any real-money run (the API-contract research workflow hit a
    session limit, so these field names follow known Kalshi conventions and must be
    confirmed against https://docs.kalshi.com):
      - order body fields (action/side/count/type/yes_price/no_price/post_only)
      - that limit price is in whole cents on the chosen side
      - balance/positions response shapes
    Demo base URL (cfg.use_demo=True) is the place to confirm all of this safely.
    """

    def create_order(
        self, *, ticker: str, side: str, action: str, count: int, price_cents: int,
        client_order_id: str, post_only: bool = True,
        time_in_force: str = "good_till_canceled",
    ) -> dict:
        import requests

        path = "/trade-api/v2/portfolio/orders"
        body = {
            "ticker": ticker,
            "action": action,            # "buy" | "sell"
            "side": side,                # "yes" | "no"
            "count": int(count),
            "type": "limit",
            "time_in_force": time_in_force,  # good_till_canceled rests as maker
            "client_order_id": client_order_id,
            "post_only": post_only,      # HARD maker guarantee: a crossing order is cancelled
        }
        # Limit price is set on the side being bought, in whole cents (1-99).
        body["yes_price" if side == "yes" else "no_price"] = int(price_cents)

        resp = requests.post(
            self.base_url + "/portfolio/orders",
            headers={**self._headers("POST", path), "Content-Type": "application/json"},
            json=body, timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def cancel_order(self, order_id: str) -> dict:
        import requests

        # v2 mutation path: the classic DELETE /portfolio/orders/{id} is being
        # deprecated (Kalshi changelog, ~2026-06-18..06-25). The v2 events path is
        # the supported replacement. VERIFY against demo before a real-money run.
        path = f"/trade-api/v2/portfolio/events/orders/{order_id}"
        resp = requests.delete(
            self.base_url + f"/portfolio/events/orders/{order_id}",
            headers=self._headers("DELETE", path), timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def balance_dollars(self) -> float:
        import requests

        path = "/trade-api/v2/portfolio/balance"
        resp = requests.get(self.base_url + "/portfolio/balance",
                            headers=self._headers("GET", path), timeout=15)
        resp.raise_for_status()
        d = resp.json()
        if d.get("balance_dollars") is not None:  # fixed-point dual-schema field
            try:
                return float(d["balance_dollars"])
            except (TypeError, ValueError):
                pass
        return d.get("balance", 0) / 100.0

    def positions(self) -> list[dict]:
        import requests

        path = "/trade-api/v2/portfolio/positions"
        resp = requests.get(self.base_url + "/portfolio/positions",
                            headers=self._headers("GET", path), timeout=15)
        resp.raise_for_status()
        return resp.json().get("market_positions", [])

    def resting_orders(self, ticker: str | None = None) -> list[dict]:
        """Orders currently resting on the book (our open maker quotes).

        VERIFY the ``status=resting`` filter value against the live API before
        depending on it; falls back to client-side filtering on ``status`` if
        the server ignores the param.
        """
        import requests

        path = "/trade-api/v2/portfolio/orders"
        params: dict = {"status": "resting"}
        if ticker:
            params["ticker"] = ticker
        resp = requests.get(self.base_url + "/portfolio/orders",
                            headers=self._headers("GET", path), params=params, timeout=15)
        resp.raise_for_status()
        orders = resp.json().get("orders", [])
        return [o for o in orders if o.get("status", "resting") == "resting"]
