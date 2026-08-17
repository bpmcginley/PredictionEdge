"""Whale-flow spike detector - sudden large bets, the insider-flow signal.

Polls Polymarket's public large-trade feed and surfaces big, recent bets on *event*
markets - politics, geopolitics, world events - which is where a sudden large
position is most likely informed (the canonical case: a massive buy on a
"will X happen" market moments before it happens). High-frequency crypto up/down
scalping and routine sports games are filtered out by default as noise.

"Big" means DOLLARS everywhere in this module. ``Trade.size`` off the feed is a
contract count, and a contract costs its price, so cash is size*price - a distinction
worth spelling out because reading one as the other made cheap longshots look like the
biggest bets on the board. Every figure here that means money is called ``usd``.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .copytrade import is_esports
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
    """'scalp' | 'esports' | 'sports' | 'event'.

    Esports is its own answer rather than a flavour of sports because the project
    switches the two independently - ordinary sports are noise on this panel, esports
    are a market the project decided not to bet at all (`Config.esports_enabled`).

    The test is ``copytrade.is_esports``, the same classifier the board and the paper
    trial reject on, NOT a copy of its pattern here: `_SPORTS` lists "league of legends"
    and misses the "LoL:" titles Polymarket actually publishes, which is precisely how
    two copies of one rule drift apart. Checked before sports, since an esports fixture
    can carry league words too.
    """
    s = f"{trade.title} {trade.slug} {trade.event_slug}".lower()
    if _SCALP.search(s):
        return "scalp"
    if is_esports(trade.title, f"{trade.slug} {trade.event_slug}"):
        return "esports"
    if _SPORTS.search(s) or _SPORTS_BET.search(s):
        return "sports"
    return "event"


@dataclass(frozen=True)
class Spike:
    trade: Trade
    category: str
    minutes_ago: float
    # What the bet COST, in dollars: trade.size is a contract count, so the money is
    # size*price. Carried as its own field rather than left for each caller to work
    # out, because "how big was this bet" is the entire question this module answers
    # and every caller that reached for `trade.size` got a number 1/price too big.
    # Required, deliberately: a defaulted 0.0 would let a Spike exist with no dollar
    # figure and sort to the bottom in silence, which is how a units bug hides.
    usd: float


def _trade_usd(t: Trade) -> float:
    """Dollars a fill cost: contracts * price per contract. The only conversion here."""
    return t.size * t.price


def find_spikes(client, *, min_usd: float = 25000, limit: int = 150,
                include_sports: bool = False, esports_enabled: bool = False,
                now_ts: float | None = None) -> list[Spike]:
    """Recent large bets on event markets, biggest FIRST BY DOLLARS. Scalp noise removed.

    ``min_usd`` is cash, matching what the feed itself applies (the Data API's
    filterType=CASH bar is size*price), and the ranking is now in those same dollars.
    It used to rank on ``trade.size``, the contract count, so the list was filtered in
    money and ordered by count: 100k contracts at 3c ($3k) outranked 20k at 80c ($16k).
    That is not a cosmetic difference - the dashboard publishes only the top 18, so the
    wrong order silently dropped the genuinely larger bets off the end.

    ``limit`` is feed rows to read, not a money figure, and is passed straight through.

    ``esports_enabled`` is a plain bool taking ``cfg.esports_enabled``, not a Config
    object: this module has never needed one and a function that reaches for global
    config is harder to test than one told what to do. It defaults to False to match
    that setting's own default, so the panel stops surfacing markets the project
    already decided not to bet - "LoL: SK Gaming vs Fnatic" is not a world event.
    Separate from ``include_sports`` on purpose: asking to see ball games is not asking
    to see the category that was retired.
    """
    now = time.time() if now_ts is None else now_ts
    spikes: list[Spike] = []
    for t in client.recent_trades(min_usd=min_usd, limit=limit):
        category = classify(t)
        if category == "scalp":
            continue
        if category == "esports" and not esports_enabled:
            continue
        if category == "sports" and not include_sports:
            continue
        spikes.append(Spike(t, category, max(0.0, (now - t.ts) / 60.0), _trade_usd(t)))
    spikes.sort(key=lambda s: s.usd, reverse=True)
    return spikes
