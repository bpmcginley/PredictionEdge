"""Sportsbook odds ingestion and consensus fair value.

Sharp books are the best public probability estimate that exists; we de-vig each
book to a fair probability and average across books to get a consensus we fade
Kalshi against. Ships a mock provider and a thin client for The Odds API
(https://the-odds-api.com), which aggregates many books behind one key.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .devig import devig, fair_all, implied_from_decimal

# Process-wide odds cache so the dashboard and the trading engine SHARE one fetch.
# The Odds API free tier is 500 credits/month (cost = sports x regions per call) and it
# RESETS MONTHLY - it was never a one-time allowance. A 30-min TTL burns 48/day on a
# single sport, which is what drained it before: ~16/day is the whole budget. At 2h one
# sport fits (~360/month); at 4h, two. For closing lines you do not need polling at all -
# one call near event start is both cheaper and the actual signal.
_ODDS_CACHE: dict = {}
_ODDS_TTL = 7200.0


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO8601 timestamp (handles a trailing 'Z'); None on failure."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class BookOdds:
    book: str
    # decimal odds for each of the two outcomes, in the event's outcome order
    outcomes: tuple[float, float]


@dataclass(frozen=True)
class SportEvent:
    event_id: str
    label: str
    outcome_names: tuple[str, str]
    books: list[BookOdds]
    start: datetime | None = None   # event commence time, for cross-venue matching


class OddsProvider(Protocol):
    def events(self) -> list[SportEvent]:
        ...


def consensus_fair_prob(event: SportEvent, method: str = "multiplicative") -> float | None:
    """Average de-vigged probability of the *first* outcome across all books."""
    probs: list[float] = []
    for b in event.books:
        try:
            implied = [implied_from_decimal(o) for o in b.outcomes]
            probs.append(devig(implied, method)[0])
        except ValueError:
            continue
    if not probs:
        return None
    return sum(probs) / len(probs)


def consensus_fair_all(event: SportEvent) -> dict[str, float] | None:
    """Per-method consensus fair P(first outcome): the A/B instrumentation.

    Same book-averaging as `consensus_fair_prob`, computed under multiplicative,
    power AND Shin at once, so every sports record can carry all three fair
    values alongside the operative one and the paper trial can later score which
    method would have done better without a second trial. A book that any method
    refuses (incomplete market) is dropped from all three - the comparison is
    only honest if every method saw the same books.
    """
    acc: dict[str, list[float]] = {"fair_mult": [], "fair_power": [], "fair_shin": []}
    for b in event.books:
        try:
            implied = [implied_from_decimal(o) for o in b.outcomes]
            per_book = fair_all(implied)
        except ValueError:
            continue
        for key, p in per_book.items():
            acc[key].append(p)
    if not acc["fair_mult"]:
        return None
    return {key: sum(v) / len(v) for key, v in acc.items()}


class MockOddsProvider:
    """Canned two-way events for offline runs and tests."""

    def events(self) -> list[SportEvent]:
        return [
            SportEvent(
                event_id="evt-lakers-celtics",
                label="Lakers vs Celtics",
                outcome_names=("Lakers", "Celtics"),
                books=[
                    BookOdds("pinnacle", (1.80, 2.10)),
                    BookOdds("draftkings", (1.83, 2.05)),
                    BookOdds("fanduel", (1.78, 2.12)),
                ],
            ),
            SportEvent(
                event_id="evt-fed-cut",
                label="Fed cuts at next meeting",
                outcome_names=("Cut", "Hold"),
                books=[
                    BookOdds("book-a", (1.35, 3.40)),
                    BookOdds("book-b", (1.33, 3.55)),
                ],
            ),
        ]


class TheOddsApiProvider:
    """Live provider backed by The Odds API, across one or more sports.

    Each sport is one API call (one credit) per refresh; events are merged. A failing
    or out-of-season sport is skipped, not fatal.
    """

    BASE = "https://api.the-odds-api.com/v4"

    # "eu", NOT "us". Pinnacle - the sharpest public book and the whole point of a
    # de-vig reference - is an EU-region book on the free tier, so the old "us" default
    # silently excluded the one line worth reading. Measured live: Pinnacle quoted a
    # 1.89% two-way overround against soft books' 4-6%, de-vigging to 0.6457 where the
    # 22-book consensus sat at 0.6411.
    def __init__(self, api_key: str, sports=("basketball_nba",), regions: str = "eu",
                 ttl: float = _ODDS_TTL):
        self.api_key = api_key
        self.sports = (sports,) if isinstance(sports, str) else tuple(sports)
        self.regions = regions
        self.ttl = ttl

    def events(self) -> list[SportEvent]:
        import requests  # lazy: not needed in mock mode

        key = (self.sports, self.regions)
        hit = _ODDS_CACHE.get(key)
        now = time.time()
        if hit is not None and now - hit[0] < self.ttl:
            return hit[1]

        out: list[SportEvent] = []
        for sport in self.sports:
            try:
                resp = requests.get(
                    f"{self.BASE}/sports/{sport}/odds",
                    params={"apiKey": self.api_key, "regions": self.regions,
                            "markets": "h2h", "oddsFormat": "decimal"},
                    timeout=15,
                )
                resp.raise_for_status()
                rows = resp.json()
            except Exception:  # noqa: BLE001 - one bad/off-season sport must not sink the scan
                continue
            for ev in rows:
                event = _parse_odds_event(ev)
                if event is not None:
                    out.append(event)
        _ODDS_CACHE[key] = (now, out)
        return out


def _parse_odds_event(ev: dict) -> SportEvent | None:
    names = (ev.get("home_team"), ev.get("away_team"))
    if not all(names):
        return None
    books: list[BookOdds] = []
    for bk in ev.get("bookmakers", []):
        price = {o["name"]: o["price"]
                 for m in bk.get("markets", []) if m["key"] == "h2h"
                 for o in m["outcomes"]}
        # A three-way market (soccer: home/draw/away) cannot be carried by SportEvent's
        # two-outcome shape. Taking the two named teams and discarding the Draw is what
        # produced the +11pt favourite bias - the survivors sum to less than 1 and the
        # de-vig then inflates them. `devig.require_complete_market` is the backstop;
        # this is the cause. Skip the book rather than refactor to three-way: the whole
        # EPL family is 18 markets on ~12.9k lifetime contracts, not worth the surface.
        if len(price) > 2:
            continue
        if names[0] in price and names[1] in price:
            books.append(BookOdds(bk["key"], (price[names[0]], price[names[1]])))
    if not books:
        return None
    return SportEvent(ev["id"], f"{names[0]} vs {names[1]}", names, books,
                      start=_parse_ts(ev.get("commence_time")))
