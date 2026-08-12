"""Polymarket US (QCX) execution layer - verified contract (Ed25519, retail).

Auth (api.polymarket.us): Ed25519 over the EXACT string `{ms_timestamp}{METHOD}{path}`
(no body, no separators), key = base64decode(secret)[:32] as the seed. Headers:
X-PM-Access-Key, X-PM-Timestamp (same ms), X-PM-Signature (base64). Public reads are on
gateway.polymarket.us (no auth). Orders are keyed by **marketSlug**, price is a decimal
string in an Amount object, quantity is in CONTRACTS, side is intent-based.
"""

from __future__ import annotations

import base64
import json as _json
import time
from dataclasses import dataclass

API_BASE = "https://api.polymarket.us"
GATEWAY_BASE = "https://gateway.polymarket.us"

# Side intents (verified):
BUY_YES = "ORDER_INTENT_BUY_LONG"
BUY_NO = "ORDER_INTENT_BUY_SHORT"


@dataclass(frozen=True)
class PMUSMarket:
    slug: str
    yes_bid: float
    yes_ask: float
    last_px: float


@dataclass(frozen=True)
class PMUSListing:
    """One market from the `/v1/markets` listing - metadata, not a quote.

    `sides` carries what the bridge's name matcher needs: each side's full
    name ("Alexander Shevchenko"), the venue's own abbreviation ("aleshe"),
    and whether it is the LONG leg - the side the bbo's yes-price refers to
    (verified live 2026-08-12: bestAsk == the long side's quote).
    """

    slug: str
    question: str
    outcomes: tuple[str, ...]
    sides: tuple[dict, ...]      # {"name", "abbr", "long"}
    game_start: str              # ISO; the slug's own date can differ by days
    market_type: str             # sportsMarketType, e.g. "tennis_match_winner"


def _parse_listing(m: dict) -> PMUSListing:
    outcomes = m.get("outcomes") or "[]"
    if isinstance(outcomes, str):
        try:
            outcomes = _json.loads(outcomes)
        except ValueError:
            outcomes = []
    sides = tuple(
        {"name": (s.get("team") or {}).get("name") or s.get("description") or "",
         "abbr": (s.get("team") or {}).get("abbreviation") or "",
         "long": bool(s.get("long"))}
        for s in (m.get("marketSides") or []))
    return PMUSListing(
        slug=m.get("slug") or "",
        question=m.get("question") or "",
        outcomes=tuple(str(o) for o in outcomes),
        sides=sides,
        game_start=m.get("gameStartTime") or "",
        market_type=m.get("sportsMarketType") or m.get("marketType") or "",
    )


def _amt(value: float) -> dict:
    return {"value": f"{value:.2f}", "currency": "USD"}


