"""Kalshi vs Polymarket disagreement - a second, non-whale source of ideas.

Omen quotes *global Polymarket*, so Kalshi is not somewhere you trade; it is an
independent read on the same event by a completely different crowd (CFTC-regulated
US retail, dollar-funded) than Polymarket's (global, crypto-funded). When those two
price the same question far apart, at least one of them is wrong, and that is worth
a human's attention - especially on politics and economics, where the whale trade
feed is thin because that flow accumulates quietly instead of arriving as one big
taker order.

THE RISK IS MIS-MATCHING, NOT MISPRICING. Two markets can share a headline and settle
on different rules - different cutoff dates, different sources, different definitions
of "wins". A bad match invents an edge that was never there. So matching demands high
token overlap AND close resolution dates, every ticket carries the similarity score
and both titles, and the warning to read both rule texts is not optional.

All reads here are public and unauthenticated on both venues.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA = "https://gamma-api.polymarket.com"

# Auto-generated multivariate parlays flood Kalshi's market list and match nothing.
_JUNK = ("MVE",)
_SPORTS = ("NBA", "NFL", "MLB", "NHL", "EPL", "GAME", "TENNIS", "UFC", "SOCCER",
           "F1", "GOLF", "NASCAR", "WNBA", "CFB", "MLS", "ATP", "WTA")

_STOP = {
    "will", "the", "a", "an", "be", "is", "are", "was", "were", "to", "of", "in",
    "on", "at", "by", "for", "and", "or", "if", "then", "than", "that", "this",
    "before", "after", "any", "all", "who", "what", "when", "which", "how", "many",
    "much", "more", "less", "least", "most", "next", "new", "become", "becomes",
    "have", "has", "had", "do", "does", "did", "get", "gets", "there", "it",
}


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class VenueMarket:
    venue: str          # "kalshi" | "polymarket"
    market_id: str
    title: str
    yes_price: float    # 0..1
    end_iso: str
    volume: float
    url: str


@dataclass(frozen=True)
class Divergence:
    """One event, priced differently on the two venues."""

    kalshi: VenueMarket
    poly: VenueMarket
    similarity: float
    gap: float          # kalshi_yes - poly_yes; positive => Polymarket YES looks cheap

    @property
    def buy_yes(self) -> bool:
        return self.gap > 0

    @property
    def entry_price(self) -> float:
        """What you'd pay on Omen for the side Kalshi implies is underpriced."""
        return self.poly.yes_price if self.buy_yes else round(1.0 - self.poly.yes_price, 3)


# Words that ARE the question. Two titles can share almost every token and still ask
# different things: "who will RUN FOR the Democratic nomination" (62c) vs "will AOC WIN
# the Democratic primary" (14c) overlap at 0.88, and the 48c "gap" is simply correct.
# Same for NOMINEE vs winning the PRESIDENCY. If two titles pick different terms from
# one group, they are different questions no matter how similar the words look.
_DECISIVE = (
    {"win", "wins", "winner", "won"},
    {"nominee", "nomination", "nominated"},
    {"run", "runs", "running", "candidate", "declare", "announce"},
    {"resign", "resigns", "resignation", "out", "leave", "leaves", "fired"},
    {"pardon", "pardons", "pardoned"},
    {"indicted", "indictment", "convicted", "conviction", "arrested"},
)


def question_conflict(a: str, b: str) -> bool:
    """True when the two titles choose different terms from the same decisive group."""
    ta, tb = tokens(a), tokens(b)
    for group in _DECISIVE:
        ga, gb = ta & group, tb & group
        if ga and gb and not (ga & gb):
            return True          # both name the concept, but differently
        if bool(ga) != bool(gb):
            return True          # one asks about it, the other does not
    return False


def tokens(title: str) -> set[str]:
    """Significant words - lowercase, punctuation stripped, stop-words removed."""
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    return {w for w in words if w not in _STOP and len(w) > 2}


