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

import logging

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
# adapter needs. Everything else (listings not up yet, a lookup that timed out) is
# retried on every run until the ticket leaves the board.
_PERMANENT = frozenset({"matched", "no-signature", "no-series", "outcome-not-a-team",
                        "outcome-not-yes-no"})


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


def _carry(t: dict) -> dict:
    """Slice-later context copied from the origin ticket onto its bridge rows."""
    return {k: t.get(k) for k in ("conviction", "n_wallets", "whale_usd", "drift_c",
                                  "hours_to_resolve", "liquidity", "end_iso",
                                  "event_iso")}


def build_bridge_tickets(tickets: list[dict], *, pmus_client=None, kalshi_fetch=None,
                         series_map: dict[str, str] | None = None,
                         skip: set[str] | None = None):
    """Bridge each shown whale ticket onto both tradeable venues.

    Returns (bridge_tickets, attempts) where each attempt is
    {"origin_key", "venue", "status"}. `skip` holds "origin_key:venue" strings whose
    status is already permanent (matched or structurally impossible) so settled
    history isn't re-fetched every 15 minutes.
    """
    series_map = series_map if series_map is not None else DEFAULT_KALSHI_SERIES
    skip = skip or set()
    out: list[dict] = []
    attempts: list[dict] = []

    def note(okey: str, venue: str, status: str):
        attempts.append({"origin_key": okey, "venue": venue, "status": status})

    matcher = None  # built lazily: 8 paginated calls only if a ticket needs them

    for t in tickets:
        slug = (t.get("slug") or "").strip().lower()
        okey = f"{t.get('market_id', '')}:{(t.get('outcome') or '').strip().lower()}"
        price = float(t.get("entry_price") or 0.0)
        if not 0.0 < price < 1.0:
            continue  # the trial itself would refuse it; don't measure coverage on it

        # --- Polymarket US -------------------------------------------------
        if pmus_client is not None and f"{okey}:polymarket-us" not in skip:
            sig = slug_signature(slug)
            outcome = (t.get("outcome") or "").strip().lower()
            if sig is None:
                note(okey, "polymarket-us", "no-signature")
            elif outcome not in ("yes", "no"):
                # bbo prices the Yes leg; an outcome named anything else (a team, a
                # bracket) has no stated side on a binary quote. Refuse, don't guess.
                note(okey, "polymarket-us", "outcome-not-yes-no")
            else:
                if matcher is None:
                    matcher = PMUSMatcher(pmus_client.active_slugs())
                counterpart = matcher.match(slug)
                if counterpart is None:
                    note(okey, "polymarket-us",
                         "ambiguous" if sig in matcher.ambiguous else "no-counterpart")
                else:
                    q = pmus_client.market(counterpart)
                    ask = 0.0
                    if q is not None:
                        # Buying NO takes the other side of the Yes book: pay 1 - bid.
                        ask = q.yes_ask if outcome == "yes" else (
                            (1.0 - q.yes_bid) if q.yes_bid > 0 else 0.0)
                    if not 0.0 < ask < 1.0:
                        note(okey, "polymarket-us", "no-ask")
                    else:
                        note(okey, "polymarket-us", "matched")
                        out.append({
                            "market_id": counterpart,
                            "venue": "polymarket-us",
                            "origin_market_id": t.get("market_id", ""),
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
