"""Every forecast, whether or not it produced a ticket. The unbiased sample.

WHY THIS IS A SEPARATE THING FROM THE TRIAL. The paper trial records what we BET, so
it only ever sees days where the forecast disagreed with the market by more than the
edge bar. Fitting the forecast's error from that sample would measure the error
CONDITIONAL ON HAVING DISAGREED, which is a different and much worse-behaved quantity -
the same selection trap as judging a screen by the stocks it bought. So this logs every
city-day the model looked at, including the overwhelming majority where it stayed quiet.

WHAT COUNTS AS TRUTH HERE. Not the hourly station observations: those are spot readings
roughly an hour apart and they MISS the intraday peak. Measured 2026-08-11 against
Kalshi's own settlements, hourly-derived highs ran 0-3F below the bracket that actually
paid - Denver 8/8 read 98.1F from the hourly feed while the market settled on 100-101F.
Truth is the CLI report, and Kalshi's settled ladder is a free, already-wired mirror of
it: exactly one bracket pays, and that bracket is the reported high. So resolution here
reads `kalshi.market_meta`, the same call settlement already makes.

The payoff is a per-city bias and a per-city sigma fitted from data, replacing the two
guesses in `weather.py`. Until then the log is the only thing standing between this
sleeve and another `macrofv` incident.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .weather import CITIES, _event_day, _mid, market_distribution, forecast_highs

DEFAULT_PATH = "docs/wxlog.json"


def _key(series: str, day: str) -> str:
    return f"{series}:{day}"


def load(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {"rows": {}, "generated": ""}
    data = json.loads(p.read_text(encoding="utf-8"))
    data.setdefault("rows", {})
    return data


def save(path: str | Path, log: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    log["generated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    p.write_text(json.dumps(log, indent=2, sort_keys=True), encoding="utf-8")


def observe(log: dict, *, cities=None, kalshi_fetch=None, nws_fetch=None,
            now: float | None = None) -> int:
    """Record today's view of every upcoming city-day. Returns rows touched.

    FIRST SIGHTING WINS on the forecast fields. A forecast four days out and the same
    forecast the morning of are different claims of very different difficulty, and
    overwriting would silently relabel every hard call as an easy one by the time it
    resolves. `last_*` keeps the freshest read alongside it, so lead-time decay can be
    fitted later without contaminating the headline number.
    """
    from .kalshi import KALSHI_API, _default_fetch as kx_fetch, _price_to_dollars

    getter = kalshi_fetch or kx_fetch
    now = time.time() if now is None else now
    touched = 0

    for city in (cities or CITIES).values():
        try:
            highs = forecast_highs(city, fetch=nws_fetch)
        except Exception:  # noqa: BLE001
            continue
        if not highs:
            continue
        try:
            data = getter(f"{KALSHI_API}/markets",
                          {"series_ticker": city.series, "limit": 200})
        except Exception:  # noqa: BLE001
            continue

        ladders: dict[str, list[dict]] = {}
        for m in data.get("markets") or []:
            if m.get("status") not in ("active", "open"):
                continue
            bid, ask = _price_to_dollars(m, "yes_bid"), _price_to_dollars(m, "yes_ask")
            ladders.setdefault(m.get("event_ticker", ""), []).append({
                "floor_strike": m.get("floor_strike"), "cap_strike": m.get("cap_strike"),
                "ticker": m.get("ticker", ""), "mid": _mid(bid, ask)})

        for event, ladder in ladders.items():
            day = _event_day(event)
            if not day or day not in highs:
                continue
            market = market_distribution(ladder)
            key = _key(city.series, day)
            row = log["rows"].setdefault(key, {
                "series": city.series, "city": city.name, "day": day,
                "event_ticker": event, "settles_by": city.cli_url,
                "first_seen_at": now, "first_forecast_f": round(highs[day], 2),
                "first_market_mu_f": market and round(market[0], 2),
                "first_market_sigma_f": market and round(market[1], 2),
                # Filled in by `resolve` once the ladder settles. Present and null from
                # the start so a missing resolution reads as pending, not as absent.
                "observed_f": None, "observed_bracket": None, "resolved_at": None,
            })
            row["last_seen_at"] = now
            row["last_forecast_f"] = round(highs[day], 2)
            row["last_market_mu_f"] = market and round(market[0], 2)
            row["last_market_sigma_f"] = market and round(market[1], 2)
            touched += 1
    return touched


def resolve(log: dict, *, fetch=None, now: float | None = None,
            today: str | None = None) -> int:
    """Fill in what the CLI actually reported, via Kalshi's settled ladder.

    The reported high is the bracket that paid. A two-degree bracket pins it to a
    one-degree midpoint, and an open-ended tail pins it only to a bound - so the bound
    is recorded and `observed_f` is left null rather than invented, because a fitted
    bias built on made-up tail values would be worse than a smaller honest sample.

    ONLY PAST DAYS ARE QUERIED. This runs on the board's ~15-minute cadence, and the log
    holds every future day the model has looked at - so querying unresolved rows blindly
    would fire one Kalshi call per pending future day, ninety-six times a day, forever,
    to be told each time that tomorrow has not happened yet. A past-day row resolves
    within a run or two and then stops being asked.
    """
    from .kalshi import KALSHI_API, _default_fetch as kx_fetch

    getter = fetch or kx_fetch
    now = time.time() if now is None else now
    today = today or time.strftime("%Y-%m-%d", time.gmtime(now))
    filled = 0
    for row in log["rows"].values():
        if row.get("resolved_at") or not row.get("event_ticker"):
            continue
        if row.get("day", "") >= today:
            continue                   # not observable yet; asking buys nothing
        try:
            data = getter(f"{KALSHI_API}/markets",
                          {"event_ticker": row["event_ticker"], "limit": 100})
        except Exception:  # noqa: BLE001
            continue
        won = [m for m in data.get("markets") or [] if m.get("result") == "yes"]
        if len(won) != 1:
            continue                   # unsettled, void, or disputed: leave it pending
        m = won[0]
        floor, cap = m.get("floor_strike"), m.get("cap_strike")
        row["observed_bracket"] = m.get("yes_sub_title") or m.get("ticker", "")
        row["observed_f"] = ((float(floor) + float(cap)) / 2.0
                             if floor is not None and cap is not None else None)
        row["resolved_at"] = now
        filled += 1
    return filled


def summarise(log: dict) -> dict:
    """Per-city forecast bias and error spread, from resolved rows only.

    `bias_f` is forecast minus observed: POSITIVE means the grid runs warm against the
    settling thermometer, which is the Central Park hypothesis this log exists to test.
    `n` is small for a long time and the numbers are meaningless until it is not - which
    is why they are reported next to it rather than on their own.
    """
    out: dict[str, dict] = {}
    for row in log["rows"].values():
        if row.get("observed_f") is None or row.get("first_forecast_f") is None:
            continue
        errs = out.setdefault(row["series"], {"city": row["city"], "errors": []})
        errs["errors"].append(row["first_forecast_f"] - row["observed_f"])
    for series, d in out.items():
        e = d.pop("errors")
        n = len(e)
        bias = sum(e) / n
        var = sum((x - bias) ** 2 for x in e) / (n - 1) if n > 1 else 0.0
        d.update(n=n, bias_f=round(bias, 2), sigma_f=round(var ** 0.5, 2),
                 mae_f=round(sum(abs(x) for x in e) / n, 2))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="predictionedge.weatherlog")
    ap.add_argument("--log", default=DEFAULT_PATH)
    ap.add_argument("--no-resolve", action="store_true")
    args = ap.parse_args(argv)

    log = load(args.log)
    touched = observe(log)
    filled = 0 if args.no_resolve else resolve(log)
    log["summary"] = summarise(log)
    save(args.log, log)

    pending = sum(1 for r in log["rows"].values() if r.get("observed_f") is None)
    print(f"wxlog: {touched} city-day(s) observed, {filled} resolved, "
          f"{len(log['rows'])} total, {pending} pending")
    for series, d in sorted(log["summary"].items()):
        print(f"  {series:<10} n={d['n']:<4} bias {d['bias_f']:+.2f}F  "
              f"sigma {d['sigma_f']:.2f}F  mae {d['mae_f']:.2f}F")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
