"""Backtest / validation harness for the settled-bets dataset.

Answers the only question that matters before scaling caps: does the edge survive
fees out of sample, or is it noise? It mirrors the rigor of the StockTradingLol
optimizer - a per-bet Sharpe deflated for multiple-testing (Bailey & Lopez de Prado
DSR), a block-bootstrap confidence interval on mean return, and calibration scoring -
behind a single deploy gate that must pass on all counts.

    python -m predictionedge.backtest                 # uses data/settled_bets.jsonl
    python -m predictionedge.backtest path.jsonl --trials 20
"""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path
from statistics import NormalDist, mean, stdev

from .config import Config

_ND = NormalDist()
_EULER = 0.5772156649015329


def _moments(xs: list[float], m: float, s: float) -> tuple[float, float]:
    """Population skewness and (non-excess) kurtosis; normal kurtosis = 3."""
    if s <= 0 or len(xs) == 0:
        return 0.0, 3.0
    n = len(xs)
    skew = sum(((x - m) / s) ** 3 for x in xs) / n
    kurt = sum(((x - m) / s) ** 4 for x in xs) / n
    return skew, kurt


def _psr(sr: float, sr0: float, n: int, skew: float, kurt: float) -> float:
    """Probabilistic Sharpe Ratio: P(true SR > sr0) given the estimate."""
    denom = math.sqrt(max(1e-12, 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr * sr))
    return _ND.cdf((sr - sr0) * math.sqrt(max(n - 1, 1)) / denom)


def _deflated_sr(sr: float, n: int, skew: float, kurt: float, n_trials: int) -> tuple[float, float]:
    """Deflated Sharpe Ratio and the implied benchmark SR0 from multiple testing."""
    if n_trials <= 1:
        return _psr(sr, 0.0, n, skew, kurt), 0.0
    var_sr = max(1e-12, (1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr * sr) / max(n - 1, 1))
    z1 = _ND.inv_cdf(1.0 - 1.0 / n_trials)
    z2 = _ND.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    sr0 = math.sqrt(var_sr) * ((1.0 - _EULER) * z1 + _EULER * z2)
    return _psr(sr, sr0, n, skew, kurt), sr0


def _block_bootstrap(returns: list[float], *, block: int = 5, n_boot: int = 2000,
                     alpha: float = 0.05, seed: int = 12345) -> tuple[float, float, float]:
    """Circular-ish block bootstrap of the mean: (lo CI, hi CI, P(mean<=0))."""
    n = len(returns)
    if n == 0:
        return 0.0, 0.0, 1.0
    rng = random.Random(seed)
    block = max(1, min(block, n))
    n_blocks = math.ceil(n / block)
    means: list[float] = []
    le0 = 0
    for _ in range(n_boot):
        sample: list[float] = []
        for _ in range(n_blocks):
            start = rng.randint(0, n - block)
            sample.extend(returns[start:start + block])
        sample = sample[:n]
        m = sum(sample) / len(sample)
        means.append(m)
        if m <= 0:
            le0 += 1
    means.sort()
    lo = means[int(alpha * n_boot)]
    hi = means[min(n_boot - 1, int((1.0 - alpha) * n_boot))]
    return lo, hi, le0 / n_boot