def similarity(a: str, b: str) -> float:
    """Overlap coefficient: |A∩B| / min(|A|,|B|).

    Deliberately not Jaccard - Kalshi titles are terse ("Fed hikes in March") where
    Polymarket's are verbose, and Jaccard would punish that length gap even when one
    title fully contains the other's meaning.
    """
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _hours_between(a_iso: str, b_iso: str) -> float | None:
    def parse(s):
        try:
            d = datetime.fromisoformat((s or "").replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            return None
    a, b = parse(a_iso), parse(b_iso)
    if a is None or b is None:
        return None
    return abs((a - b).total_seconds()) / 3600.0


def fetch_kalshi(*, pages: int = 4, per_page: int = 200, fetch=None,
                 include_sports: bool = False) -> list[VenueMarket]:
    """Open, genuinely-quoted Kalshi markets. Public endpoint, no auth."""
    getter = fetch or _requests_get
    out: list[VenueMarket] = []
    cursor = None
    for _ in range(pages):
        params = {"status": "open", "with_nested_markets": "true", "limit": per_page}
        if cursor:
            params["cursor"] = cursor
        try:
            data = getter(f"{KALSHI_API}/events", params)
        except Exception:  # noqa: BLE001
            break
        events = data.get("events") or []
        for ev in events:
            tick = ev.get("event_ticker") or ""
            if any(j in tick for j in _JUNK):
                continue
            if not include_sports and any(s in tick.upper() for s in _SPORTS):
                continue
            for m in (ev.get("markets") or []):
                bid = _f(m.get("yes_bid_dollars"))
                ask = _f(m.get("yes_ask_dollars"))
                if bid <= 0 and ask <= 0:
                    continue          # no book = no opinion, not a reference price
                mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else (bid or ask)
                title = ev.get("title") or m.get("title") or ""
                sub = m.get("yes_sub_title") or ""
                out.append(VenueMarket(
                    venue="kalshi", market_id=m.get("ticker") or "",
                    title=f"{title} {sub}".strip(), yes_price=round(mid, 3),
                    end_iso=m.get("expiration_time") or m.get("close_time") or "",
                    volume=_f(m.get("volume_fp")),
                    url=f"https://kalshi.com/markets/{tick}",
                ))
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def fetch_polymarket(*, limit: int = 500, min_volume: float = 20_000.0,
                     fetch=None) -> list[VenueMarket]:
    """Active, liquid Polymarket markets - the universe Omen actually quotes."""
    import json as _json
    getter = fetch or _requests_get
    out: list[VenueMarket] = []
    # Gamma caps a page at 100 no matter what `limit` says, so walk it with offset.
    rows: list = []
    page = 100
    for offset in range(0, max(page, limit), page):
        try:
            batch = getter(f"{GAMMA}/markets",
                           {"active": "true", "closed": "false",
                            "limit": page, "offset": offset})
        except Exception:  # noqa: BLE001
            break
        batch = batch if isinstance(batch, list) else (batch.get("data") or [])
        if not batch:
            break
        rows += batch
        if len(batch) < page:
            break
    for m in rows:
        vol = _f(m.get("volume"))
        if vol < min_volume:
            continue
        prices = m.get("outcomePrices")
        if isinstance(prices, str):
            try:
                prices = _json.loads(prices)
            except Exception:  # noqa: BLE001
                prices = None
        if not prices:
            continue
        out.append(VenueMarket(
            venue="polymarket", market_id=m.get("conditionId") or "",
            title=m.get("question") or "", yes_price=round(_f(prices[0]), 3),
            end_iso=m.get("endDate") or "", volume=vol,
            url=("https://polymarket.com/event/" + m["slug"]) if m.get("slug") else "",
        ))
    return out


def find_divergences(kalshi: list[VenueMarket], poly: list[VenueMarket], *,
                     min_similarity: float = 0.75, min_gap: float = 0.08,
                     max_end_hours: float = 72.0,
                     price_band: tuple[float, float] = (0.05, 0.95)
                     ) -> list[Divergence]:
    """Same-event pairs whose prices disagree by more than ``min_gap``."""
    out: list[Divergence] = []
    for p in poly:
        if not (price_band[0] <= p.yes_price <= price_band[1]):
            continue
        best: tuple[float, VenueMarket] | None = None
        for k in kalshi:
            if not (price_band[0] <= k.yes_price <= price_band[1]):
                continue
            sim = similarity(k.title, p.title)
            if sim < min_similarity:
                continue
            if question_conflict(k.title, p.title):
                continue    # same words, different question - the fake-edge trap
            # Same headline, different deadline is a different question.
            dh = _hours_between(k.end_iso, p.end_iso)
            if dh is not None and dh > max_end_hours:
                continue
            if best is None or sim > best[0]:
                best = (sim, k)
        if best is None:
            continue
        sim, k = best
        gap = round(k.yes_price - p.yes_price, 3)
        if abs(gap) < min_gap:
            continue
        out.append(Divergence(kalshi=k, poly=p, similarity=round(sim, 2), gap=gap))
    out.sort(key=lambda d: abs(d.gap), reverse=True)
    return out


def _requests_get(url: str, params: dict):
    import requests
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()


def main(argv: list[str] | None = None) -> int:
    """python -m predictionedge.crossvenue [--show-rejected]

    Prints genuine Kalshi/Polymarket disagreements. Expect zero most days: the
    guard throws out same-words-different-question pairs, which is nearly all of them.
    """
    import argparse
    ap = argparse.ArgumentParser(prog="predictionedge.crossvenue")
    ap.add_argument("--min-similarity", type=float, default=0.75)
    ap.add_argument("--min-gap", type=float, default=0.08)
    ap.add_argument("--show-rejected", action="store_true",
                    help="list pairs the question-conflict guard threw out")
    args = ap.parse_args(argv)

    k, p = fetch_kalshi(), fetch_polymarket()
    print(f"kalshi quoted non-sports: {len(k)}   polymarket active liquid: {len(p)}")

    if args.show_rejected:
        print("\n-- rejected as different questions (would have been fake edges) --")
        n = 0
        for pm in p:
            for km in k:
                if similarity(km.title, pm.title) >= args.min_similarity \
                        and question_conflict(km.title, pm.title):
                    print(f"  K={km.yes_price:.2f} P={pm.yes_price:.2f} "
                          f"| {km.title[:46]} || {pm.title[:46]}")
                    n += 1
                    break
            if n >= 15:
                break

    div = find_divergences(k, p, min_similarity=args.min_similarity,
                           min_gap=args.min_gap)
    print(f"\n{len(div)} genuine divergence(s):")
    for d in div:
        print(f"  gap {d.gap:+.2f}  kalshi={d.kalshi.yes_price:.2f} "
              f"polymarket={d.poly.yes_price:.2f}  sim={d.similarity:.2f}")
        print(f"    -> {'BUY YES' if d.buy_yes else 'BUY NO'} at {d.entry_price:.2f}")
        print(f"    K: {d.kalshi.title[:74]}")
        print(f"    P: {d.poly.title[:74]}")
        print("    VERIFY BOTH RULE TEXTS BEFORE TRADING.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
