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

from .devig import devig, implied_from_decimal

# Process-wide odds cache so the dashboard and the trading engine SHARE one fetch.
# The Odds API free tier is only 500 credits/month (1 credit per sport per call), so
# polling every cycle exhausts it in hours - this is the difference between viable and
# dead. Default 30 min; sports lines don't move enough at our edge threshold to matter.
_ODDS_CACHE: dict = {}
_ODDS_TTL = 1800.0


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

    def __init__(self, api_key: str, sports=("basketball_nba",), regions: str = "us",
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
        if names[0] in price and names[1] in price:
            books.append(BookOdds(bk["key"], (price[names[0]], price[names[1]])))
    if not books:
        return None
    return SportEvent(ev["id"], f"{names[0]} vs {names[1]}", names, books,
                      start=_parse_ts(ev.get("commence_time")))