def _px(obj) -> float:
    if isinstance(obj, dict):
        try:
            return float(obj.get("value") or 0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(obj or 0)
    except (TypeError, ValueError):
        return 0.0


class PolymarketUSClient:
    def __init__(self, key_id: str = "", private_key_path: str = "",
                 api_base: str = API_BASE, gateway_base: str = GATEWAY_BASE):
        self.key_id = key_id
        self.private_key_path = private_key_path
        self.api_base = api_base.rstrip("/")
        self.gateway_base = gateway_base.rstrip("/")
        self._pk = None

    def _private_key(self):
        if self._pk is None:
            with open(self.private_key_path, "rb") as f:
                data = f.read().strip()
            if data.startswith(b"-----BEGIN"):
                from cryptography.hazmat.primitives.serialization import load_pem_private_key
                self._pk = load_pem_private_key(data, password=None)
            else:
                from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                    Ed25519PrivateKey,
                )
                self._pk = Ed25519PrivateKey.from_private_bytes(base64.b64decode(data)[:32])
        return self._pk

    def _headers(self, method: str, path: str) -> dict:
        if not (self.key_id and self.private_key_path):
            return {}
        ts = str(int(time.time() * 1000))           # milliseconds
        message = (ts + method.upper() + path).encode()   # NO body
        sig = base64.b64encode(self._private_key().sign(message)).decode()
        return {"X-PM-Access-Key": self.key_id, "X-PM-Timestamp": ts,
                "X-PM-Signature": sig, "Content-Type": "application/json"}

    # --- read (public gateway) ---------------------------------------------
    def market(self, slug: str) -> PMUSMarket | None:
        import requests
        try:
            r = requests.get(f"{self.gateway_base}/v1/markets/{slug}/bbo", timeout=12)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            b = r.json().get("marketData") or r.json()   # bbo nests under marketData
            return PMUSMarket(slug, _px(b.get("bestBid")), _px(b.get("bestAsk")),
                              _px(b.get("lastTradePx") or b.get("currentPx")))
        except Exception:  # noqa: BLE001
            return None

    def active_slugs(self, limit: int = 1500) -> list[str]:
        """First `limit` active market slugs. Prefer `active_markets`: the listing is
        20,000+ deep with futures at the front, so a shallow read misses every daily
        game - measured 2026-08-12, offset 20,000 still hadn't reached tennis."""
        return [m.slug for m in self.active_markets(max_markets=limit)]

    def active_markets(self, max_markets: int = 40_000) -> list[PMUSListing]:
        """The FULL active listing with metadata (public, paginated).

        There is no server-side filter worth having - `category`, `tag`, `q` and
        `sportsMarketType` params are all silently ignored (probed 2026-08-12); only
        an exact `slug` lookup filters. So the one honest way to see the daily game
        markets behind 9,000+ futures is to page to the bottom: ~110 requests at 200
        a page. Callers should fetch once and match locally.
        """
        import requests
        out: list[PMUSListing] = []
        for off in range(0, max_markets, 200):
            try:
                r = requests.get(f"{self.gateway_base}/v1/markets",
                                 params={"limit": 200, "offset": off, "active": "true",
                                         "closed": "false"}, timeout=15)
                r.raise_for_status()
                data = r.json()
                mk = data if isinstance(data, list) else (data.get("markets") or data.get("data") or [])
            except Exception:  # noqa: BLE001
                break
            if not mk:
                break
            out += [_parse_listing(m) for m in mk if m.get("slug")]
            if len(mk) < 200:
                break
        return out

    # --- account (authenticated) -------------------------------------------
    def balance(self) -> dict:
        import requests
        path = "/v1/account/balances"
        r = requests.get(self.api_base + path, headers=self._headers("GET", path), timeout=15)
        r.raise_for_status()
        return r.json()

    def positions(self) -> list[dict]:
        import requests
        path = "/v1/portfolio/positions"
        r = requests.get(self.api_base + path, headers=self._headers("GET", path), timeout=15)
        r.raise_for_status()
        data = r.json()
        pos = data.get("positions", data)
        return list(pos.values()) if isinstance(pos, dict) else list(pos)

    # --- trade (authenticated) ---------------------------------------------
    def create_order(self, *, market_slug: str, intent: str, price: float, quantity: float,
                     tif: str = "TIME_IN_FORCE_GOOD_TILL_CANCEL", post_only: bool = False) -> dict:
        import requests
        path = "/v1/orders"
        body = {
            "marketSlug": market_slug,
            "type": "ORDER_TYPE_LIMIT",
            "intent": intent,                  # BUY_YES / BUY_NO
            "price": _amt(price),              # decimal string Amount, 0.01-0.99
            "quantity": quantity,              # CONTRACTS
            "tif": tif,
            "manualOrderIndicator": "AUTOMATIC",   # regulatory: bot-originated
        }
        if post_only:
            body["participateDontInitiate"] = True
        r = requests.post(self.api_base + path, headers=self._headers("POST", path),
                          data=_json.dumps(body), timeout=15)
        r.raise_for_status()
        return r.json()

    def cancel_order(self, order_id: str) -> dict:
        import requests
        path = f"/v1/order/{order_id}/cancel"
        r = requests.post(self.api_base + path, headers=self._headers("POST", path), timeout=15)
        r.raise_for_status()
        return r.json() if r.text.strip() else {}


class MockPolymarketUSClient:
    def __init__(self, markets: dict[str, PMUSMarket] | None = None, balance: float = 0.0,
                 listings: list[PMUSListing] | None = None):
        self._markets = markets or {}
        self._balance = balance
        self._listings = listings
        self.placed: list[dict] = []

    def market(self, slug: str) -> PMUSMarket | None:
        return self._markets.get(slug)

    def active_slugs(self, limit: int = 1500) -> list[str]:
        return [m.slug for m in self.active_markets()]

    def active_markets(self, max_markets: int = 40_000) -> list[PMUSListing]:
        if self._listings is not None:
            return list(self._listings)
        # Bare-quote mocks still list: slug-only entries, enough for the slug matcher.
        return [PMUSListing(slug=s, question="", outcomes=("Yes", "No"), sides=(),
                            game_start="", market_type="")
                for s in self._markets]

    def balance(self) -> dict:
        return {"buyingPower": {"value": str(self._balance), "currency": "USD"},
                "currentBalance": {"value": str(self._balance), "currency": "USD"}}

    def positions(self) -> list[dict]:
        return []

    def create_order(self, *, market_slug, intent, price, quantity, **kw) -> dict:
        order = {"id": "pmus-" + str(len(self.placed) + 1), "state": "OPEN",
                 "marketSlug": market_slug, "intent": intent, "price": price,
                 "quantity": quantity}
        self.placed.append(order)
        return order

    def cancel_order(self, order_id: str) -> dict:
        return {}
