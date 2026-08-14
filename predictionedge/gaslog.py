"""Every AAA print and every gas forecast, whether or not it produced a ticket.

The gas sleeve's analogue of `weatherlog.py`, and it exists for the same reason: the
paper trial records what we BET, so it only sees days where the forecast disagreed
with the market by more than the edge bar. Fitting drift or sigma from that sample
would measure them CONDITIONAL ON HAVING DISAGREED - the same selection trap as
judging a screen by the stocks it bought. This logs every AAA print and every
forecast the model issued, including the majority of days it stayed quiet.

It is also the sleeve's ONLY source of width. `weather.py` had to take sigma from
the ladder because its settlement variable is hard to know a priori; here the
settlement series itself is free and observable daily, so sigma is measured from
the print's own day-over-day deltas accumulated in this file. That makes the log
load-bearing from day one, not merely a calibration side-channel: until it holds
`min_deltas` consecutive-day moves the sleeve emits nothing at all.

TRUTH IS SIMPLER THAN WEATHER'S. The settlement source publishes its own history
one day at a time, so resolution needs no Kalshi settlement call: the observed
value for day X is simply the print recorded under X once it exists. `days` holds
the prints (keyed by the page's own "Price as of" date), `rows` holds the
forecasts, and `resolve` marries them.

State lives in docs/gaslog.json for the same reason docs/wxlog.json does: the
scheduled workflow checks out fresh each run and commits `docs/`, so writing there
is what lets the history accumulate.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_PATH = "docs/gaslog.json"


def load(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {"days": {}, "rows": {}, "generated": ""}
    data = json.loads(p.read_text(encoding="utf-8"))
    data.setdefault("days", {})
    data.setdefault("rows", {})
    return data


def save(path: str | Path, log: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    log["generated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    p.write_text(json.dumps(log, indent=2, sort_keys=True), encoding="utf-8")


def _prev_day(day: str) -> str:
    return (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


def record_snapshot(log: dict, snap: dict, *, fallback_day: str | None = None) -> int:
    """File the page's prints under their calendar days. Returns days touched.

    The current print is keyed by the page's own "Price as of" date (`as_of`),
    falling back to the caller's date only when the stamp failed to parse - our
    clock and AAA's early-morning refresh are not the same thing. Today's value
    overwrites (the freshest read of the page is the freshest truth for that day);
    the page's `yesterday` figure only BACKFILLS a day we never saw directly,
    because a print observed on its own day outranks the next day's echo of it.
    """
    if not snap or snap.get("current") is None:
        return 0
    day = snap.get("as_of") or fallback_day
    if not day:
        return 0
    touched = 0
    days = log.setdefault("days", {})
    if days.get(day) != snap["current"]:
        days[day] = snap["current"]
        touched += 1
    yday = _prev_day(day)
    if snap.get("yesterday") is not None and yday not in days:
        days[yday] = snap["yesterday"]
        touched += 1
    return touched


def consecutive_deltas(log: dict) -> list[float]:
    """Day-over-day moves, oldest first, from CONSECUTIVE calendar days only.

    A gap (scrape outage, missed run) yields no delta rather than a multi-day move
    dressed as a daily one - a two-day 1c move logged as a one-day delta would
    inflate sigma and understate autocorrelation at the same time.
    """
    days = log.get("days") or {}
    out: list[float] = []
    for day in sorted(days):
        prev = _prev_day(day)
        if prev in days:
            out.append(float(days[day]) - float(days[prev]))
    return out


def drift_sigma(log: dict, *, ewma_window: int = 5, sigma_window: int = 10,
                sigma_floor: float = 0.0015,
                min_deltas: int = 3) -> tuple[float, float] | None:
    """(drift, sigma) from observed history, or None if there is not enough of it.

    Drift is an EWMA of the daily deltas (span `ewma_window`), which is the
    persistence hypothesis in one number: recent momentum, discounted smoothly.
    Sigma is the sample standard deviation of the last `sigma_window` deltas,
    FLOORED at `sigma_floor` - three quiet days must not let the model claim it
    knows tomorrow's print to a hundredth of a cent, which is exactly the too-tight
    sigma that invented edge in `macrofv` and nearly did in `weather`. Below
    `min_deltas` observed moves the answer is None: no history, no opinion.
    """
    deltas = consecutive_deltas(log)
    if len(deltas) < max(1, min_deltas):
        return None
    alpha = 2.0 / (ewma_window + 1.0)
    drift = deltas[0]
    for d in deltas[1:]:
        drift = alpha * d + (1.0 - alpha) * drift
    recent = deltas[-sigma_window:]
    mean = sum(recent) / len(recent)
    var = (sum((d - mean) ** 2 for d in recent) / (len(recent) - 1)
           if len(recent) > 1 else 0.0)
    return drift, max(var ** 0.5, sigma_floor)


def record_forecast(log: dict, day: str, *, forecast: float, aaa_today: float,
                    drift: float, sigma: float, made_at: float) -> None:
    """One row per target day, FIRST SIGHTING WINS on the forecast fields.

    A forecast two days out and the same forecast the morning of are claims of
    different difficulty; overwriting would relabel every hard call as an easy one
    by the time it resolves. `last_*` keeps the freshest read alongside so
    lead-time decay can be fitted later without contaminating the headline number.
    """
    row = log.setdefault("rows", {}).setdefault(day, {
        "day": day,
        "first_made_at": made_at,
        "first_forecast": round(forecast, 4),
        "first_aaa_today": round(aaa_today, 4),
        "first_drift": round(drift, 5),
        "first_sigma": round(sigma, 5),
        # Filled by `resolve` once the print for `day` lands in `days`. Present and
        # null from the start so a missing resolution reads as pending, not absent.
        "observed": None, "resolved_at": None,
    })
    row["last_made_at"] = made_at
    row["last_forecast"] = round(forecast, 4)
    row["last_drift"] = round(drift, 5)
    row["last_sigma"] = round(sigma, 5)


def resolve(log: dict, *, now: float | None = None) -> int:
    """Fill in what AAA actually printed for every pending row. Returns fills."""
    now = time.time() if now is None else now
    days = log.get("days") or {}
    filled = 0
    for day, row in (log.get("rows") or {}).items():
        if row.get("resolved_at") is not None:
            continue
        if day in days:
            row["observed"] = float(days[day])
            row["resolved_at"] = now
            filled += 1
    return filled


def summarise(log: dict) -> dict:
    """Forecast bias and error spread, from resolved rows only.

    `bias` is forecast minus observed in dollars: positive means the persistence
    model runs high. `n` is small for a long time and the numbers are meaningless
    until it is not - which is why it is reported next to them.
    """
    errors = [row["first_forecast"] - row["observed"]
              for row in (log.get("rows") or {}).values()
              if row.get("observed") is not None
              and row.get("first_forecast") is not None]
    n = len(errors)
    if not n:
        return {"n": 0}
    bias = sum(errors) / n
    var = sum((e - bias) ** 2 for e in errors) / (n - 1) if n > 1 else 0.0
    return {"n": n, "bias": round(bias, 5), "sigma": round(var ** 0.5, 5),
            "mae": round(sum(abs(e) for e in errors) / n, 5)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="predictionedge.gaslog")
    ap.add_argument("--log", default=DEFAULT_PATH)
    args = ap.parse_args(argv)

    from .gas import fetch_national

    log = load(args.log)
    snap = fetch_national()
    touched = record_snapshot(log, snap or {},
                              fallback_day=time.strftime("%Y-%m-%d"))
    filled = resolve(log)
    log["summary"] = summarise(log)
    save(args.log, log)

    deltas = consecutive_deltas(log)
    pending = sum(1 for r in log["rows"].values() if r.get("observed") is None)
    print(f"gaslog: {touched} day(s) recorded, {filled} resolved, "
          f"{len(log['days'])} prints, {len(deltas)} delta(s), {pending} pending")
    s = log["summary"]
    if s.get("n"):
        print(f"  n={s['n']} bias {s['bias']:+.4f}  sigma {s['sigma']:.4f}  "
              f"mae {s['mae']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
