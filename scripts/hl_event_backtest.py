"""Event backtest for the EVENT-CONVEX ticket (docs/HYPERLIQUID_CONVEX.md, section 3a).

Answers the only question that matters for that strategy: *measured on real prints,
what is the expected return on a ticket, and how wide is the uncertainty?*

For each scheduled macro release (CPI / FOMC / NFP, `scripts/hl_events.csv`) it:

1. pulls 1-minute BTC candles for the window around the release
   (Binance spot by default - it carries weight 3 of 12 in Hyperliquid's oracle, so it is
   the closest free proxy for the mark; `--source hyperliquid` uses candleSnapshot),
2. applies the ticket rules mechanically - direction = sign of the release minute's
   move, entry at the next minute's open, hard stop, trailing stop once in profit,
   time stop, liquidation at the tier's maintenance margin, taker fees both sides,
3. reports hit rate, mean/median return on the stake, a bootstrap CI on the mean,
   the drawdown risk at the chosen bankroll fraction, and a random-direction baseline
   that shows what fees + stop asymmetry cost with no edge at all.

Stdlib only. Candle windows are cached under data/hl_events/ so a run needs the
network once. `--synthetic N` exercises the engine on simulated windows with no network,
which validates the harness, NOT the strategy.

Run locally (this sandbox cannot reach any exchange):

    python scripts/hl_event_backtest.py                      # CPI+FOMC+NFP, Binance
    python scripts/hl_event_backtest.py --grid               # sweep impulse thresholds
    python scripts/hl_event_backtest.py --source hyperliquid # Hyperliquid perp candles
    python scripts/hl_event_backtest.py --synthetic 400      # harness check, no network

Every date in hl_events.csv should be checked against bls.gov / federalreserve.gov
before you trust a number; rows marked verify=0 are best-effort.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVENTS_CSV = ROOT / "scripts" / "hl_events.csv"
CACHE_DIR = ROOT / "data" / "hl_events"

MIN_PER_YEAR = 365.25 * 24 * 60
TAKER = 0.00045
MM_TIER1_BTC = 0.0125


@dataclass(frozen=True)
class Candle:
    t: int      # open time, ms UTC
    o: float
    h: float
    l: float
    c: float


@dataclass(frozen=True)
class Params:
    leverage: float = 20.0
    sl: float = 0.015          # hard stop, fraction of entry
    trail_arm: float = 0.015   # trailing stop arms once this far in profit
    trail: float = 0.010       # trailing distance from the best price
    hold: int = 60             # minutes before the time stop
    min_impulse: float = 0.004
    max_impulse: float = 0.015
    slip: float = 0.0002       # per side
    taker: float = TAKER
    mm: float = MM_TIER1_BTC
    pre: int = 30              # minutes of candles fetched before the release (unused by rules)

    @property
    def d_liq(self) -> float:
        return 1.0 / self.leverage - self.mm


@dataclass
class Ticket:
    event: str
    kind: str
    taken: bool
    reason: str                # skip reason or exit reason
    direction: int = 0
    impulse: float = 0.0
    entry: float = 0.0
    exit: float = 0.0
    minutes: int = 0
    price_ret: float = 0.0     # signed, fraction of entry
    stake_ret: float = 0.0     # return on the isolated margin, after fees
    activity: float = 0.0      # release-minute range / median pre-window range (date sanity)


# ---------------------------------------------------------------- events

def _et_to_utc(date_s: str, time_s: str) -> datetime:
    naive = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M")
    try:
        from zoneinfo import ZoneInfo
        return naive.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
    except Exception:  # no tzdata (bare Windows): US DST rule, 2nd Sun Mar -> 1st Sun Nov
        y = naive.year
        mar = datetime(y, 3, 8) + timedelta(days=(6 - datetime(y, 3, 8).weekday()) % 7)
        nov = datetime(y, 11, 1) + timedelta(days=(6 - datetime(y, 11, 1).weekday()) % 7)
        offset = 4 if mar + timedelta(hours=2) <= naive < nov + timedelta(hours=2) else 5
        return (naive + timedelta(hours=offset)).replace(tzinfo=timezone.utc)


def load_events(path: Path, kinds: set[str] | None = None) -> list[tuple[datetime, str, str]]:
    out = []
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            if row["date"].startswith("#"):
                continue
            if kinds and row["kind"] not in kinds:
                continue
            out.append((_et_to_utc(row["date"], row["time_et"]), row["kind"], row.get("note", "")))
    out.sort()
    return out


# ---------------------------------------------------------------- data

def _get(url: str, body: dict | None = None) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "predictionedge-hl-backtest"})
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def fetch_binance(start_ms: int, end_ms: int, symbol: str = "BTCUSDT") -> list[Candle]:
    url = (f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1m"
           f"&startTime={start_ms}&endTime={end_ms}&limit=1000")
    rows = _get(url)
    return [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows]


def fetch_binance_us(start_ms: int, end_ms: int, symbol: str = "BTCUSDT") -> list[Candle]:
    url = (f"https://api.binance.us/api/v3/klines?symbol={symbol}&interval=1m"
           f"&startTime={start_ms}&endTime={end_ms}&limit=1000")
    rows = _get(url)
    return [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows]


def fetch_coinbase(start_ms: int, end_ms: int, product: str = "BTC-USD") -> list[Candle]:
    """Coinbase Exchange: [time_s, low, high, open, close, volume], newest first, max 300."""
    iso = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = (f"https://api.exchange.coinbase.com/products/{product}/candles"
           f"?granularity=60&start={iso(start_ms)}&end={iso(end_ms)}")
    rows = _get(url)
    out = [Candle(int(r[0]) * 1000, float(r[3]), float(r[2]), float(r[1]), float(r[4])) for r in rows]
    return sorted(out, key=lambda c: c.t)


def fetch_bitstamp(start_ms: int, end_ms: int, pair: str = "btcusd") -> list[Candle]:
    url = (f"https://www.bitstamp.net/api/v2/ohlc/{pair}/?step=60&limit=1000"
           f"&start={start_ms // 1000}&end={end_ms // 1000}")
    rows = _get(url)["data"]["ohlc"]
    return [Candle(int(r["timestamp"]) * 1000, float(r["open"]), float(r["high"]),
                   float(r["low"]), float(r["close"])) for r in rows]


def fetch_hyperliquid(start_ms: int, end_ms: int, coin: str = "BTC") -> list[Candle]:
    rows = _get("https://api.hyperliquid.xyz/info",
                {"type": "candleSnapshot",
                 "req": {"coin": coin, "interval": "1m", "startTime": start_ms, "endTime": end_ms}})
    return [Candle(int(r["t"]), float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"])) for r in rows]


FETCHERS = {
    "binance": lambda s, e, coin: fetch_binance(s, e, f"{coin}USDT"),
    "binanceus": lambda s, e, coin: fetch_binance_us(s, e, f"{coin}USDT"),
    "coinbase": lambda s, e, coin: fetch_coinbase(s, e, f"{coin}-USD"),
    "bitstamp": lambda s, e, coin: fetch_bitstamp(s, e, f"{coin.lower()}usd"),
    "hyperliquid": lambda s, e, coin: fetch_hyperliquid(s, e, coin),
}
# Binance.com refuses US IPs (GitHub runners included); the chain falls through to
# venues that serve them. All are spot USD(T) quotes, i.e. proxies for the oracle.
# Binance US is last: it is so thin that half its minute candles have no range at all,
# which makes every stop and trail decision on them meaningless.
AUTO_CHAIN = ("binance", "coinbase", "bitstamp", "binanceus")


def window(t0: datetime, source: str, coin: str, p: Params,
           cache: Path = CACHE_DIR) -> tuple[list[Candle], str]:
    """Candles for one event window and the source they came from. Empty responses are
    treated as failures and never cached, so a re-run retries them."""
    start = t0 - timedelta(minutes=p.pre)
    end = t0 + timedelta(minutes=p.hold + 2)
    cache.mkdir(parents=True, exist_ok=True)
    s, e = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    errors = []
    for src in (AUTO_CHAIN if source == "auto" else (source,)):
        f = cache / f"{src}_{coin}_{t0.strftime('%Y%m%dT%H%M')}.json"
        if f.exists():
            rows = json.loads(f.read_text())
            if rows:
                return [Candle(*r) for r in rows], src
        try:
            candles = FETCHERS[src](s, e, coin)
        except Exception as ex:
            errors.append(f"{src}: {type(ex).__name__} {str(ex)[:80]}")
            continue
        if not candles:
            errors.append(f"{src}: empty response")
            continue
        f.write_text(json.dumps([[c.t, c.o, c.h, c.l, c.c] for c in candles]))
        return candles, src
    raise RuntimeError("; ".join(errors))


def continuous(days: int, source: str, coin: str, cache: Path, end: datetime | None = None,
               log=print) -> tuple[list[Candle], str]:
    """1-minute candles for the last `days` days, fetched a UTC day at a time and cached
    per day (Coinbase serves 300 candles per call, so a day is 5 calls)."""
    end = end or datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    cache.mkdir(parents=True, exist_ok=True)
    out, used = [], None
    for k in range(days, 0, -1):
        day = end - timedelta(days=k)
        errors = []
        for src in (AUTO_CHAIN if source == "auto" else (source,)):
            if used and src != used:
                continue  # one venue per series; mixing books mixes wick behaviour
            f = cache / f"{src}_{coin}_{day:%Y%m%d}.json"
            if f.exists():
                rows = json.loads(f.read_text())
                if rows:
                    out.extend(Candle(*r) for r in rows); used = src
                    break
            rows = []
            try:
                for h in range(0, 24, 5):   # 5-hour chunks keep Coinbase under its 300 cap
                    s_ms = int((day + timedelta(hours=h)).timestamp() * 1000)
                    e_ms = int((day + timedelta(hours=min(h + 5, 24))).timestamp() * 1000) - 1
                    rows.extend(FETCHERS[src](s_ms, e_ms, coin))
            except Exception as ex:
                errors.append(f"{src}: {type(ex).__name__} {str(ex)[:60]}")
                continue
            if len(rows) < 1000:
                errors.append(f"{src}: only {len(rows)} candles")
                continue
            rows = sorted({c.t: c for c in rows}.values(), key=lambda c: c.t)
            f.write_text(json.dumps([[c.t, c.o, c.h, c.l, c.c] for c in rows]))
            out.extend(rows); used = src
            break
        else:
            log(f"  {day:%Y-%m-%d}: no data ({'; '.join(errors)})")
    return out, used or "none"


def scan_impulses(candles: list[Candle], p: Params) -> list[tuple[int, int]]:
    """Every minute whose own open->close move passes the impulse filter, with a cooldown
    of `hold` minutes after each trigger so tickets never overlap. Returns (index, t_ms)."""
    hits, last = [], -10**9
    for i in range(30, len(candles) - p.hold - 2):
        c = candles[i]
        if i - last <= p.hold:
            continue
        if p.min_impulse <= abs(c.c / c.o - 1.0) <= p.max_impulse:
            hits.append((i, c.t)); last = i
    return hits


# ---------------------------------------------------------------- rules

def run_ticket(candles: list[Candle], t0_ms: int, p: Params, name: str = "", kind: str = "",
               force_dir: int | None = None) -> Ticket:
    """Apply the EVENT-CONVEX rules to one window. Candle high/low are used conservatively:
    the adverse extreme is assumed to print before the favorable one within a minute."""
    idx = {c.t: i for i, c in enumerate(candles)}
    if t0_ms not in idx or idx[t0_ms] + 1 >= len(candles):
        return Ticket(name, kind, False, "no-candle")
    i0 = idx[t0_ms]
    c0 = candles[i0]
    impulse = c0.c / c0.o - 1.0
    pre_ranges = sorted((c.h - c.l) / c.o for c in candles[max(0, i0 - 30):i0]) or [0.0]
    med_pre = pre_ranges[len(pre_ranges) // 2]
    activity = ((c0.h - c0.l) / c0.o) / med_pre if med_pre > 0 else float("inf")
    if force_dir is None and not (p.min_impulse <= abs(impulse) <= p.max_impulse):
        return Ticket(name, kind, False, "no-impulse", impulse=impulse, activity=activity)
    d = force_dir if force_dir is not None else (1 if impulse > 0 else -1)
    entry = candles[i0 + 1].o * (1 + d * p.slip)
    stop = entry * (1 - d * p.sl)
    liq = entry * (1 - d * p.d_liq)
    best = entry
    trail_stop: float | None = None
    exit_px, reason, held = None, "time", 0
    for i in range(i0 + 1, min(i0 + 1 + p.hold, len(candles))):
        c = candles[i]
        held = i - i0
        adverse = c.l if d > 0 else c.h
        favorable = c.h if d > 0 else c.l
        if (d > 0 and adverse <= liq) or (d < 0 and adverse >= liq):
            exit_px, reason = liq, "liq"
            break
        if (d > 0 and adverse <= stop) or (d < 0 and adverse >= stop):
            exit_px, reason = stop * (1 - d * p.slip), "stop"
            break
        if trail_stop is not None and ((d > 0 and adverse <= trail_stop) or (d < 0 and adverse >= trail_stop)):
            exit_px, reason = trail_stop * (1 - d * p.slip), "trail"
            break
        armed_now = False
        if (d > 0 and favorable > best) or (d < 0 and favorable < best):
            best = favorable
            if abs(best / entry - 1) >= p.trail_arm:
                armed_now = trail_stop is None
                trail_stop = best * (1 - d * p.trail)
        if armed_now and ((d > 0 and c.c <= trail_stop) or (d < 0 and c.c >= trail_stop)):
            exit_px, reason = trail_stop * (1 - d * p.slip), "trail"
            break
    if exit_px is None:
        last = candles[min(i0 + p.hold, len(candles) - 1)]
        exit_px = last.c * (1 - d * p.slip)
    price_ret = d * (exit_px / entry - 1.0)
    stake_ret = -1.0 if reason == "liq" else p.leverage * price_ret - 2 * p.taker * p.leverage
    return Ticket(name, kind, True, reason, d, impulse, entry, exit_px, held, price_ret, stake_ret, activity)


# ---------------------------------------------------------------- stats

def bootstrap_mean_ci(xs: list[float], n: int = 10000, lo: float = 0.05, hi: float = 0.95,
                      seed: int = 1) -> tuple[float, float]:
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(xs, k=len(xs))) for _ in range(n))
    return means[int(lo * n)], means[int(hi * n)]


def bootstrap_drawdown(xs: list[float], fraction: float, tickets: int = 40, n: int = 5000,
                       seed: int = 2) -> tuple[float, float]:
    """5th percentile of terminal wealth and of max drawdown over `tickets` resampled
    tickets, staking `fraction` of current bankroll each time."""
    rng = random.Random(seed)
    finals, dds = [], []
    for _ in range(n):
        w, peak, dd = 1.0, 1.0, 0.0
        for r in rng.choices(xs, k=tickets):
            w *= 1 + fraction * r
            peak = max(peak, w)
            dd = max(dd, 1 - w / peak)
        finals.append(w)
        dds.append(dd)
    finals.sort(); dds.sort()
    return finals[int(0.05 * n)] - 1, dds[int(0.95 * n)]


def summarize(tickets: list[Ticket], baseline: list[Ticket], fraction: float) -> dict:
    taken = [t for t in tickets if t.taken]
    out = {"events": len(tickets), "taken": len(taken)}
    if not taken:
        return out
    rs = [t.stake_ret for t in taken]
    out["exit_mix"] = {k: sum(1 for t in taken if t.reason == k) for k in ("trail", "time", "stop", "liq")}
    out["hit_rate"] = sum(1 for r in rs if r > 0) / len(rs)
    out["mean"] = statistics.fmean(rs)
    out["median"] = statistics.median(rs)
    out["ci90"] = bootstrap_mean_ci(rs)
    wins = [r for r in rs if r > 0]; losses = [-r for r in rs if r <= 0]
    out["avg_win"] = statistics.fmean(wins) if wins else 0.0
    out["avg_loss"] = statistics.fmean(losses) if losses else 0.0
    b = out["avg_win"] / out["avg_loss"] if losses and wins else float("nan")
    out["kelly"] = out["hit_rate"] - (1 - out["hit_rate"]) / b if b == b and b > 0 else float("nan")
    out["p5_terminal_40"], out["p95_maxdd_40"] = bootstrap_drawdown(rs, fraction)
    out["bankroll_per_ticket"] = fraction * out["mean"]
    base = [t.stake_ret for t in baseline if t.taken]
    out["baseline_mean"] = statistics.fmean(base) if base else float("nan")
    return out


def print_summary(s: dict, p: Params, fraction: float) -> None:
    print(f"\nEvents: {s['events']}   tickets taken: {s['taken']}   "
          f"(impulse filter {p.min_impulse:.2%}..{p.max_impulse:.2%}, {p.leverage:.0f}x, "
          f"sl {p.sl:.1%}, trail {p.trail:.1%} after {p.trail_arm:.1%}, hold {p.hold}m)")
    if s["taken"] == 0:
        print("No ticket passed the impulse filter.")
        return
    print(f"Exit mix: {s['exit_mix']}")
    print(f"Hit rate {s['hit_rate']:.1%}   avg win {s['avg_win']:+.2f}x   avg loss {s['avg_loss']:.2f}x   "
          f"Kelly {s['kelly']:+.2f}")
    print(f"EV per ticket on stake: {s['mean']:+.3f}   median {s['median']:+.3f}   "
          f"90% bootstrap CI [{s['ci90'][0]:+.3f}, {s['ci90'][1]:+.3f}]")
    print(f"Random-direction baseline (same windows, coin-flip direction): {s['baseline_mean']:+.3f}")
    print(f"At {fraction:.1%} of bankroll per ticket: {s['bankroll_per_ticket']:+.3%} of bankroll per ticket; "
          f"over 40 tickets the 5th-pct outcome is {s['p5_terminal_40']:+.1%} "
          f"and the 95th-pct max drawdown is {s['p95_maxdd_40']:.1%}")
    ci_lo, ci_hi = s["ci90"]
    verdict = ("positive at 90%" if ci_lo > 0 else
               "NEGATIVE at 90%: the ticket loses money" if ci_hi < 0 else
               "edge not distinguishable from zero")
    print(f"Verdict: {verdict}; n={s['taken']} tickets"
          + (" (too few to conclude; keep collecting)" if s["taken"] < 30 else ""))


# ---------------------------------------------------------------- synthetic

def synthetic_windows(n: int, p: Params, sigma_ann: float = 0.9, p_dir: float = 0.5,
                      drift_per_min: float = 0.0, seed: int = 3) -> list[tuple[list[Candle], int]]:
    """GBM minute candles with a release-minute burst and an optional post-release drift
    that continues the burst's direction with probability p_dir (else reverses it).
    Used only to prove the engine runs; p_dir=0.5, drift=0 is the 'no edge' world."""
    rng = random.Random(seed)
    step = sigma_ann * math.sqrt(1 / MIN_PER_YEAR)
    out = []
    for k in range(n):
        px, t = 60000.0, 1_700_000_000_000 + k * 10_000_000
        candles = []
        burst = 1 if rng.random() < 0.5 else -1
        sign = burst if rng.random() < p_dir else -burst
        for i in range(p.pre + p.hold + 2):
            o = px
            drift = 0.0
            if i == p.pre:                     # release minute: a burst
                drift = burst * abs(rng.gauss(0.006, 0.003))
            elif i > p.pre:
                drift = sign * drift_per_min
            path = [o]
            for _ in range(6):
                path.append(path[-1] * (1 + drift / 6 + step / math.sqrt(6) * rng.gauss(0, 1)))
            px = path[-1]
            candles.append(Candle(t + i * 60_000, o, max(path), min(path), px))
        out.append((candles, t + p.pre * 60_000))
    return out


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--events", type=Path, default=EVENTS_CSV)
    ap.add_argument("--kinds", default="CPI,FOMC,NFP")
    ap.add_argument("--source", choices=("auto", *FETCHERS), default="auto",
                    help="auto tries " + " > ".join(AUTO_CHAIN))
    ap.add_argument("--coin", default="BTC")
    ap.add_argument("--leverage", type=float, default=20)
    ap.add_argument("--sl", type=float, default=0.015)
    ap.add_argument("--trail", type=float, default=0.010)
    ap.add_argument("--trail-arm", type=float, default=0.015)
    ap.add_argument("--hold", type=int, default=60)
    ap.add_argument("--min-impulse", type=float, default=0.004)
    ap.add_argument("--max-impulse", type=float, default=0.015)
    ap.add_argument("--fraction", type=float, default=0.015, help="bankroll fraction per ticket")
    ap.add_argument("--grid", action="store_true", help="sweep the impulse filter (overfitting hazard)")
    ap.add_argument("--scan", type=int, default=0, metavar="DAYS",
                    help="instead of the calendar, trigger on every qualifying impulse minute "
                         "in the last DAYS days of continuous candles (the cascade-rider test)")
    ap.add_argument("--scan-cache", type=Path, default=ROOT / "data" / "hl_scan")
    ap.add_argument("--synthetic", type=int, default=0, metavar="N", help="run on N simulated windows")
    ap.add_argument("--synthetic-edge", type=float, default=0.5, help="p_dir for --synthetic")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--cache", type=Path, default=CACHE_DIR, help="candle cache directory")
    ap.add_argument("--report", type=Path, default=None, help="also append the output to this file")
    a = ap.parse_args(argv)
    if a.report:
        class _Tee:
            def __init__(self, *fs): self.fs = fs
            def write(self, x): [f.write(x) for f in self.fs]
            def flush(self): [f.flush() for f in self.fs]
        a.report.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = _Tee(sys.__stdout__, a.report.open("a"))

    p = Params(leverage=a.leverage, sl=a.sl, trail=a.trail, trail_arm=a.trail_arm, hold=a.hold,
               min_impulse=a.min_impulse, max_impulse=a.max_impulse)
    if p.sl >= p.d_liq:
        print(f"stop {p.sl:.2%} is not inside the liquidation distance {p.d_liq:.2%}", file=sys.stderr)
        return 2

    if a.scan:
        series, src = continuous(a.scan, a.source, a.coin, a.scan_cache)
        flat = sum(1 for c in series if c.h == c.l) / len(series) if series else 1.0
        print(f"SCAN: {len(series)} minutes from {src} over the last {a.scan} days; "
              f"{flat:.1%} flat candles" + ("  <-- TOO THIN" if flat > 0.10 else ""))
        triggers = scan_impulses(series, p)
        windows = [(f"{datetime.fromtimestamp(t / 1000, tz=timezone.utc):%Y-%m-%d %H:%M}", "IMP",
                    series[i - 30:i + p.hold + 2], t) for i, t in triggers]
        print(f"{len(windows)} impulse triggers (|1-min move| in {p.min_impulse:.2%}..{p.max_impulse:.2%}, "
              f"cooldown {p.hold}m)")
    elif a.synthetic:
        wins = synthetic_windows(a.synthetic, p, p_dir=a.synthetic_edge,
                                 drift_per_min=0.0003 if a.synthetic_edge > 0.5 else 0.0)
        windows = [(f"syn{k}", "SYN", c, t0) for k, (c, t0) in enumerate(wins)]
        print(f"SYNTHETIC run: {a.synthetic} windows, p_dir={a.synthetic_edge}. Validates the harness only.")
    else:
        events = load_events(a.events, set(a.kinds.split(",")))
        windows, failures, srcs = [], [], {}
        for t0, kind, note in events:
            try:
                candles, src = window(t0, a.source, a.coin, p, a.cache)
            except Exception as e:  # network / API / geo-block
                failures.append(f"{kind} {t0:%Y-%m-%d %H:%M}Z: {e}")
                continue
            srcs[src] = srcs.get(src, 0) + 1
            windows.append((f"{kind} {t0:%Y-%m-%d}", kind, candles, int(t0.timestamp() * 1000)))
        print(f"Loaded {len(windows)}/{len(events)} event windows ({a.coin}); sources: {srcs}")
        allc = [c for _, _, cs, _ in windows for c in cs]
        if allc:
            flat = sum(1 for c in allc if c.h == c.l) / len(allc)
            print(f"Data quality: {len(allc)} candles, {flat:.1%} flat (high == low)"
                  + ("  <-- TOO THIN, stops/trails are unreliable" if flat > 0.10 else ""))
        for line in failures[:5]:
            print("  failed:", line)
        if len(failures) > 5:
            print(f"  ... and {len(failures) - 5} more failures")

    def run(params: Params) -> tuple[list[Ticket], list[Ticket]]:
        tickets, baseline = [], []
        for name, kind, candles, t0 in windows:
            t = run_ticket(candles, t0, params, name, kind)
            tickets.append(t)
            if t.taken:  # same window, both directions, averaged = what a coin flip earns
                for fd in (1, -1):
                    baseline.append(run_ticket(candles, t0, params, name, kind, force_dir=fd))
        return tickets, baseline

    tickets, baseline = run(p)
    if a.verbose:
        for t in (tickets if not a.scan else [t for t in tickets if t.taken][:40]):
            if t.taken:
                print(f"  {t.event:<18} {t.kind:<5} dir {t.direction:+d} impulse {t.impulse:+.2%} "
                      f"{t.reason:<5} {t.minutes:>3}m  price {t.price_ret:+.2%}  stake {t.stake_ret:+.2f}"
                      f"  activity x{t.activity:.1f}")
            else:
                print(f"  {t.event:<18} {t.kind:<5} skipped ({t.reason}, impulse {t.impulse:+.2%}, "
                      f"activity x{t.activity:.1f})")
        quiet = [t for t in tickets if t.reason != "no-candle" and t.activity < 2.0]
        if quiet:
            print(f"  DATE CHECK: {len(quiet)} release minutes were no busier than the prior half hour "
                  f"(activity < 2x); their dates/times may be wrong: "
                  + ", ".join(t.event for t in quiet))
    print_summary(summarize(tickets, baseline, a.fraction), p, a.fraction)

    if a.grid:
        print("\nImpulse-filter grid (mean stake return / n). Pick nothing from this table without "
              "a holdout; it is here to show how fragile the number is.")
        lows, highs = (0.002, 0.003, 0.004, 0.006), (0.010, 0.015, 0.025)
        print("  min\\max " + "".join(f"{h:>14.1%}" for h in highs))
        for lo in lows:
            cells = []
            for hi in highs:
                q = Params(**{**p.__dict__, "min_impulse": lo, "max_impulse": hi})
                tk, _ = run(q)
                rs = [t.stake_ret for t in tk if t.taken]
                cells.append(f"{statistics.fmean(rs):+.3f}/{len(rs):<3}" if rs else "      -/0  ")
            print(f"  {lo:>6.1%}  " + "".join(f"{c:>14}" for c in cells))
    return 0


if __name__ == "__main__":
    sys.exit(main())
