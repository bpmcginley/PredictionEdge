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
        """All active market slugs (public, paginated) - for the intl->PM-US matcher."""
        import requests
        out: list[str] = []
        for off in range(0, limit, 200):
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
            out += [m.get("slug") for m in mk if m.get("slug")]
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
    def __init__(self, markets: dict[str, PMUSMarket] | None = None, balance: float = 0.0):
        self._markets = markets or {}
        self._balance = balance
        self.placed: list[dict] = []

    def market(self, slug: str) -> PMUSMarket | None:
        return self._markets.get(slug)

    def active_slugs(self, limit: int = 1500) -> list[str]:
        return list(self._markets.keys())

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
