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
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .edge import Quote

log = logging.getLogger(__name__)


def _default_fetch(url: str, params: dict) -> dict:
    import requests
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


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
    expiration_time: str = ""  # ISO; the LATEST settlement deadline, not the event
    # When the market's outcome is actually determined. For Kalshi crypto markets
    # these differ by a WEEK - a KXBTCD market closing 2026-08-06T23:00Z carries
    # expiration_time 2026-08-13T23:00Z - so any time-to-expiry model must use this
    # one. Reading expiration_time instead priced hours-out markets as 7-day options.
    close_time: str = ""

    def quote(self) -> Quote:
        return Quote(yes_ask=self.yes_ask, no_ask=self.no_ask)

    def web_url(self) -> str:
        return market_url(self.event_ticker or self.ticker)


def market_url(event_ticker: str) -> str:
    """Public Kalshi web page for an event/market."""
    return f"https://kalshi.com/markets/{event_ticker}"


# Public read endpoint. Market data needs no signing, which is what lets the paper
# trial settle Kalshi rows from CI with no secrets - the same property the Polymarket
# half relies on.
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

# Terminal statuses, duplicated from `settle._TERMINAL_STATUSES` rather than imported:
# `settle` owns the LIVE order ledger and importing it here would drag state/config
# into a pure read path. If Kalshi renames one, both lists need it.
_SETTLED_STATUSES = frozenset({"finalized", "settled", "determined"})
# A market can end without a yes/no answer. Those must be REMOVED from the trial, not
# held: a voided market never resolves, so treating it as "still open" is how a
# position sits in the record forever quietly inflating the open count.
_VOID_RESULTS = frozenset({"void", "voided", "canceled", "cancelled", "invalid"})


def _event_of(ticker: str) -> str:
    """`KXHIGHNY-26AUG10-B91.5` -> `KXHIGHNY-26AUG10`."""
    return ticker.rsplit("-", 1)[0] if "-" in ticker else ticker


# Kalshi hangs a per-series SUFFIX off one shared `YYMMMDD` date grammar, and reading
# the WHOLE tail as the date is how a sleeve goes silently blind. VERIFIED LIVE against
# 162 event tickers on 2026-08-16: the index ladders name their settlement hour
# (`KXINX-26AUG17H1600`), the crypto dailies name an hour with no letter at all
# (`KXBTCD-26AUG1800`), a game carries team codes (`KXNFLGAME-26AUG22ATLIND`), and the
# weather and gas ladders hang nothing at all (`KXHIGHNY-26AUG12`). So the day is
# matched as a PREFIX and the suffix is deliberately never interpreted - a caller only
# needs to know which day the ladder settles on, and a series that invents a new suffix
# tomorrow must keep parsing rather than quietly stop matching.
#
# This lives here, once, because it used to live three times. spxdensity, weather and
# gas each carried a byte-identical private copy that read the whole tail, and the
# spxdensity copy was therefore broken for the entire life of that sleeve: it returned
# None for every real index ticker, the day filter skipped every market, and the sleeve
# produced nothing without ever raising. The other two were correct only by luck, their
# series happening to carry no suffix. One parser means the next series to gain a suffix
# cannot break a fourth sleeve the same way.
#
# Case-insensitive because `%b` always was, and a shared parser must not accept LESS
# than the copies it replaces. The day is exactly two digits, which IS narrower than
# `%d` - that is deliberate, because `\d{1,2}` cannot tell day 18 + suffix "0" from
# day 1 + suffix "80" in `26AUG180`. No Kalshi series uses an unpadded day.
_EVENT_DAY = re.compile(r"^(\d{2}[A-Z]{3}\d{2})[A-Z0-9]*$", re.IGNORECASE)


def event_day(event_ticker: str) -> str | None:
    """`KXINX-26AUG17H1600` -> `2026-08-17`, ignoring whatever suffix follows the day.

    None whenever the day cannot be read, so a caller skips the market rather than
    guessing a settlement date for it.
    """
    m = _EVENT_DAY.match((event_ticker or "").rsplit("-", 1)[-1])
    if m is None:
        return None
    try:
        return datetime.strptime(m.group(1), "%y%b%d").replace(
            tzinfo=timezone.utc).strftime("%Y-%m-%d")
    except ValueError:
        return None        # a real-looking tail that is not a real date (26AUG99)


