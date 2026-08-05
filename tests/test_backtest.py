import random

from predictionedge.backtest import evaluate, evaluate_oos


def _bets(n_win, n_loss, r_win=0.9, r_loss=-1.0, pred=0.6, stake=10.0):
    bets = [{"stake": stake, "realized": r_win * stake, "won": True, "pred": pred}
            for _ in range(n_win)]
    bets += [{"stake": stake, "realized": r_loss * stake, "won": False, "pred": pred}
             for _ in range(n_loss)]
    return bets


def _bets_mixed(n, win_rate=0.66, r_win=0.9, r_loss=-1.0, pred=0.62, stake=10.0, seed=3):
    """Wins/losses spread across the order, so an OOS split is representative."""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        won = rng.random() < win_rate
        out.append({"stake": stake, "realized": (r_win if won else r_loss) * stake,
                    "won": won, "pred": pred})
    return out


def test_empty_dataset_not_deployable():
    res = evaluate([])
    assert res["n"] == 0 and res["deploy"] is False


def test_profitable_series_deploys():
    res = evaluate(_bets(130, 70), n_trials=1, min_n=30)
    assert res["n"] == 200
    assert res["mean_return"] > 0
    assert res["bootstrap_ci"][0] > 0
    assert res["deploy"] is True


def test_coinflip_not_deployable():
    res = evaluate(_bets(100, 100, r_win=1.0, r_loss=-1.0), n_trials=1, min_n=30)
    assert res["deploy"] is False


def test_small_sample_blocked():
    res = evaluate(_bets(8, 2), min_n=30)
    assert res["deploy"] is False
    assert any("min_n" in r for r in res["reasons"])


def test_deflation_tightens_with_more_trials():
    base = evaluate(_bets(130, 70), n_trials=1)
    deflated = evaluate(_bets(130, 70), n_trials=500)
    assert deflated["dsr"] <= base["dsr"]
    assert deflated["implied_sr0"] >= base["implied_sr0"]


def test_oos_profitable_deploys_both_folds():
    res = evaluate_oos(_bets_mixed(300, win_rate=0.66), oos_frac=0.3, min_n=30)
    assert res["in_sample"]["deploy"] is True
    assert res["out_of_sample"]["deploy"] is True
    assert res["deploy"] is True


def test_oos_coinflip_fails():
    res = evaluate_oos(_bets_mixed(300, win_rate=0.5, r_win=1.0, r_loss=-1.0),
                       oos_frac=0.3, min_n=30)
    assert res["deploy"] is False


def test_oos_too_small_blocked():
    res = evaluate_oos(_bets_mixed(20), min_n=30)
    assert res["deploy"] is False
