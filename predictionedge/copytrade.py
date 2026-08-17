"""Copy-trading signal - mirror historically-profitable Polymarket wallets.

Identify wallets with a strong track record (the per-category PnL leaderboards), watch
the recent large-trade feed for buys BY those wallets, and surface "smart money is
buying X" signals across *any* market (sports, politics, crypto, world events) - not
just game winners. This is the Polymarket-native edge: follow proven informed money.

Free data (Polymarket Data API). Execution is on Polymarket US (a market the smart
wallet bought is taken in the same direction, sized small under the caps).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .config import Config
from .edge import copy_give_up_c
from .fees import DEFAULT_FEE_MULTIPLIER
from .polymarket_us import BUY_NO, BUY_YES
from .whales import SmartWalletScorer

# Which leaderboard a market's expertise would come from. Deliberately narrow: these
# match the Polymarket leaderboard categories exactly, because a category we cannot
# name a leaderboard for is a category we cannot check competence in.
_CRYPTO_RE = re.compile(
    r"\b(bitcoin|btc|ethereum|eth|solana|sol|xrp|ripple|dogecoin|doge|cardano|ada|"
    r"crypto|altcoin|stablecoin|satoshi|blockchain|binance|coinbase)\b", re.I)
_SPORTS_RE = re.compile(
    r"(\bvs\.?\b|\bat\b.*\bgame\b|\bhandicap\b|\bspread\b|\bo/u\b|\bover/under\b|"
    r"\bmoneyline\b|\bto win the\b|\bnba\b|\bnfl\b|\bmlb\b|\bnhl\b|\bepl\b|\bufc\b|"
    r"\batp\b|\bwta\b|\bfifa\b|\buefa\b|\blaliga\b|\bserie a\b|\bbundesliga\b|"
    r"\bopen:|\bmatch\b|\bplayoff|\bchampionship\b|\bcup\b|\bleague\b|\bseason\b)", re.I)
_POLITICS_RE = re.compile(
    r"\b(election|elected|president|presidential|senate|senator|congress|house seat|"
    r"governor|primary|nominee|nomination|parliament|prime minister|chancellor|"
    r"cabinet|impeach|referendum|ballot|vote[sd]?|voter|candidate|mayor|"
    r"secretary of|supreme court|confirmed as|resign|coalition|party leader)\b", re.I)
# The distortion case is narrower than politics: a contest with a winner, where a single
# whale's size moves the quoted odds. "Will the Fed cut" is political, but nobody's
# position in it is a claim about who wins an election.
_ELECTION_RE = re.compile(
    r"\b(election|elected|presidential race|president in \d{4}|senate race|"
    r"house race|governor|gubernatorial|primary|nominee|nomination|ballot|"
    r"referendum|mayoral|parliamentary|electoral)\b", re.I)


def classify_market(title: str, slug: str = "") -> str:
    """Which leaderboard's track record is relevant here: SPORTS/POLITICS/CRYPTO or "".

    Empty string means "we could not tell", and every caller must treat that as a
    reason to skip the category rule entirely rather than guess a category. Crypto is
    tested first because "Bitcoin above $120k" is not a sporting event no matter how
    many league-sounding words a slug contains.
    """
    text = f"{title or ''} {(slug or '').replace('-', ' ')}"
    if not text.strip():
        return ""
    if _CRYPTO_RE.search(text):
        return "CRYPTO"
    if _POLITICS_RE.search(text):
        return "POLITICS"
    if _SPORTS_RE.search(text):
        return "SPORTS"
    return ""


def is_election_market(title: str, slug: str = "") -> bool:
    """A contested race whose price a single large wallet is known to be able to move."""
    return bool(_ELECTION_RE.search(f"{title or ''} {(slug or '').replace('-', ' ')}"))


# Competitive video-gaming markets. Matched on unambiguous game names, the word
# "esports", the bracketed best-of convention Polymarket uses only here ("(BO3)"), and
# the event-slug prefixes the venue assigns per title. Deliberately NOT the league
# acronyms LCK/LPL/LEC/LCS: `lec-tig-vwh` is a LEAGUES CUP soccer fixture, and matching
# on it silently swept four soccer markets into the esports pile in testing.
_ESPORTS_RE = re.compile(
    r"counter-strike|\bcs2\b|\bcs:?go\b|league of legends|\blol\b|\bdota\b|valorant|"
    r"rainbow six|call of duty|overwatch|rocket league|starcraft|\besports?\b|"
    r"\(bo[1-9]\)|(?:^|/)(?:cs2|lol|val|dota2?|ow|r6|cod|rl|sc2)-", re.I)


def is_esports(title: str, slug_or_url: str = "") -> bool:
    """A competitive-gaming market, which this project no longer bets.

    Kept as a classifier rather than a hardcoded skip so the cut is one config flag and
    the evidence for it is testable - see `Config.esports_enabled` for the numbers. The
    slug/url is checked as well as the title because Polymarket's derivative markets on
    an esports fixture carry no game name at all ("Game Handicap: NS (-1.5) vs DN
    SOOPers (+1.5)"); only the event slug `lol-dnf-ns-2026-08-12` says what sport it is.
    """
    return bool(_ESPORTS_RE.search(f"{title or ''} {slug_or_url or ''}"))


@dataclass(frozen=True)
class CopySignal:
    market_id: str         # Polymarket condition_id
    title: str
    outcome: str           # the outcome smart money bought (Yes/No/team)
    n_wallets: int         # how many distinct profitable wallets bought it
    # Cash, not contracts. `Trade.size` off the feed is a CONTRACT count (which is why
    # dividing a size-weighted price sum by it recovers a price at all), so the dollars
    # at risk are size*price. Measured over the 567 recorded trial fills, reading the
    # count as dollars overstated the whale by 1.9x at the median and 2.8x at p10 -
    # anything asking "how big was the bet" means money, so it gets money.
    total_usd: float       # their combined stake in dollars
    avg_price: float       # their contract-weighted entry price
    minutes_ago: float     # recency of the latest such buy
    slug: str = ""         # market slug = the Polymarket US order key (marketSlug)
    event_slug: str = ""
    # Who put in what, in dollars like the total. The aggregate hides the thing that
    # matters most - whether this is one wallet's whole conviction or petty change from
    # several - and bankroll weighting downstream is per-wallet, so it cannot work off
    # the total alone. Dollars matter doubly here: this is divided by a wallet's USDC
    # bankroll downstream, and contracts over dollars is not a fraction of anything.
    wallet_usd: tuple[tuple[str, float], ...] = ()
    # True when this came from the fresh-wallet pattern rather than the leaderboard.
    # These wallets have NO track record - that is the whole point, and it means their
    # credibility rests on the shape of the bet, never on who placed it.
    fresh: bool = False
    category: str = ""             # "" = could not be determined, so do not judge on it
    n_category_matched: int = 0    # wallets proven on THIS category's leaderboard
    is_election: bool = False


@dataclass(frozen=True)
class CopyExit:
    """Smart money going the OTHER way - a sell of a specific market outcome.

    Only BUYs were ever read, which quietly assumed informed wallets only express
    themselves by entering. A wallet unwinding a position it already holds is the same
    trader with the same information, and on a market we are about to recommend it is
    the single most relevant thing on the feed.
    """
    market_id: str
    outcome: str
    n_wallets: int
    total_usd: float       # dollars sold (size*price), matching CopySignal.total_usd
    avg_price: float
    minutes_ago: float     # recency of the latest such sell


def smart_by_category(client, scorer: SmartWalletScorer, categories,
                      time_periods=("ALL",), limit: int = 50) -> dict[str, set[str]]:
    """Profitable wallets per leaderboard category, not flattened into one pile.

    Sweeping several windows matters: an ALL-time board is dominated by wallets that
    made their money long ago, while a MONTH board surfaces who is informed right now.
    A wallet only needs to clear the quality bar on one of them to be worth following.

    Keeping the categories APART is what makes "is this wallet any good at *this*"
    answerable at all - flattening them is how a soccer specialist ends up scored as an
    authority on a Senate race. No extra API calls: these are the same requests the
    union was already making, just not thrown together at the end.
    """
    out: dict[str, set[str]] = {}
    for cat in categories:
        found: set[str] = set()
        for period in time_periods:
            try:
                rows = client.leaderboard(cat, period, limit)
            except TypeError:      # older/mock clients take category only
                try:
                    rows = client.leaderboard(cat)
                except Exception:  # noqa: BLE001
                    continue
            except Exception:      # noqa: BLE001
                continue
            found |= scorer.smart_set(rows)
        out[cat] = found
    return out


def smart_universe(client, scorer: SmartWalletScorer, categories,
                   time_periods=("ALL",), limit: int = 50) -> set[str]:
    """Every profitable wallet, regardless of what they are good at."""
    smart: set[str] = set()
    for found in smart_by_category(client, scorer, categories, time_periods, limit).values():
        smart |= found
    return smart


def scan_smart_flow(client, scorer: SmartWalletScorer, *,
                    categories=("OVERALL", "SPORTS", "POLITICS", "CRYPTO"),
                    min_usd: float = 10000, limit: int = 1500,
                    max_price: float = 0.90, min_wallets: int = 1,
                    time_periods=("ALL",), leaderboard_limit: int = 50,
                    track_exits: bool = False, fresh_min_usd: float | None = None,
                    now_ts: float | None = None) -> tuple[list[CopySignal], list[CopyExit]]:
    """Both directions of smart-money flow off ONE read of the trade feed.

    Buys and sells come from the same request deliberately: fetching them separately
    would compare two different snapshots of a feed that turns over in minutes, and
    "did they sell after they bought" is a question about ordering.

    ``limit`` is how far back the trade feed reaches, and it - not ``min_usd`` - is
    the real constraint on how much signal exists: 500 trades covers only ~20h, while
    the staleness rule downstream accepts up to 48h. Lowering ``min_usd`` instead
    *shrinks* the window, since the same 500 slots fill with smaller trades.

    Two accumulators per group, named for their units and kept apart on purpose:
    ``shares`` counts contracts (``Trade.size``) and ``usd`` counts the cash those
    contracts cost (size*price). Everything a caller calls a dollar figure comes off
    ``usd``; ``shares`` exists only as the denominator that turns the cash back into a
    weighted price. They were once the same field, which quietly inflated every whale
    figure in the product by 1/price.
    """
    now = time.time() if now_ts is None else now_ts
    by_cat = smart_by_category(client, scorer, categories, time_periods, leaderboard_limit)
    smart: set[str] = set()
    for found in by_cat.values():
        smart |= found
    if not smart:
        return [], []

    groups: dict[tuple[str, str], dict] = {}
    exits: dict[tuple[str, str], dict] = {}
    unknown: dict[tuple[str, str, str], dict] = {}   # (wallet, market, outcome)
    seen_sides: dict[tuple[str, str], set[str]] = {}  # (wallet, market) -> outcomes
    for t in client.recent_trades(min_usd=min_usd, limit=limit):
        if t.wallet not in smart:
            # No leaderboard history is the normal case and usually means nothing. It
            # is also the ONLY state a genuinely new, funded-up wallet can be in, so
            # this branch is the only place the fresh-wallet pattern could ever appear.
            if fresh_min_usd is not None and t.side == "BUY" and t.wallet:
                seen_sides.setdefault((t.wallet, t.condition_id), set()).add(t.outcome)
                u = unknown.setdefault((t.wallet, t.condition_id, t.outcome), {
                    "title": t.title, "event_slug": t.event_slug, "slug": t.slug,
                    "shares": 0.0, "usd": 0.0, "latest": 0,
                })
                u["shares"] += t.size
                u["usd"] += t.size * t.price
                u["latest"] = max(u["latest"], t.ts)
            continue
        if t.side == "SELL":
            if not track_exits:
                continue
            e = exits.setdefault((t.condition_id, t.outcome),
                                 {"wallets": set(), "shares": 0.0, "usd": 0.0, "latest": 0})
            e["wallets"].add(t.wallet)
            e["shares"] += t.size
            e["usd"] += t.size * t.price
            e["latest"] = max(e["latest"], t.ts)
            continue
        if t.side != "BUY":
            continue
        g = groups.setdefault((t.condition_id, t.outcome), {
            "title": t.title, "event_slug": t.event_slug, "slug": t.slug,
            "wallets": {}, "shares": 0.0, "usd": 0.0, "latest": 0,
        })
        g["wallets"][t.wallet] = g["wallets"].get(t.wallet, 0.0) + t.size * t.price
        g["shares"] += t.size
        g["usd"] += t.size * t.price
        g["latest"] = max(g["latest"], t.ts)

    out: list[CopySignal] = []
    for (cid, outcome), g in groups.items():
        if len(g["wallets"]) < min_wallets:
            continue
        avg = g["usd"] / g["shares"] if g["shares"] > 0 else 0.0
        if avg <= 0 or avg > max_price:
            continue  # no upside left if they're already near resolution
        cat = classify_market(g["title"], g["slug"])
        matched = len(g["wallets"].keys() & by_cat.get(cat, set())) if cat else 0
        out.append(CopySignal(cid, g["title"], outcome, len(g["wallets"]), g["usd"],
                              avg, max(0.0, (now - g["latest"]) / 60.0),
                              g["slug"], g["event_slug"],
                              tuple(sorted(g["wallets"].items(),
                                           key=lambda kv: kv[1], reverse=True)),
                              False, cat, matched,
                              is_election_market(g["title"], g["slug"])))
    # Fresh-wallet candidates: one wallet, one side, big. The leaderboard cannot rank a
    # wallet with no history, so this pattern is structurally invisible to everything
    # above - it is found by shape, not by reputation. Age is NOT checked here: it costs
    # an API call per wallet, so it is the last gate applied, after the board's cheap
    # filters have already thrown most candidates out.
    for (wallet, cid, outcome), u in unknown.items():
        # Dollars against a dollar threshold. This gate used to read the contract count,
        # so the "$5k" bar really admitted ~$2.6k of cash at the median fill price. The
        # default in `whale_fresh_min_usd` was set against that inflated figure and has
        # NOT been retuned here: the bar now bites as hard as it always claimed to.
        if u["usd"] < (fresh_min_usd or 0.0):
            continue
        # Both sides of the same market is a hedge or a market-maker, not a directional
        # opinion - and "one-sided" is half the claim this signal is making.
        if len(seen_sides.get((wallet, cid), ())) != 1:
            continue
        avg = u["usd"] / u["shares"] if u["shares"] > 0 else 0.0
        if avg <= 0 or avg > max_price:
            continue
        # No track record anywhere, so no category credit is possible - only the
        # election flag, which is a property of the market rather than the trader.
        out.append(CopySignal(cid, u["title"], outcome, 1, u["usd"], avg,
                              max(0.0, (now - u["latest"]) / 60.0),
                              u["slug"], u["event_slug"],
                              ((wallet, u["usd"]),), True,
                              classify_market(u["title"], u["slug"]), 0,
                              is_election_market(u["title"], u["slug"])))

    out.sort(key=lambda s: s.total_usd, reverse=True)

    # Exits carry no min_wallets / max_price gate: those exist to judge whether a BUY
    # is worth copying. One smart wallet heading for the door is worth saying out loud
    # regardless, because it is a caveat on advice, not advice of its own.
    exit_out = [
        CopyExit(cid, outcome, len(e["wallets"]), e["usd"],
                 e["usd"] / e["shares"] if e["shares"] > 0 else 0.0,
                 max(0.0, (now - e["latest"]) / 60.0))
        for (cid, outcome), e in exits.items()
    ]
    exit_out.sort(key=lambda x: x.total_usd, reverse=True)
    return out, exit_out


def find_copy_signals(client, scorer: SmartWalletScorer, **kw) -> list[CopySignal]:
    """Just the buy side. Kept because callers and tests speak in copy signals."""
    kw.pop("track_exits", None)
    return scan_smart_flow(client, scorer, **kw)[0]


def copy_order_params(signal: CopySignal, market, *, min_price: float = 0.05,
                      max_price: float = 0.90, size_usd: float = 5.0,
                      max_give_up_c: float = Config.board_max_drift_c,
                      fee_multiplier: float = DEFAULT_FEE_MULTIPLIER):
    """(intent, price, quantity) for a Polymarket US copy order, or None if not actionable.

    Side mirrors what the smart money bought; price comes from PM-US's own book (BUY YES
    at the YES ask; BUY NO at the NO ask = 1 - YES bid), capped to a sane band.

    The order path had NO comparison to the whale's own price at all - not drift, not
    fees - so it would copy a fill at any distance from the trade it was copying, while
    the research board next door refused anything past 4c. Same bar, same function
    (`edge.copy_give_up_c`), so the armed path can never take what the board rejects.
    """
    if market is None:
        return None
    buy_no = signal.outcome.strip().lower() == "no"
    intent = BUY_NO if buy_no else BUY_YES
    price = round(1.0 - market.yes_bid, 2) if buy_no else round(market.yes_ask, 2)
    if not (min_price <= price <= max_price):
        return None
    if copy_give_up_c(price, signal.avg_price, multiplier=fee_multiplier) > max_give_up_c:
        return None
    qty = int(size_usd / price)
    if qty < 1:
        return None
    return intent, price, qty