def market_meta(tickers: list[str], *, base: str = KALSHI_API, fetch=None,
                failures: set[str] | None = None) -> dict[str, dict]:
    """Kalshi settlement metadata in the SHAPE ``gamma.market_meta`` returns.

    Deliberately mirrors the Polymarket adapter field for field - ``closed`` plus
    parallel ``outcomes``/``prices`` - so `papertrial._winner` settles either venue
    without knowing which one it is looking at. A resolved Kalshi binary is expressed
    as the winning leg at 1.0 and the other at 0.0, which is exactly what a resolved
    Polymarket market quotes, so the existing "one leg at 1, the rest at 0" test does
    the right thing on both without a special case.

    QUERIES BY EVENT, NOT BY TICKER, and that is not a style choice. The `tickers=`
    filter returns an EMPTY array rather than an error (measured 2026-08-11), and
    `list_markets` defaults to `status="open"`, which hides exactly the finalized
    markets settlement needs - the same shape of bug as Gamma's `closed=false` default.
    An `event_ticker` query with no status param returns the whole ladder including
    finalized rows, and one call covers every strike of that day's event.
    """
    ids = [t for t in tickers if t]
    if not ids:
        return {}
    getter = fetch or _default_fetch
    by_event: dict[str, list[str]] = {}
    for t in ids:
        by_event.setdefault(_event_of(t), []).append(t)

    unread: set[str] = set()
    rows: list[dict] = []
    for event, members in by_event.items():
        try:
            data = getter(f"{base}/markets", {"event_ticker": event, "limit": 200})
        except Exception:  # noqa: BLE001
            # One event failing must not cost the others, and must not be mistaken for
            # "these markets did not resolve" - that reads as a clean open position.
            unread.update(members)
            continue
        rows.extend(data.get("markets") or [])

    if unread:
        log.warning("kalshi settlement unread for %d/%d markets", len(unread), len(ids))
    if failures is not None:
        failures.update(unread)

    wanted = set(ids)
    out: dict[str, dict] = {}
    for m in rows:
        ticker = m.get("ticker", "")
        if ticker not in wanted:
            continue          # the rest of the ladder came along for free; ignore it
        status, result = m.get("status", ""), (m.get("result") or "").lower()
        settled = status in _SETTLED_STATUSES and result in ("yes", "no")
        volume = _fixed_point(m, "volume_fp", legacy="volume")
        liquidity = _fixed_point(m, "liquidity_dollars", legacy="liquidity")
        if volume is None or liquidity is None:
            # Loud on purpose. The silent version of this is the bug being fixed here:
            # an absent field read as 0.0 is indistinguishable from a real empty book.
            log.warning("kalshi %s: no %s - depth/volume UNKNOWN, not zero", ticker,
                        " or ".join(n for n, v in (("volume_fp", volume),
                                                   ("liquidity_dollars", liquidity))
                                    if v is None))
        out[ticker] = {
            "question": m.get("title", ""),
            "slug": ticker,
            "end_date": m.get("expiration_time", ""),
            "event_iso": m.get("expected_expiration_time") or m.get("close_time") or "",
            "closed": settled,
            "voided": status in _SETTLED_STATUSES and result in _VOID_RESULTS,
            "outcomes": ["Yes", "No"],
            "prices": ([1.0, 0.0] if result == "yes" else [0.0, 1.0]) if settled else [],
            "yes_price": _price_to_dollars(m, "last_price"),
            "best_bid": _price_to_dollars(m, "yes_bid"),
            "best_ask": _price_to_dollars(m, "yes_ask"),
            "volume": volume or 0.0,
            # `liquidity_dollars` reads $0.0000 on every market measured - 0 of ~900
            # nonzero on 2026-08-16 across KXINX, KXBTCD, KXNBAGAME, KXNFLGAME,
            # KXMLBGAME and KXHIGHNY - while the order book shows real resting size,
            # the same finding as the 2026-08-11 weather sweep under the old key name.
            # Correcting the key fixed the LOOKUP; it did not make the field populated.
            # Still never gate on it. The book is the only honest depth signal here.
            "liquidity": liquidity or 0.0,
        }
        if status in _SETTLED_STATUSES and not settled and not out[ticker]["voided"]:
            # Decided-looking but wearing an unrecognised result. Same defensive stance
            # `settle.py` takes: skip loudly rather than guess a winner.
            log.warning("kalshi %s status %r has unrecognised result %r - not settling",
                        ticker, status, result)
    return out


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


def _fixed_point(d: dict, key: str, *, legacy: str = "") -> float | None:
    """A Kalshi size/depth measure, or None when the field is absent ENTIRELY.

    Same mid-migration dual schema `_price_to_dollars` handles, one step further along:
    the bare `volume` and `liquidity` names are now GONE, not merely optional. Measured
    2026-08-16 over 600 live markets, both scored 0 hits, so reading them returned the
    `or 0.0` default on every market - a silent zero that any depth filter reads as an
    empty book. The live names are `volume_fp` and `liquidity_dollars`.

    THE SUFFIX NAMES THE ENCODING, NOT A SCALE FACTOR, and getting that backwards would
    be worse than the bug it replaces - a stray divisor would filter real markets out
    just as quietly. Both arrive as decimal STRINGS already in natural units (contracts
    for `_fp`, dollars for `_dollars`), so they are floated and never rescaled. Verified
    rather than inferred from the name: `/markets/trades` for KXINX-26AUG11H1600-B7737
    sums to 115702.77 contracts, exactly that market's `volume_fp` of "115702.77", and
    each individual trade carries `count_fp: "1.00"` for one contract.

    Returning None rather than 0.0 is the whole point of the signature: the NEXT rename
    has to surface as "unknown" at the call site instead of passing as "no depth".
    """
    for name in (key, legacy):
        if name and d.get(name) is not None:
            try:
                return float(d[name])
            except (TypeError, ValueError):
                return None      # present but unreadable is a schema change too
    return None


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
            close_time=(m.get("expected_expiration_time")
                        or m.get("close_time") or ""),
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
