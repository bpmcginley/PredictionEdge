"""Linking a sportsbook event to the Kalshi market that resolves on the same thing.

Reliable auto-matching across venues is genuinely hard (different titles, scopes,
and resolution criteria), and a wrong match silently turns "edge" into noise, so
this starts as an explicit, audited registry. ``yes_is_outcome0`` records which
sportsbook outcome corresponds to Kalshi's YES, so we can orient the probability.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime

from .normalize import team_score
from .odds import SportEvent


@dataclass(frozen=True)
class MarketLink:
    kalshi_ticker: str
    event_id: str          # SportEvent.event_id from the odds provider
    yes_is_outcome0: bool  # True if Kalshi YES == sportsbook outcome[0]
    note: str = ""


# Explicit, hand-verified links. Each one should be eyeballed before it earns a spot.
DEFAULT_LINKS: list[MarketLink] = [
    MarketLink("KXNBA-LALBOS", "evt-lakers-celtics", yes_is_outcome0=True,
               note="Kalshi YES = Lakers win = sportsbook outcome[0]"),
    MarketLink("KXFED-CUT", "evt-fed-cut", yes_is_outcome0=True,
               note="Kalshi YES = Fed cuts = sportsbook outcome[0]"),
]


def orient(fair_prob_outcome0: float, link: MarketLink) -> float:
    """Convert a sportsbook outcome[0] probability into P(Kalshi YES)."""
    return fair_prob_outcome0 if link.yes_is_outcome0 else 1.0 - fair_prob_outcome0


# --- Automatic cross-venue matching ----------------------------------------

@dataclass(frozen=True)
class KalshiSportMarket:
    """A Kalshi sports market parsed into its two teams and YES orientation."""
    ticker: str
    team_a: str
    team_b: str
    yes_team: str            # the team Kalshi YES pays out on
    start: datetime | None = None


def _time_ok(k: KalshiSportMarket, e: SportEvent, window_hours: float) -> bool:
    if k.start is None or e.start is None:
        return True  # no time to compare on -> rely on the team match alone
    return abs((k.start - e.start).total_seconds()) <= window_hours * 3600.0


def _pair_score(k: KalshiSportMarket, e: SportEvent) -> float:
    """Best score for lining up both teams, in either orientation."""
    o0, o1 = e.outcome_names
    direct = min(team_score(k.team_a, o0), team_score(k.team_b, o1))
    swapped = min(team_score(k.team_a, o1), team_score(k.team_b, o0))
    return max(direct, swapped)


def match_market(
    market: KalshiSportMarket, events: list[SportEvent], *,
    min_team_score: float = 0.5, window_hours: float = 18.0,
) -> MarketLink | None:
    """Link one Kalshi market to its best-matching sportsbook event, or None.

    Requires BOTH teams to line up (in some orientation) at or above
    ``min_team_score`` and, when both have a start time, to fall within
    ``window_hours`` - a wrong match silently turns edge into noise, so the bar
    is deliberately a dual-team agreement, not a single fuzzy hit.
    """
    best: SportEvent | None = None
    best_score = min_team_score
    for e in events:
        if not _time_ok(market, e, window_hours):
            continue
        score = _pair_score(market, e)
        if score >= best_score:
            best_score, best = score, e
    if best is None:
        return None
    o0, o1 = best.outcome_names
    yes_is_outcome0 = team_score(market.yes_team, o0) >= team_score(market.yes_team, o1)
    return MarketLink(market.ticker, best.event_id, yes_is_outcome0,
                      note=f"auto-match score={best_score:.2f}")


def build_links(
    markets: list[KalshiSportMarket], events: list[SportEvent], *,
    min_team_score: float = 0.5, window_hours: float = 18.0,
) -> list[MarketLink]:
    """Match a batch of Kalshi markets to sportsbook events."""
    links = []
    for m in markets:
        link = match_market(m, events, min_team_score=min_team_score,
                            window_hours=window_hours)
        if link is not None:
            links.append(link)
    return links


# Best-effort title -> teams parser. The exact Kalshi sports title/ticker format is
# pending verification (research workflow), so this handles the common shapes and is
# the single swappable piece; the matcher above is format-independent.
_VS = re.compile(r"\b(?:vs\.?|@|at|beat|beats|over|defeats?)\b", re.IGNORECASE)


def parse_sports_title(ticker: str, title: str, start: datetime | None = None
                       ) -> KalshiSportMarket | None:
    """Parse 'Team A vs Team B' / 'Will A beat B?' style titles into two teams.

    Heuristic: the first team mentioned is treated as the YES subject. Returns None
    if two teams can't be confidently extracted.
    """
    cleaned = re.sub(r"^(will|does|did)\s+the\s+|^the\s+|^game\s*\d+\s*:?\s*|\?$", "",
                     title.strip(), flags=re.IGNORECASE).strip()
    parts = _VS.split(cleaned, maxsplit=1)
    if len(parts) != 2:
        return None
    team_a, team_b = _clean_team(parts[0]), _clean_team(parts[1])
    if not team_a or not team_b:
        return None
    return KalshiSportMarket(ticker, team_a, team_b, yes_team=team_a, start=start)


_NOISE = re.compile(r"\b(the|winner|game|pro\s+football|pro\s+basketball)\b", re.IGNORECASE)


def _clean_team(s: str) -> str:
    return _NOISE.sub("", s).strip(" ?.")


def parse_sports_market(market, start: datetime | None = None) -> KalshiSportMarket | None:
    """Parse a Kalshi sports market into its matchup + YES orientation.

    Kalshi lists one market PER TEAM (e.g. ...ATLSF-SF and ...ATLSF-ATL), and on each
    the YES/NO sub-titles both name that market's own team - so the two opponents come
    from the TITLE ("Atlanta vs San Francisco Winner?"), and ``yes_sub_title`` (the
    authoritative team field) tells us which side YES backs. EPL "Tie" markets aren't
    two-way moneylines, so they're skipped.
    """
    yes_team = (getattr(market, "yes_sub_title", "") or "").strip()
    if yes_team.lower() == "tie":
        return None
    parsed = parse_sports_title(getattr(market, "ticker", ""),
                                getattr(market, "title", ""), start=start)
    if parsed is None:
        return None
    if yes_team:
        yt = (parsed.team_a if team_score(yes_team, parsed.team_a)
              >= team_score(yes_team, parsed.team_b) else parsed.team_b)
        return replace(parsed, yes_team=yt)
    return parsed
