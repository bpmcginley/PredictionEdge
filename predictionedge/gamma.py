"""Resolve Polymarket human slugs to condition_ids (the Data API's market key).

The whale map needs condition_ids (0x + 64 hex), but humans know slugs. This calls
the public Gamma API to translate, and can build a whale_map.json from a
{kalshi_ticker: polymarket_slug} file.

    python -m predictionedge.gamma <slug>                        # print condition_id
    python -m predictionedge.gamma --build pairs.json out.json   # ticker->slug -> map
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com"
# Max condition_ids per metadata request. Gamma rejects the call outright past roughly
# this many repeated params, so keep it well clear of the edge.
_META_CHUNK = 20


def _default_fetch(url: str, params: dict):
    import requests
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def resolve_condition_id(slug: str, *, base: str = GAMMA, fetch=None) -> str | None:
    """Return the condition_id for a Polymarket market slug, or None."""
    try:
        data = (fetch or _default_fetch)(f"{base}/markets", {"slug": slug})
    except Exception:  # noqa: BLE001
        return None
    rows = data if isinstance(data, list) else (data.get("data") or data.get("markets") or [])
    for m in rows:
        cid = m.get("conditionId") or m.get("condition_id")
        if cid:
            return cid
    return None


def end_dates(condition_ids: list[str], *, base: str = GAMMA, fetch=None) -> dict[str, str]:
    """Batch lookup of market end dates (ISO strings) keyed by condition_id."""
    ids = [c for c in condition_ids if c]
    if not ids:
        return {}
    # Gamma wants repeated condition_ids params, not a comma-joined value.
    params = [("condition_ids", c) for c in ids] + [("limit", len(ids) + 5)]
    try:
        data = (fetch or _default_fetch)(f"{base}/markets", params)
    except Exception:  # noqa: BLE001
        return {}
    rows = data if isinstance(data, list) else (data.get("data") or data.get("markets") or [])
    out: dict[str, str] = {}
    for m in rows:
        cid = m.get("conditionId") or m.get("condition_id")
        ed = m.get("endDate") or m.get("endDateIso") or m.get("end_date_iso")
        if cid and ed:
            out[cid] = ed
    return out


def market_meta(condition_ids: list[str], *, base: str = GAMMA, fetch=None,
                failures: set[str] | None = None) -> dict[str, dict]:
    """Batch market metadata keyed by condition_id.

    Pass ``failures`` to learn which ids could not be READ, as opposed to the ones
    Gamma answered about and does not have. Both are missing from the result and the
    difference matters: one is a network fault to fix, the other is a market to skip.

    Beyond the end date this carries the two fields that decide whether a signal is
    *tradeable advice* rather than noise: ``game_start`` (a sports market whose kickoff
    has passed is in-play - copying whales there is buying their exhaust) and the live
    book (``best_ask``), which tells us how far price has run since the whale filled.
    """
    ids = [c for c in condition_ids if c]
    if not ids:
        return {}
    # One repeated param per id, so a large batch overruns what Gamma will accept and
    # the whole call fails - which is silent and catastrophic here, because a market
    # with no metadata skips every date check and then dies as "unpriceable". Chunk it.
    rows: list = []
    getter = fetch or _default_fetch

    def _chunk(cids: list[str]) -> None:
        """Fetch one batch, halving on failure, and record ids that never resolved.

        The bare `continue` this replaces made a DROPPED BATCH indistinguishable from
        "these markets do not exist" - 20 signals at a time vanished under a rejection
        reason that was false. Two different failures hide in one call: a batch too
        large for Gamma (which a bisect fixes) and a genuine outage (which it cannot),
        so a chunk that fails alone is reported as unread rather than assumed absent.
        """
        params = [("condition_ids", c) for c in cids] + [("limit", len(cids) + 5)]
        try:
            data = getter(f"{base}/markets", params)
        except Exception:  # noqa: BLE001
            if len(cids) > 1:
                mid = len(cids) // 2
                _chunk(cids[:mid])
                _chunk(cids[mid:])
            else:
                unread.add(cids[0])
            return
        rows.extend(data if isinstance(data, list)
                    else (data.get("data") or data.get("markets") or []))

    unread: set[str] = set()
    for i in range(0, len(ids), _META_CHUNK):
        _chunk(ids[i:i + _META_CHUNK])
    if unread:
        # Loud on purpose. Silence here is the failure mode: the signal is still gone
        # either way, but only a logged count tells you whether the funnel is filtering
        # or leaking. If this is a large share of a run, the board is under-fed by a
        # network problem and not by a shortage of ideas.
        log.warning("gamma metadata unread for %d/%d markets", len(unread), len(ids))
    if failures is not None:
        failures.update(unread)
    out: dict[str, dict] = {}
    for m in rows:
        cid = m.get("conditionId") or m.get("condition_id")
        if not cid:
            continue
        prices = m.get("outcomePrices")
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except Exception:  # noqa: BLE001
                prices = None
        outcomes = m.get("outcomes")
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except Exception:  # noqa: BLE001
                outcomes = None
        out[cid] = {
            "question": m.get("question") or m.get("title") or "",
            "slug": m.get("slug") or "",
            "end_date": m.get("endDate") or m.get("endDateIso") or "",
            # `gameStartTime` ONLY. `startDate` is when the market was CREATED, and it
            # is therefore always in the past - falling back to it made every market
            # without a kickoff (politics, crypto, econ, world events: everything that
            # is not a scheduled fixture) read as "in-play (event already started)".
            # That single `or` clause structurally deleted those categories from the
            # board, which is why every live ticket was baseball or tennis while the
            # leaderboard scan was spending its calls on POLITICS and CRYPTO. Worse,
            # the rejection ledger - the board's whole honesty claim - then printed a
            # reason that was false, and a ledger that misattributes retires the wrong
            # hypothesis. Absent is absent; the caller decides what that means.
            "game_start": m.get("gameStartTime") or "",
            "created_at": m.get("startDate") or "",
            "closed": bool(m.get("closed")),
            "active": bool(m.get("active", True)),
            "volume": float(m.get("volume") or 0.0),
            "liquidity": float(m.get("liquidity") or 0.0),
            "best_bid": float(m.get("bestBid") or 0.0),
            "best_ask": float(m.get("bestAsk") or 0.0),
            "yes_price": float(prices[0]) if prices else 0.0,
            # Kept as parallel lists so a caller can price the *specific* outcome a
            # trader bought. Comparing a named team against the YES price is how you
            # end up reporting a 40c move that never happened.
            "outcomes": [str(o) for o in outcomes] if outcomes else [],
            "prices": [float(p) for p in prices] if prices else [],
        }
    return out


def outcome_price(meta: dict, outcome: str) -> float | None:
    """Current price of one named outcome, or None if it can't be identified."""
    names = meta.get("outcomes") or []
    prices = meta.get("prices") or []
    want = (outcome or "").strip().lower()
    for name, price in zip(names, prices):
        if name.strip().lower() == want:
            return price
    return None


