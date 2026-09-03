import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "hl_event_backtest", Path(__file__).resolve().parent.parent / "scripts" / "hl_event_backtest.py")
bt = importlib.util.module_from_spec(_spec)
sys.modules["hl_event_backtest"] = bt
_spec.loader.exec_module(bt)

P = bt.Params()
T0 = 1_700_000_000_000


def _candles(closes, t0_index=1, wick=0.0):
    """Flat candles at 100 before the release; then the given closes, each candle spanning
    from the prior close to this close, with an optional symmetric wick."""
    out, prev = [], 100.0
    for i, c in enumerate(closes):
        hi, lo = max(prev, c) * (1 + wick), min(prev, c) * (1 - wick)
        out.append(bt.Candle(T0 + (i - t0_index) * 60_000, prev, hi, lo, c))
        prev = c
    return out


def test_skips_when_impulse_too_small():
    t = bt.run_ticket(_candles([100, 100.1, 101, 102]), T0, P)
    assert not t.taken and t.reason == "no-impulse"


def test_skips_when_impulse_too_large():
    t = bt.run_ticket(_candles([100, 102.5, 103, 104]), T0, P)
    assert not t.taken and t.reason == "no-impulse"


def test_trailing_stop_locks_in_a_hit():
    # +0.6% release minute, then a 4% run, then a 1.5% give-back -> trail exit in profit
    closes = [100, 100.6] + [100.6 + 0.4 * k for k in range(1, 11)] + [104.6 * (1 - 0.015)]
    t = bt.run_ticket(_candles(closes), T0, P)
    assert t.taken and t.direction == 1 and t.reason == "trail"
    assert t.price_ret > 0.02
    assert abs(t.stake_ret - (P.leverage * t.price_ret - 2 * P.taker * P.leverage)) < 1e-9


def test_hard_stop_caps_the_loss_inside_liquidation():
    closes = [100, 99.4, 99.0, 98.0, 97.0]        # short ticket, then price rips... no: down move then reverse
    closes = [100, 99.4, 99.0, 100.0, 101.5]      # -0.6% impulse -> short; then +2.5% against
    t = bt.run_ticket(_candles(closes), T0, P)
    assert t.taken and t.direction == -1 and t.reason == "stop"
    assert -P.sl * 1.001 - 0.0005 < t.price_ret < -P.sl + 1e-9
    assert t.stake_ret > -1.0                      # not the whole stake


def test_liquidation_loses_the_whole_stake():
    closes = [100, 100.6, 100.5, 95.0]             # long, then a 5% crash through the 3.75% liq distance
    t = bt.run_ticket(_candles(closes), T0, P)
    assert t.taken and t.reason == "liq" and t.stake_ret == -1.0


def test_time_stop_closes_at_last_close():
    closes = [100, 100.6] + [100.6 + 0.005 * k for k in range(1, 70)]  # slow grind, never arms trail
    t = bt.run_ticket(_candles(closes), T0, P)
    assert t.taken and t.reason == "time" and t.minutes == P.hold


def test_random_direction_baseline_is_negative_with_no_edge():
    p = bt.Params()
    wins = bt.synthetic_windows(300, p, p_dir=0.5, drift_per_min=0.0, seed=11)
    rs = []
    for candles, t0 in wins:
        for d in (1, -1):
            t = bt.run_ticket(candles, t0, p, force_dir=d)
            rs.append(t.stake_ret)
    assert sum(rs) / len(rs) < 0.0


def test_events_csv_parses_and_converts_to_utc():
    ev = bt.load_events(bt.EVENTS_CSV)
    assert len(ev) > 50
    kinds = {k for _, k, _ in ev}
    assert kinds == {"CPI", "FOMC", "NFP"}
    # CPI at 08:30 ET is 12:30Z in summer, 13:30Z in winter
    by_date = {t.strftime("%Y-%m-%d"): t for t, k, _ in ev if k == "CPI"}
    assert by_date["2024-07-11"].strftime("%H:%M") == "12:30"
    assert by_date["2024-01-11"].strftime("%H:%M") == "13:30"
