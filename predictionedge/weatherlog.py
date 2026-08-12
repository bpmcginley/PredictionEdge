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
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from .weather import (CITIES, USER_AGENT, _event_day, _mid, market_distribution,
                      forecast_highs)

DEFAULT_PATH = "docs/wxlog.json"

NWS_PRODUCTS = "https://api.weather.gov/products"


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


# --- reading the CLI report itself ------------------------------------------------
#
# The settled ladder pins the reported high to a bracket; the CLI text states the
# number. For an interior bracket the difference is half a degree of precision, but an
# open-ended winner - exactly the big forecast misses a calibration most needs - pins
# nothing, and before this existed those days were CENSORED out of the fitted bias
# (one row proved it: NYC, reported high 85F, logged as null). Truncating the largest
# errors out of an error estimate does not merely shrink the sample, it biases sigma
# low, which is the one direction this sleeve cannot afford.

_MONTHS = {m: i for i, m in enumerate(
    ("JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST",
     "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"), start=1)}
# "...THE CENTRAL PARK NY CLIMATE SUMMARY FOR AUGUST 11 2026..." - the report names
# the day it covers, and matching on that line (never on issuance time alone) is what
# keeps an evening report for the wrong day from being read as truth.
_SUMMARY_RE = re.compile(r"CLIMATE SUMMARY FOR\s+([A-Z]+)\s+(\d{1,2})\s+(\d{4})")
# First column after the label is the observed value; "MM" (not observed) fails the
# digit match and correctly yields nothing rather than a guess.
_TEMP_RE = {"high": re.compile(r"^\s*MAXIMUM\s+(-?\d+)\b", re.MULTILINE),
            "low": re.compile(r"^\s*MINIMUM\s+(-?\d+)\b", re.MULTILINE)}


def _cli_json(url: str, params: dict | None = None) -> dict:
    import requests
    r = requests.get(url, params=params or {}, timeout=20,
                     headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    r.raise_for_status()
    return r.json()


def _next_day(day: str) -> str:
    return (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def _summary_day(text: str) -> str | None:
    m = _SUMMARY_RE.search(text)
    if not m:
        return None
    month = _MONTHS.get(m.group(1))
    if not month:
        return None
    return f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(2)):02d}"


def cli_observed(cli_code: str, day: str, *, kind: str = "high",
                 fetch=None) -> float | None:
    """The exact temperature the CLI reported for `day`, or None.

    A day's summary is issued twice - preliminary that evening, final the next
    morning - so only issuances dated `day` or the day after are worth fetching, and
    they are scanned newest-first so the final (or a correction) wins over the
    preliminary. Any failure returns None: the bracket fallback in `resolve` is
    honest, a guessed temperature is not.
    """
    getter = fetch or _cli_json
    try:
        graph = (getter(f"{NWS_PRODUCTS}/types/CLI/locations/{cli_code}")
                 or {}).get("@graph") or []
    except Exception:  # noqa: BLE001
        return None
    for item in graph:
        issued = str(item.get("issuanceTime") or "")[:10]
        if issued not in (day, _next_day(day)):
            continue
        try:
            text = (getter(f"{NWS_PRODUCTS}/{item.get('id')}")
                    or {}).get("productText") or ""
        except Exception:  # noqa: BLE001
            continue
        if _summary_day(text) != day:
            continue
        m = _TEMP_RE.get(kind, _TEMP_RE["high"]).search(text)
        if m:
            return float(m.group(1))
    return None


def _in_bracket(temp: float, floor, cap) -> bool:
    """Whole-degree bracket semantics, as `weather.bracket_probability` documents:
    "85-86" is the event {85, 86}, ">90" pays 91 and above, "<83" pays 82 and below."""
    if floor is not None and cap is not None:
        return float(floor) <= temp <= float(cap)
    if cap is not None:
        return temp < float(cap)
    if floor is not None:
        return temp > float(floor)
    return False


def resolve(log: dict, *, fetch=None, cli_fetch=None, cities=None,
            now: float | None = None, today: str | None = None) -> int:
    """Fill in what the CLI actually reported, via Kalshi's settled ladder.

    The settled ladder is the TRIGGER and the validator; the CLI text is the value.
    Kalshi deciding the market is what proves the day is truly resolved, then the CLI
    report supplies the exact temperature. A CLI read that lands OUTSIDE the bracket
    that paid is a wrong report, a wrong parse, or a wrong day, and is discarded -
    whichever of the two sources is lying, a number they disagree on must not enter
    the calibration sample. Without a CLI value an interior bracket falls back to its
    one-degree midpoint, and an open-ended tail records its bound with `observed_f`
    left null (see `backfill_tails` for the retry) rather than invented, because a
    fitted bias built on made-up tail values would be worse than a smaller honest
    sample.

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
        # Bounds ride along so `backfill_tails` can validate a late CLI read against
        # the bracket that paid without re-asking Kalshi.
        row["bracket_floor"], row["bracket_cap"] = floor, cap
        city = (cities or CITIES).get(row.get("series", ""))
        exact = cli_observed(city.cli, row["day"], kind=city.kind,
                             fetch=cli_fetch) if city else None
        if exact is not None and _in_bracket(exact, floor, cap):
            row["observed_f"], row["observed_src"] = exact, "cli"
        else:
            mid = ((float(floor) + float(cap)) / 2.0
                   if floor is not None and cap is not None else None)
            row["observed_f"] = mid
            row["observed_src"] = "bracket" if mid is not None else None
        row["resolved_at"] = now
        filled += 1
    return filled


def backfill_tails(log: dict, *, cli_fetch=None, cities=None,
                   now: float | None = None, window_days: int = 7) -> int:
    """Retry the CLI read for resolved rows the bracket could not pin. Returns fills.

    A tail winner resolves the moment Kalshi settles, which can be hours before the
    final CLI report is issued - so the first attempt in `resolve` may honestly find
    nothing, and without a retry the day is censored after all. The window is bounded
    because the products feed only serves recent issuances and a row that old has
    missed every edition it will ever get.
    """
    now = time.time() if now is None else now
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(now - window_days * 86400))
    filled = 0
    for row in log["rows"].values():
        if not row.get("resolved_at") or row.get("observed_f") is not None:
            continue
        if row.get("day", "") < cutoff:
            continue
        city = (cities or CITIES).get(row.get("series", ""))
        if city is None:
            continue
        exact = cli_observed(city.cli, row["day"], kind=city.kind, fetch=cli_fetch)
        if exact is None:
            continue
        floor, cap = row.get("bracket_floor"), row.get("bracket_cap")
        # Rows resolved before bounds were stored cannot be re-checked against the
        # ladder; the summary-line day match is the remaining validation, and the CLI
        # is the settlement source itself, so its value stands.
        if (floor is not None or cap is not None) and not _in_bracket(exact, floor, cap):
            continue
        row["observed_f"], row["observed_src"] = exact, "cli"
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
    filled = backfilled = 0
    if not args.no_resolve:
        filled = resolve(log)
        backfilled = backfill_tails(log)
    log["summary"] = summarise(log)
    save(args.log, log)

    pending = sum(1 for r in log["rows"].values() if r.get("observed_f") is None)
    print(f"wxlog: {touched} city-day(s) observed, {filled} resolved, "
          f"{backfilled} tail(s) backfilled, {len(log['rows'])} total, "
          f"{pending} pending")
    for series, d in sorted(log["summary"].items()):
        print(f"  {series:<10} n={d['n']:<4} bias {d['bias_f']:+.2f}F  "
              f"sigma {d['sigma_f']:.2f}F  mae {d['mae_f']:.2f}F")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