def build_whale_map(ticker_to_slug: dict[str, str], *, base: str = GAMMA, fetch=None) -> dict[str, str]:
    """Translate { kalshi_ticker: pm_slug } into { kalshi_ticker: condition_id }."""
    out: dict[str, str] = {}
    for ticker, slug in ticker_to_slug.items():
        cid = resolve_condition_id(slug, base=base, fetch=fetch)
        if cid:
            out[ticker] = cid
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="predictionedge.gamma")
    ap.add_argument("slug", nargs="?", help="a Polymarket market slug")
    ap.add_argument("--build", nargs=2, metavar=("PAIRS_JSON", "OUT_JSON"),
                    help="{ticker: slug} JSON -> {ticker: condition_id} JSON")
    args = ap.parse_args(argv)

    if args.build:
        pairs = json.loads(Path(args.build[0]).read_text(encoding="utf-8"))
        mapping = build_whale_map(pairs)
        Path(args.build[1]).write_text(json.dumps(mapping, indent=2), encoding="utf-8")
        print(f"wrote {len(mapping)}/{len(pairs)} condition_ids to {args.build[1]}")
        return 0

    if not args.slug:
        ap.error("provide a slug or --build PAIRS_JSON OUT_JSON")
    cid = resolve_condition_id(args.slug)
    print(cid or "(not found)")
    return 0 if cid else 1


if __name__ == "__main__":
    raise SystemExit(main())
