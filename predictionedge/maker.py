"""Kalshi Liquidity Incentive Program (LIP) quoting engine — FRAMEWORK.

Strategy: rest two-sided quotes in quiet, slow-moving Kalshi markets to earn
daily LIP reward pools (program pays through 2026-09-01; open to regular
members, no MM agreement). Scoring is second-by-second snapshots weighted by
size and proximity to top-of-book, so the bot's job is to keep qualifying
quotes alive as much of the day as possible while never getting run over.

Economics (verified 2026-07):
- Taker fee: 0.07 * C * (1-C) per contract (see fees.py). Maker fee: 25% of
  the taker fee — quoting is NOT free anymore, so spread capture must clear it.
- Reward pools: $10-$1,000/day per market; size targets 100-20,000 contracts.
  At our bankroll only ~100-contract quotes in cheap (<= ~35c) markets qualify.
- The real cost is adverse selection: a stale quote in a news-driven market is
  a donation. We only quote markets whose drivers move slowly (weather highs,
  econ prints between releases) and we PULL quotes around scheduled releases.

Wiring status: pure logic (pricing, diffing, inventory skew) is implemented
and unit-testable; everything touching the live API is a stub marked TODO.
The venue surface this needs beyond LiveKalshiTradingClient is defined in
MakerVenue — implement those three methods on the client to go live.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Protocol

from .config import _load_dotenv, _truthy
from .fees import fee_per_contract

log = logging.getLogger(__name__)

CONTRACT_MAX_PRICE = 0.99
CONTRACT_MIN_PRICE = 0.01


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class MakerConfig:
    """Tunables for the quoting engine. Wire into Config.from_env later."""

    # Which markets to quote. Empty allowlist = quote nothing (safe default);
    # series prefixes like "KXHIGH" (weather highs) go here after vetting.
    series_allowlist: tuple[str, ...] = ()

    # Quote shape.
    quote_size: int = 100            # contracts per side; LIP minimum target tier
    half_spread_cents: int = 3       # distance from our fair mid, each side
    min_edge_after_fees: float = 0.005  # $/contract our spread must clear vs maker fee

    # Requote discipline: don't churn the book (rate limits + LIP uptime both
    # favor staying put). Only move when the market has actually moved.
    requote_threshold_cents: int = 2
    min_requote_interval_s: float = 5.0

    # Inventory / risk.
    max_position_per_market: int = 200   # contracts, either side
    max_total_exposure: float = 400.0    # $ cost basis across all quoted markets
    inventory_skew_cents_per_100: int = 1  # shade quotes against our inventory

    # Adverse-selection guards.
    pull_minutes_before_event: int = 30  # scheduled data releases (NWS, BLS...)
    pull_minutes_after_event: int = 15
    max_book_staleness_s: float = 30.0   # no fresh book -> pull quotes

    dry_run: bool = True  # log intended orders instead of placing them

    @classmethod
    def from_env(cls) -> "MakerConfig":
        """Load from .env / environment, mirroring Config.from_env()'s pattern.

        Series allowlist starts EMPTY on purpose (see field docstring) — quoting
        a market means manually vetting it's LIP-eligible and slow-moving first,
        so PE_MAKER_SERIES must be set explicitly; there is no safe default set.
        """
        _load_dotenv()
        e = os.environ
        # slots=True replaces class attrs with member descriptors, so
        # cls.field doesn't yield the default — pull defaults from the
        # dataclass field metadata instead.
        d = {fld.name: fld.default for fld in fields(cls)}

        def f(name: str, default: float) -> float:
            v = e.get(name)
            return float(v) if v else default

        return cls(
            series_allowlist=tuple(s.strip() for s in
                                   e.get("PE_MAKER_SERIES", "").split(",") if s.strip()),
            quote_size=int(f("PE_MAKER_QUOTE_SIZE", d["quote_size"])),
            half_spread_cents=int(f("PE_MAKER_HALF_SPREAD_CENTS", d["half_spread_cents"])),
            min_edge_after_fees=f("PE_MAKER_MIN_EDGE", d["min_edge_after_fees"]),
            requote_threshold_cents=int(f("PE_MAKER_REQUOTE_THRESHOLD_CENTS",
                                          d["requote_threshold_cents"])),
            min_requote_interval_s=f("PE_MAKER_MIN_REQUOTE_INTERVAL_S",
                                     d["min_requote_interval_s"]),
            max_position_per_market=int(f("PE_MAKER_MAX_POSITION",
                                          d["max_position_per_market"])),
            max_total_exposure=f("PE_MAKER_MAX_EXPOSURE", d["max_total_exposure"]),
            inventory_skew_cents_per_100=int(f("PE_MAKER_SKEW_CENTS_PER_100",
                                               d["inventory_skew_cents_per_100"])),
            pull_minutes_before_event=int(f("PE_MAKER_PULL_BEFORE_MIN",
                                            d["pull_minutes_before_event"])),
            pull_minutes_after_event=int(f("PE_MAKER_PULL_AFTER_MIN",
                                           d["pull_minutes_after_event"])),
            max_book_staleness_s=f("PE_MAKER_MAX_BOOK_STALENESS_S", d["max_book_staleness_s"]),
            dry_run=_truthy(e.get("PE_MAKER_DRY_RUN"), d["dry_run"]),
        )


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass(slots=True)
class BookTop:
    """Best bid/ask (YES side, dollars) plus when we fetched it."""

    ticker: str
    bid: float | None
    ask: float | None
    fetched_at: datetime

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2


@dataclass(slots=True)
class RestingOrder:
    order_id: str
    ticker: str
    side: str          # "yes" | "no"
    price: float       # dollars
    remaining: int     # contracts


@dataclass(frozen=True, slots=True)
class QuotePlan:
    """Desired state for one market: a YES bid and a NO bid (two-sided)."""

    ticker: str
    yes_price: float
    no_price: float
    size: int

    def sides(self) -> tuple[tuple[str, float], tuple[str, float]]:
        return ("yes", self.yes_price), ("no", self.no_price)


@dataclass(slots=True)
class QuoteAction:
    """One concrete step the engine wants to take this cycle."""

    kind: str                       # "place" | "cancel" | "hold" | "pull_all"
    ticker: str
    side: str = ""
    price: float = 0.0
    size: int = 0
    reason: str = ""


# --------------------------------------------------------------------------
# Venue surface
# --------------------------------------------------------------------------

class MakerVenue(Protocol):
    """What the engine needs from Kalshi beyond what kalshi.py already has."""

    def orderbook(self, ticker: str) -> BookTop: ...
    def resting_orders(self) -> list[RestingOrder]: ...
    def lip_markets(self, series_allowlist: tuple[str, ...]) -> list[str]: ...
    def positions(self) -> dict[str, int]: ...
    def place(self, ticker: str, side: str, price: float, size: int) -> str: ...
    def cancel(self, order_id: str) -> None: ...


class KalshiMakerVenue:
    """Adapts LiveKalshiTradingClient to MakerVenue.

    There is no official LIP-eligible-markets API (verified 2026-07 — the
    program page lists pools but not a machine-readable feed), so
    ``lip_markets`` treats the manually-vetted ``series_allowlist`` as the
    ground truth and simply lists currently-open tickers in those series.
    Re-vet the allowlist weekly by hand against the LIP program page.
    """

    def __init__(self, client):
        self.client = client  # LiveKalshiTradingClient

    def orderbook(self, ticker: str) -> BookTop:
        book = self.client.orderbook(ticker)
        yes_levels = book.get("yes") or []
        no_levels = book.get("no") or []
        # Best YES ask == 1 - best NO bid (complementary sides of one market).
        best_no_bid = max((lvl[0] for lvl in no_levels), default=None)
        best_yes_bid = max((lvl[0] for lvl in yes_levels), default=None)
        bid = (best_yes_bid / 100) if best_yes_bid is not None else None
        ask = (1 - best_no_bid / 100) if best_no_bid is not None else None
        return BookTop(ticker, bid, ask, utcnow())

    def resting_orders(self) -> list[RestingOrder]:
        raw = self.client.resting_orders()
        out = []
        for o in raw:
            price = o.get("yes_price_dollars") or o.get("no_price_dollars")
            if price is None:
                cents = o.get("yes_price") or o.get("no_price") or 0
                price = cents / 100
            out.append(RestingOrder(
                order_id=o.get("order_id", ""), ticker=o.get("ticker", ""),
                side=o.get("side", ""), price=float(price),
                remaining=int(o.get("remaining_count", o.get("count", 0))),
            ))
        return out

    def lip_markets(self, series_allowlist: tuple[str, ...]) -> list[str]:
        tickers: list[str] = []
        for series in series_allowlist:
            for m in self.client.list_markets(series_ticker=series, status="open"):
                tickers.append(m.ticker)
        return tickers

    def positions(self) -> dict[str, int]:
        """Ticker -> signed net YES contracts (negative = net NO)."""
        out: dict[str, int] = {}
        for p in self.client.positions():
            qty = int(p.get("position", 0))  # VERIFY sign convention against live API
            out[p.get("ticker", "")] = qty
        return out

    def place(self, ticker: str, side: str, price: float, size: int) -> str:
        resp = self.client.create_order(
            ticker=ticker, side=side, action="buy", count=size,
            price_cents=round(price * 100),
            client_order_id=f"maker-{ticker}-{side}-{int(utcnow().timestamp())}",
            post_only=True,
        )
        return (resp.get("order") or {}).get("order_id") or resp.get("order_id", "")

    def cancel(self, order_id: str) -> None:
        self.client.cancel_order(order_id)


class MockMakerVenue:
    """Canned venue for dry-run/testing without any network or credentials."""

    def __init__(self, books: dict[str, BookTop] | None = None):
        self.books = books or {}
        self.resting: list[RestingOrder] = []
        self._inventory: dict[str, int] = {}
        self.placed: list[tuple[str, str, float, int]] = []
        self.cancelled: list[str] = []

    def orderbook(self, ticker: str) -> BookTop:
        return self.books.get(ticker, BookTop(ticker, None, None, utcnow()))

    def resting_orders(self) -> list[RestingOrder]:
        return list(self.resting)

    def lip_markets(self, series_allowlist: tuple[str, ...]) -> list[str]:
        return [t for t in self.books if any(t.startswith(s) for s in series_allowlist)]

    def positions(self) -> dict[str, int]:
        return dict(self._inventory)

    def place(self, ticker: str, side: str, price: float, size: int) -> str:
        self.placed.append((ticker, side, price, size))
        oid = f"mock-{len(self.placed)}"
        self.resting.append(RestingOrder(oid, ticker, side, price, size))
        return oid

    def cancel(self, order_id: str) -> None:
        self.cancelled.append(order_id)
        self.resting = [o for o in self.resting if o.order_id != order_id]


# --------------------------------------------------------------------------
# Pure logic — implemented, unit-testable without any API
# --------------------------------------------------------------------------

def spread_clears_fees(cfg: MakerConfig, mid: float) -> bool:
    """Would earning our half-spread on a round trip beat the maker fees?

    Conservative: assumes both sides eventually fill (worst case for fees) and
    ignores LIP rewards entirely, so LIP income is pure upside on top.
    """
    half_spread = cfg.half_spread_cents / 100
    fee = fee_per_contract(mid, maker=True)
    return half_spread - fee >= cfg.min_edge_after_fees


def desired_quotes(
    cfg: MakerConfig, book: BookTop, inventory: int
) -> QuotePlan | None:
    """Compute the two-sided quote for one market, or None to stay out.

    inventory: net YES contracts we hold (negative = net NO). Quotes shade
    away from our inventory so fills mean-revert our position.
    """
    mid = book.mid
    if mid is None or not spread_clears_fees(cfg, mid):
        return None

    skew = (inventory / 100) * (cfg.inventory_skew_cents_per_100 / 100)
    center = mid - skew  # long YES -> quote lower -> sell YES / buy NO fills first

    half = cfg.half_spread_cents / 100
    yes_price = round(center - half, 2)
    no_price = round(1 - (center + half), 2)  # NO bid at p == YES ask at 1-p

    if not (CONTRACT_MIN_PRICE <= yes_price <= CONTRACT_MAX_PRICE):
        return None
    if not (CONTRACT_MIN_PRICE <= no_price <= CONTRACT_MAX_PRICE):
        return None
    return QuotePlan(book.ticker, yes_price, no_price, cfg.quote_size)


def diff_quotes(
    cfg: MakerConfig, plan: QuotePlan | None, resting: list[RestingOrder]
) -> list[QuoteAction]:
    """Cancel/replace only when the desired quote moved past the threshold.

    LIP scores uptime, so an unnecessary cancel is a direct rewards loss;
    a quote off by 1c usually still scores (proximity-weighted).
    """
    actions: list[QuoteAction] = []
    threshold = cfg.requote_threshold_cents / 100

    if plan is None:
        for o in resting:
            actions.append(QuoteAction("cancel", o.ticker, o.side, o.price,
                                       o.remaining, reason="no quote wanted"))
        return actions

    wanted = dict(plan.sides())
    for o in resting:
        want = wanted.pop(o.side, None)
        if want is None or abs(o.price - want) > threshold:
            actions.append(QuoteAction("cancel", o.ticker, o.side, o.price,
                                       o.remaining, reason="stale price"))
            if want is not None:
                actions.append(QuoteAction("place", plan.ticker, o.side, want,
                                           plan.size, reason="requote"))
        else:
            actions.append(QuoteAction("hold", o.ticker, o.side, o.price,
                                       o.remaining, reason="within threshold"))
    for side, price in wanted.items():
        actions.append(QuoteAction("place", plan.ticker, side, price,
                                   plan.size, reason="new quote"))
    return actions


def should_pull(
    cfg: MakerConfig,
    book: BookTop,
    now: datetime,
    next_event: datetime | None,
) -> str | None:
    """Return a reason to pull all quotes in this market, or None to stay."""
    age = (now - book.fetched_at).total_seconds()
    if age > cfg.max_book_staleness_s:
        return f"book stale ({age:.0f}s)"
    if next_event is not None:
        dt_min = (next_event - now).total_seconds() / 60
        if -cfg.pull_minutes_after_event <= dt_min <= cfg.pull_minutes_before_event:
            return f"scheduled release in {dt_min:.0f}min window"
    return None


# --------------------------------------------------------------------------
# Engine — orchestration skeleton
# --------------------------------------------------------------------------

class MakerEngine:
    """One cycle: select markets -> plan quotes -> diff -> act.

    Not yet wired: a per-series release calendar (NWS forecast cycles,
    BLS/CPI timestamps) feeding ``should_pull``'s ``next_event`` — until that
    exists, ``next_event`` is always None (no scheduled-release pulls; only
    the staleness guard is live). Also not wired: persisting fills tagged
    strategy="maker" into StateStore (needs a `strategy` column WS-F adds).
    Wire both in before flipping ``dry_run=False`` for real.
    """

    def __init__(self, cfg: MakerConfig, venue: MakerVenue):
        self.cfg = cfg
        self.venue = venue
        self._last_requote: dict[str, datetime] = {}

    def select_markets(self) -> list[str]:
        """LIP-eligible ∩ series allowlist ∩ our size/price constraints."""
        if not self.cfg.series_allowlist:
            return []
        tickers = self.venue.lip_markets(self.cfg.series_allowlist)
        out = []
        for t in tickers:
            book = self.venue.orderbook(t)
            if book.mid is None:
                continue
            cost = book.mid * self.cfg.quote_size
            if cost > self.cfg.max_total_exposure:
                log.debug("skip %s: quote cost %.2f exceeds exposure cap", t, cost)
                continue
            out.append(t)
        return out

    def net_inventory(self, ticker: str) -> int:
        """Net YES contracts held in this market."""
        return self.venue.positions().get(ticker, 0)

    def event_window(self, ticker: str) -> datetime | None:
        """Next scheduled release for this market's series, if known.

        Placeholder: no calendar wired yet, so quotes never pull for a
        scheduled release — only for book staleness. Fill in a real
        per-series calendar before trading anything news-sensitive.
        """
        return None

    def cycle(self, now: datetime | None = None) -> list[QuoteAction]:
        """Plan and (unless dry_run) execute one full quoting pass."""
        now = now or utcnow()
        all_actions: list[QuoteAction] = []
        markets = self.select_markets()
        resting_by_ticker: dict[str, list[RestingOrder]] = {}
        for o in self.venue.resting_orders():
            resting_by_ticker.setdefault(o.ticker, []).append(o)

        for ticker in markets:
            book = self.venue.orderbook(ticker)
            reason = should_pull(self.cfg, book, now, self.event_window(ticker))
            plan = None if reason else desired_quotes(
                self.cfg, book, self.net_inventory(ticker)
            )
            if reason:
                log.info("pull %s: %s", ticker, reason)

            last = self._last_requote.get(ticker)
            if (last is not None
                    and (now - last).total_seconds() < self.cfg.min_requote_interval_s):
                continue  # too soon to touch this market again

            actions = diff_quotes(self.cfg, plan, resting_by_ticker.get(ticker, []))
            touched = False
            for action in actions:
                if action.kind == "hold":
                    continue
                touched = True
                if self.cfg.dry_run:
                    log.info("DRY-RUN maker %s %s %s @ %.2f x%d (%s)",
                             action.kind, action.ticker, action.side,
                             action.price, action.size, action.reason)
                    continue
                if action.kind == "cancel":
                    match = next((o for o in resting_by_ticker.get(ticker, [])
                                 if o.side == action.side), None)
                    if match:
                        self.venue.cancel(match.order_id)
                elif action.kind == "place":
                    self.venue.place(action.ticker, action.side, action.price, action.size)
            if touched:
                self._last_requote[ticker] = now
            all_actions.extend(actions)

        # Markets no longer selected but still resting elsewhere: pull them.
        for ticker, orders in resting_by_ticker.items():
            if ticker in markets:
                continue
            for o in orders:
                action = QuoteAction("cancel", ticker, o.side, o.price, o.remaining,
                                     reason="market dropped from selection")
                if self.cfg.dry_run:
                    log.info("DRY-RUN maker cancel %s %s (dropped)", ticker, o.side)
                else:
                    self.venue.cancel(o.order_id)
                all_actions.append(action)
        return all_actions


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
