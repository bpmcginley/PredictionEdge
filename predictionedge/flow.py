"""Whale-flow spike detector - sudden large bets, the insider-flow signal.

Polls Polymarket's public large-trade feed and surfaces big, recent bets on *event*
markets - politics, geopolitics, world events - which is where a sudden large
position is most likely informed (the canonical case: a massive buy on a
"will X happen" market moments before it happens). High-frequency crypto up/down
scalping and routine sports games are filtered out by default as noise.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .polymarket import Trade

# Crypto micro-duration scalping - pure noise, never insider signal.
_SCALP = re.compile(
    r"up or down|updown|-15m-|-1h-|-1m-|hourly|every \d+ min|\bup\b.*\bdown\b", re.IGNORECASE
)
# Routine sports / esports games. Matched by league/sport keywords (which appear in
# the slug) - deliberately NOT bare "vs", which also shows up in political head-to-heads.
_SPORTS = re.compile(
    r"\b(nba|wnba|nfl|mlb|nhl|ncaa|cfb|cbb|dota|cs2|csgo|valorant|league of legends|"
    r"ufc|atp|wta|f1|formula 1|premier league|la liga|serie a|bundesliga|ligue 1|"
    r"champions league|europa league|tennis|golf|pga|world cup|soccer)\b", re.IGNORECASE
)
# Sports *betting* structures (spreads/totals/dated game winners) - very common on
# Polymarket during tournaments and not league-named in the title.
_SPORTS_BET = re.compile(
    r"spread:|moneyline|\bo/u\b|over/under|\b(over|under)\b\s*\d|\(\s*[-+]\d|"
    r"win on \d{4}-\d{2}-\d{2}|halftime|half-time|first half|leading at|"
    r"end in a draw|clean sheet|to win the match|both teams to score|exact score", re.IGNORECASE
)


def classify(trade: Trade) -> str:
    """'scalp' | 'sports' | 'event'."""
    s = f"{trade.title} {trade.slug} {trade.event_slug}".lower()
    if _SCALP.search(s):
        return "scalp"
    if _SPORTS.search(s) or _SPORTS_BET.search(s):
        return "sports"
    return "event"


@dataclass(frozen=True)
class Spike:
    trade: Trade
    category: str
    minutes_ago: float


def find_spikes(client, *, min_usd: float = 25000, limit: int = 150,
                include_sports: bool = False, now_ts: float | None = None) -> list[Spike]:
    """Recent large bets on event markets, biggest first. Scalp noise removed."""
    now = time.time() if now_ts is None else now_ts
    spikes: list[Spike] = []
    for t in client.recent_trades(min_usd=min_usd, limit=limit):
        category = classify(t)
        if category == "scalp":
            continue
        if category == "sports" and not include_sports:
            continue
        spikes.append(Spike(t, category, max(0.0, (now - t.ts) / 60.0)))
    spikes.sort(key=lambda s: s.trade.size, reverse=True)
    return spikes