def evaluate(bets: list[dict], *, n_trials: int = 1, min_n: int = 30,
             dsr_threshold: float = 0.95, block: int = 5) -> dict:
    """Score a settled-bets dataset and decide whether the edge is deployable."""
    rows = [b for b in bets if b.get("stake", 0) > 0]
    n = len(rows)
    if n == 0:
        return {"n": 0, "deploy": False, "reasons": ["no settled bets"]}

    returns = [b["realized"] / b["stake"] for b in rows]
    won = [1.0 if b["won"] else 0.0 for b in rows]
    pred = [float(b["pred"]) for b in rows]

    m = mean(returns)
    s = stdev(returns) if n > 1 else 0.0
    sharpe = m / s if s > 0 else 0.0
    skew, kurt = _moments(returns, m, s)
    dsr, sr0 = _deflated_sr(sharpe, n, skew, kurt, n_trials)
    psr = _psr(sharpe, 0.0, n, skew, kurt)
    lo, hi, p_le0 = _block_bootstrap(returns, block=block)

    total_pnl = sum(b["realized"] for b in rows)
    total_stake = sum(b["stake"] for b in rows)
    win_rate = mean(won)
    avg_pred = mean(pred)
    brier = mean((p - w) ** 2 for p, w in zip(pred, won))

    reasons: list[str] = []
    if n < min_n:
        reasons.append(f"n={n} < min_n={min_n}")
    if m <= 0:
        reasons.append("mean per-bet return <= 0")
    if dsr < dsr_threshold:
        reasons.append(f"DSR {dsr:.3f} < {dsr_threshold}")
    if lo <= 0:
        reasons.append("bootstrap 5% CI of mean return <= 0")

    return {
        "n": n,
        "win_rate": round(win_rate, 4),
        "avg_predicted": round(avg_pred, 4),
        "calibration_gap": round(avg_pred - win_rate, 4),
        "brier": round(brier, 4),
        "total_pnl": round(total_pnl, 2),
        "roi_on_stake": round(total_pnl / total_stake, 4),
        "mean_return": round(m, 4),
        "sharpe_per_bet": round(sharpe, 4),
        "psr_vs_zero": round(psr, 4),
        "dsr": round(dsr, 4),
        "implied_sr0": round(sr0, 4),
        "bootstrap_ci": [round(lo, 4), round(hi, 4)],
        "p_mean_le_0": round(p_le0, 4),
        "deploy": not reasons,
        "reasons": reasons,
    }


def evaluate_oos(bets: list[dict], *, oos_frac: float = 0.3, n_trials: int = 1,
                 min_n: int = 30, dsr_threshold: float = 0.95, block: int = 5) -> dict:
    """Walk-forward style split: the edge must hold in-sample AND out-of-sample.

    Bets are recorded in resolution order, so the last ``oos_frac`` is a genuine
    hold-out the strategy never 'saw'. Deploy only if both folds pass their gate -
    the honest test against an edge that decays once markets adapt.
    """
    rows = [b for b in bets if b.get("stake", 0) > 0]
    n = len(rows)
    if n < min_n:
        return {"deploy": False, "reasons": [f"n={n} < min_n={min_n}"],
                "in_sample": None, "out_of_sample": None}
    split = max(1, int(round(n * (1.0 - oos_frac))))
    oos_min = max(10, int(round(min_n * oos_frac)))
    in_sample = evaluate(rows[:split], n_trials=n_trials, min_n=min_n,
                         dsr_threshold=dsr_threshold, block=block)
    out_sample = evaluate(rows[split:], n_trials=n_trials, min_n=oos_min,
                          dsr_threshold=dsr_threshold, block=block)
    reasons = []
    if not in_sample["deploy"]:
        reasons.append("in-sample fold failed")
    if not out_sample["deploy"]:
        reasons.append("out-of-sample fold failed")
    return {"n": n, "split_at": split, "deploy": not reasons, "reasons": reasons,
            "in_sample": in_sample, "out_of_sample": out_sample}


def load_bets(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="predictionedge.backtest")
    ap.add_argument("path", nargs="?", help="settled-bets JSONL (default: config)")
    ap.add_argument("--trials", type=int, default=1, help="strategy configs tested (deflation)")
    ap.add_argument("--min-n", type=int, default=30)
    ap.add_argument("--dsr", type=float, default=0.95, help="DSR deploy threshold")
    ap.add_argument("--oos", action="store_true", help="walk-forward in/out-of-sample gate")
    ap.add_argument("--oos-frac", type=float, default=0.3, help="hold-out fraction for --oos")
    args = ap.parse_args(argv)

    cfg = Config.from_env()
    path = args.path or cfg.settled_path
    bets = load_bets(path)
    if args.oos:
        result = evaluate_oos(bets, oos_frac=args.oos_frac, n_trials=args.trials,
                              min_n=args.min_n, dsr_threshold=args.dsr)
    else:
        result = evaluate(bets, n_trials=args.trials, min_n=args.min_n, dsr_threshold=args.dsr)
    print(json.dumps(result, indent=2))
    return 0 if result.get("deploy") else 1


if __name__ == "__main__":
    raise SystemExit(main())
