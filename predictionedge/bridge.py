"""Venue bridge: re-record each whale ticket at the price a US person could pay.

The whale signal fires on international Polymarket, which a US person cannot trade.
The trial's `whale` sleeve therefore measures the signal at a venue that is off the
table. This module answers the question that actually matters - does the edge survive
the hop to a tradeable venue? - by paper-recording every shown whale ticket a second
and third time, at the live ask on Polymarket US (`whale-pmus`) and on Kalshi
(`whale-kalshi`). Same signal, same moment, different books: any spread between the
three sleeves' realised edges IS the venue cost, measured instead of assumed.

Design rules, in order of importance:

1. WRONG MATCHES ARE WORSE THAN NO MATCHES. A missed ticket costs one data point; a
   ticket recorded against the wrong contract reports someone else's outcome as ours.
   So both adapters refuse anything ambiguous: the PM-US matcher drops colliding
   signatures outright (see `pmus_match`), and the Kalshi adapter never searches - it
   CONSTRUCTS the one exact ticker the ticket implies and checks whether it exists.
   A wrong guess about Kalshi's naming produces zero matches, never a wrong row.
   Neither adapter is even offered a market whose title does not name the outcome the
   bet is on - spreads, totals, map/game/set legs - because there the two venues use
   the same words for different questions (see `_DERIVATIVE_TITLE`).
2. TAKER FEES, ALWAYS. Bridge rows model "take the ask the moment the board fires",
   so they carry `_maker: False` regardless of the trial's global maker default.
   Kalshi's published taker formula is also applied to the PM-US rows - PM-US's own
   schedule is unverified, and overcharging a paper sleeve is the safe direction.
3. COVERAGE IS PART OF THE RESULT. Every ticket the bridge could not carry across is
   logged with a reason under `trial["bridge"]`, because a sleeve that silently skips
   the hard half of the flow would report the easy half's edge as the venue's.

Everything here reads public, unauthenticated endpoints only (gateway.polymarket.us
and Kalshi market data). No keys, no orders, no account.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
import unicodedata

from .pmus_match import PMUSMatcher, slug_signature

log = logging.getLogger(__name__)

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

# Intl-slug league prefix -> Kalshi single-game series. Best-effort and deliberately
# incomplete: an unlisted league logs `no-series` rather than guessing, and a wrong
# series name here yields zero matches (the constructed ticker simply won't exist),
# never a wrong one. Extend via PE_BRIDGE_KALSHI_SERIES ("mlb:KXMLBGAME,nba:...").
DEFAULT_KALSHI_SERIES: dict[str, str] = {
    "mlb": "KXMLBGAME",
    "nba": "KXNBAGAME",
    "nfl": "KXNFLGAME",
    "nhl": "KXNHLGAME",
    "wnba": "KXWNBAGAME",
}

_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

# Statuses that can never improve with retrying: the ticket itself lacks what the
# adapter needs. Everything else (listings not up yet, a lookup that timed out, a
# name that didn't map) is retried on every run until the ticket leaves the board.
# "outcome-not-yes-no" - retired 2026-08-12 when the name matcher landed - is
# deliberately NOT here, so tickets it froze under the old adapter get re-tried.
# "origin-not-binary" is permanent for the same reason the others are: a title does not
# become a different kind of market on the next run.
_PERMANENT = frozenset({"matched", "no-signature", "no-series", "outcome-not-a-team",
                        "origin-not-binary"})


def parse_series_env(raw: str) -> dict[str, str]:
    """`"mlb:KXMLBGAME,nba:KXNBAGAME"` -> league map; blank/garbage entries dropped."""
    out = dict(DEFAULT_KALSHI_SERIES)
    for part in (raw or "").split(","):
        if ":" not in part:
            continue
        league, series = part.split(":", 1)
        if league.strip() and series.strip():
            out[league.strip().lower()] = series.strip().upper()
    return out


def _date_token(iso_date: str) -> str | None:
    """`2026-08-12` -> `26AUG12`, the token Kalshi bakes into game tickers."""
    try:
        y, m, d = iso_date.split("-")
        return f"{int(y) % 100:02d}{_MONTHS[int(m) - 1]}{int(d):02d}"
    except (ValueError, IndexError):
        return None


def kalshi_candidates(slug: str, outcome: str,
                      series_map: dict[str, str]) -> tuple[list[str], str]:
    """The exact Kalshi tickers this intl slug could be, or a reason it can't match.

    Returns (candidates, reason): candidates non-empty means "check whether one of
    these exists", empty means the reason says why this ticket can never bridge.
    Construction, not search: `mlb-bal-tex-2026-08-12-tex` implies exactly
    `KXMLBGAME-26AUG12BALTEX-TEX` or `KXMLBGAME-26AUG12TEXBAL-TEX` (home/away order
    is the one fact the slug doesn't state), and nothing else is ever considered.
    """
    sig = slug_signature(slug)
    if sig is None:
        return [], "no-signature"
    teams, date, out_token = sig
    league = slug.split("-", 1)[0].lower()
    series = series_map.get(league)
    if not series:
        return [], "no-series"
    # The outcome must name one of the two teams, because the ticker suffix is a team
    # code. Totals, spreads and draws don't, and refusing them here is what keeps an
    # "Over 8.5" from ever being booked as a moneyline (rule 1 in the module docstring).
    code = out_token if out_token in teams else (outcome or "").strip().lower()
    if code not in teams:
        return [], "outcome-not-a-team"
    dt = _date_token(date)
    if dt is None:
        return [], "no-signature"
    a, b = sorted(t.upper() for t in teams)
    return [f"{series}-{dt}{a}{b}-{code.upper()}",
            f"{series}-{dt}{b}{a}-{code.upper()}"], ""


def _kalshi_lookup(candidates: list[str], fetch) -> tuple[dict | None, str]:
    """Find which (if either) candidate ticker exists; return its market row."""
    failed = False
    for ticker in candidates:
        event = ticker.rsplit("-", 1)[0]
        try:
            data = fetch(f"{KALSHI_API}/markets", {"event_ticker": event, "limit": 200})
        except Exception:  # noqa: BLE001 - one venue down must not kill the other
            failed = True
            continue
        for m in data.get("markets") or []:
            if m.get("ticker") == ticker:
                return m, "matched"
    return None, ("lookup-failed" if failed else "no-market")


# PM-US market types that mean "who wins the whole event" - the only claim an intl
# moneyline/match ticket makes. The same two sides also hang off spreads, totals,
# set/inning/quarter winners and "first five" markets, so matching on names alone
# would happily route a match bet onto a set-1 market; the type gate is what stops it.
_WINNER_TYPE_SUFFIXES = ("_match_winner", "_fight_winner", "_full_game_winner",
                         "_team_winner")

_SLUG_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _is_winner_type(market_type: str) -> bool:
    mt = (market_type or "").lower()
    return mt == "moneyline" or mt.endswith(_WINNER_TYPE_SUFFIXES)


def _norm_words(s: str) -> list[str]:
    """Accent-stripped lowercase words: "Christopher O'Connell" -> [christopher,
    oconnell]. Apostrophes join rather than split so `oconnel` can prefix-match."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("'", "").replace("’", "")
    return [w for w in re.split(r"[^a-z0-9]+", s) if w]


def _date_of(iso_or_slug: str) -> _dt.date | None:
    m = _SLUG_DATE.search(iso_or_slug or "")
    if not m:
        return None
    try:
        return _dt.date.fromisoformat(m.group(0))
    except ValueError:
        return None


def _frag_hits(frag: str, side: dict) -> bool:
    """Does an intl slug fragment ("shevche", "sd") name this PM-US side?

    Two spellings of the same participant: intl truncates names into slug fragments,
    PM-US carries its own abbreviation plus the full name. Equality with the venue
    abbreviation catches team codes; prefix-against-a-name-word catches the truncated
    surnames ("nakashi" -> "nakashima"). At least 2 chars, or everything matches.
    """
    if len(frag) < 2:
        return False
    if frag == (side.get("abbr") or "").lower():
        return True
    return any(w.startswith(frag) for w in _norm_words(side.get("name") or ""))


def match_by_name(listings, intl_slug: str, outcome: str):
    """Match an intl named-outcome ticket to the PM-US market for the same event.

    Returns ((listing, side), "matched") or (None, reason). Used when the intl
    market's outcomes are participant names (tennis, MLB/NBA/NFL moneylines, UFC):
    those PM-US markets use the venue's OWN abbreviations in the slug ("aleshe", not
    "shevche"), so slug-signature equality can never fire and the match has to run
    on the metadata instead. Requirements, all of them:

    - winner-type market with exactly two sides;
    - event date within a day of the intl date (time zones straddle midnight);
    - BOTH intl slug fragments identify DISTINCT sides - one hit is a name-alike,
      two hits on the same game is the game;
    - the ticket's outcome equals exactly one side's full name (normalized);
    - and only one market survives all of the above - two survivors means refuse.
    """
    sig = slug_signature(intl_slug)
    if sig is None:
        return None, "no-signature"
    teams, date_s, _ = sig
    frags = [f.lower() for f in teams]
    idate = _date_of(date_s)
    want = _norm_words(outcome)
    if idate is None or not want:
        return None, "no-signature"

    matches, saw_candidate = [], False
    for li in listings:
        if not _is_winner_type(li.market_type) or len(li.sides) != 2:
            continue
        ldate = _date_of(li.game_start) or _date_of(li.slug)
        if ldate is None or abs((ldate - idate).days) > 1:
            continue
        a, b = li.sides
        pairs = [(x, y) for x, y in ((a, b), (b, a))
                 if _frag_hits(frags[0], x) and _frag_hits(frags[1], y)]
        if not pairs:
            continue
        saw_candidate = True
        picked = [s for s in (a, b) if _norm_words(s.get("name") or "") == want]
        if len(picked) == 1:
            matches.append((li, picked[0]))
    if len(matches) == 1:
        return matches[0], "matched"
    if len(matches) > 1:
        return None, "ambiguous"
    return None, ("outcome-unmapped" if saw_candidate else "no-counterpart")


# Origin titles where the team named in the title is NOT the outcome the bet is on.
# On a plain binary market "Texas Rangers" means the Rangers win; on the run line it
# means they win by two, and on a map/game/set market it means they take one leg of a
# series. Both venues use the same words for those different questions, so the matchers
# above - which reason about names, dates and team codes, none of which change - will
# cheerfully find the destination's PLAIN market for a spread ticket and book someone
# else's question at our price. Every 24-28c "arbitrage" this record has shown between
# venues came from exactly that: not free money, the price of a different question.
#
# What this is: a heuristic over titles, and it is wrong in both directions. A mis-typed
# market whose title reads plain ("Rangers to win the series") sails straight through,
# and a genuinely binary market that merely contains the word "total" ("Will total
# domestic gross reach $900m?") is refused and costs real coverage. It is a title regex
# because the origin ticket has no market-type field to ask instead: PM-US publishes one
# (`sportsMarketType`, which is what `_is_winner_type` gates the DESTINATION on) and the
# international Gamma ticket the whale sleeve builds carries no equivalent. If one ever
# appears on the origin ticket, that field belongs here and this regex becomes its
# fallback.
_DERIVATIVE_TITLE = re.compile(
    r"handicap|spread|o/u|over/under|total|map \d|game \d|set \d|"
    r"first five|inning|quarter|[+-]\d+\.5", re.I)


def is_derivative_title(title: str) -> bool:
    """True when a title names a market whose team name is not the outcome bet on.

    Public because the paper trial holds these rows out of its headline using this
    predicate, and it has to be THIS one. The same argument the trial already makes for
    calling `is_esports` rather than copying its regex: what the record excludes and what
    the bridge refuses must be one implementation, or the two eventually disagree and the
    direction they disagree in is nobody's decision.
    """
    return bool(_DERIVATIVE_TITLE.search(title or ""))


def _carry(t: dict) -> dict:
    """Slice-later context copied from the origin ticket onto its bridge rows.

    `minutes_ago` and `wallet_usd` travel with the rest: a bridge row is the SAME whale
    signal expressed at another venue's price, so it has to answer "which wallets, how
    stale" identically to its origin or the two stop being comparable.
    """
    return {k: t.get(k) for k in ("conviction", "n_wallets", "whale_usd", "drift_c",
                                  "hours_to_resolve", "liquidity", "end_iso",
                                  "event_iso", "minutes_ago", "wallet_usd")}


def build_bridge_tickets(tickets: list[dict], *, pmus_client=None, kalshi_fetch=None,
                         series_map: dict[str, str] | None = None,
                         skip: set[str] | None = None):
    """Bridge each shown whale ticket onto both tradeable venues.

    Returns (bridge_tickets, attempts) where each attempt is
    {"origin_key", "venue", "status"}. `skip` holds "origin_key:venue" strings whose
    status is already permanent (matched or structurally impossible) so settled
    history isn't re-fetched every 15 minutes. A ticket whose origin market is not a
    plain binary question reaches neither adapter - see `_DERIVATIVE_TITLE`.
    """
    series_map = series_map if series_map is not None else DEFAULT_KALSHI_SERIES
    skip = skip or set()
    out: list[dict] = []
    attempts: list[dict] = []

    def note(okey: str, venue: str, status: str):
        attempts.append({"origin_key": okey, "venue": venue, "status": status})

    # Built lazily: the full PM-US listing is ~110 paginated calls (21k+ markets with
    # futures at the front and no server-side filter), paid only when a ticket needs it
    # and once per run for all tickets together.
    listings = None
    matcher = None

    def _load_listings():
        nonlocal listings, matcher
        if listings is None:
            listings = pmus_client.active_markets()
            matcher = PMUSMatcher([li.slug for li in listings])
        return listings

    def _pmus_ask(slug_: str, want_long: bool) -> float:
        """The price to take now: the long ask, or 1 - long bid for the short leg
        (verified equal to the venue's own shortQuote, 2026-08-12)."""
        q = pmus_client.market(slug_)
        if q is None:
            return 0.0
        if want_long:
            return q.yes_ask
        return (1.0 - q.yes_bid) if q.yes_bid > 0 else 0.0

    for t in tickets:
        slug = (t.get("slug") or "").strip().lower()
        okey = f"{t.get('market_id', '')}:{(t.get('outcome') or '').strip().lower()}"
        price = float(t.get("entry_price") or 0.0)
        if not 0.0 < price < 1.0:
            continue  # the trial itself would refuse it; don't measure coverage on it

        # One gate, both venues: what makes this ticket unmirrorable is a fact about the
        # ORIGIN market, so it is refused here rather than re-derived inside each
        # adapter, where the two could drift apart. Recorded per venue like any other
        # refusal, because that is the only way the cost shows up: a gate that skipped
        # silently would shrink the denominator and make the bridge's coverage read
        # better for having stopped trying.
        if _DERIVATIVE_TITLE.search(t.get("title") or ""):
            for venue, attempted in (("polymarket-us", pmus_client is not None),
                                     ("kalshi", kalshi_fetch is not None)):
                if attempted and f"{okey}:{venue}" not in skip:
                    note(okey, venue, "origin-not-binary")
            continue

        # --- Polymarket US -------------------------------------------------
        if pmus_client is not None and f"{okey}:polymarket-us" not in skip:
            sig = slug_signature(slug)
            outcome = (t.get("outcome") or "").strip().lower()
            counterpart, ask, status = "", 0.0, ""
            if sig is None:
                status = "no-signature"
            elif outcome in ("yes", "no"):
                # Per-outcome markets (soccer's `...-ger`, `...-draw`): same slug
                # shape both venues, so the exact signature matcher applies.
                _load_listings()
                counterpart = matcher.match(slug)
                if counterpart is None:
                    status = ("ambiguous" if sig in matcher.ambiguous
                              else "no-counterpart")
                else:
                    ask = _pmus_ask(counterpart, want_long=(outcome == "yes"))
            else:
                # Named outcomes (tennis, MLB/UFC moneylines): PM-US slugs use the
                # venue's own abbreviations, so the match runs on side metadata.
                hit, status = match_by_name(_load_listings(), slug,
                                            t.get("outcome", ""))
                if hit is not None:
                    li, side = hit
                    counterpart, status = li.slug, ""
                    ask = _pmus_ask(li.slug, want_long=side["long"])
            if not status and not 0.0 < ask < 1.0:
                status = "no-ask"
            if status:
                note(okey, "polymarket-us", status)
            else:
                note(okey, "polymarket-us", "matched")
                out.append({
                    "market_id": counterpart,
                    "venue": "polymarket-us",
                    "origin_market_id": t.get("market_id", ""),
                    # The INTL outcome name, verbatim: settlement compares it to the
                    # origin market's winner, never to PM-US's own labels.
                    "outcome": t.get("outcome", ""),
                    "title": t.get("title", ""),
                    "url": f"https://polymarket.us/market/{counterpart}",
                    "entry_price": round(ask, 4),
                    "source": "whale-pmus",
                    "_shown": True,
                    "_maker": False,
                    **_carry(t),
                })

        # --- Kalshi --------------------------------------------------------
        if kalshi_fetch is not None and f"{okey}:kalshi" not in skip:
            candidates, reason = kalshi_candidates(slug, t.get("outcome", ""),
                                                   series_map)
            if not candidates:
                note(okey, "kalshi", reason)
            else:
                m, status = _kalshi_lookup(candidates, kalshi_fetch)
                if m is None:
                    note(okey, "kalshi", status)
                else:
                    from .kalshi import _price_to_dollars
                    ask = _price_to_dollars(m, "yes_ask")
                    if not 0.0 < ask < 1.0:
                        note(okey, "kalshi", "no-ask")
                    else:
                        note(okey, "kalshi", "matched")
                        ticker = m["ticker"]
                        out.append({
                            "market_id": ticker,
                            "venue": "kalshi",
                            "origin_market_id": t.get("market_id", ""),
                            # The ticker names the team; the position on it is YES.
                            "outcome": "Yes",
                            "title": m.get("title") or t.get("title", ""),
                            "url": f"https://kalshi.com/markets/"
                                   f"{ticker.rsplit('-', 1)[0]}",
                            "entry_price": round(ask, 4),
                            "source": "whale-kalshi",
                            "_shown": True,
                            "_maker": False,
                            **_carry(t),
                        })

    return out, attempts


def update_log(trial: dict, attempts: list[dict], *, now: float) -> None:
    """Fold this run's attempts into `trial["bridge"]` and refresh the summary.

    One entry per origin-leg-venue, upgraded in place: a transient failure may become
    a match on a later run (listings appear, a timeout clears), but `matched` is
    final - the position was recorded at that moment's price and the log must keep
    saying so. The summary is recomputed from the log so it can never drift from it.
    """
    br = trial.setdefault("bridge", {})
    seen = br.setdefault("log", {})
    for a in attempts:
        entry = seen.setdefault(a["origin_key"], {})
        if entry.get(a["venue"]) != "matched":
            entry[a["venue"]] = a["status"]
            entry["ts"] = round(now, 1)
    summary: dict[str, dict[str, int]] = {}
    for entry in seen.values():
        for venue, status in entry.items():
            if venue == "ts":
                continue
            v = summary.setdefault(venue, {})
            v[status] = v.get(status, 0) + 1
    br["summary"] = summary


def permanent_skips(trial: dict) -> set[str]:
    """"origin_key:venue" pairs whose bridge status can never change - see _PERMANENT."""
    out: set[str] = set()
    for okey, entry in (trial.get("bridge", {}).get("log") or {}).items():
        for venue, status in entry.items():
            if venue != "ts" and status in _PERMANENT:
                out.add(f"{okey}:{venue}")
    return out
