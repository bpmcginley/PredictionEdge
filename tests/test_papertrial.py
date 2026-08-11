import json
import os
import tempfile

from predictionedge.backtest import evaluate
from predictionedge.fees import trade_fee
from predictionedge.papertrial import load, record, save, settle, stats


def _blank():
    return {"open": [], "settled": [], "stats": {}}


def _ticket(mid="0xabc", outcome="Mets", price=0.5, **over):
    t = {"market_id": mid, "outcome": outcome, "entry_price": price,
         "title": "Mets vs Pirates", "url": "https://x", "conviction": 0.7,
         "n_wallets": 3, "whale_usd": 12000.0, "drift_c": 2.0,
         "hours_to_resolve": 6.0, "liquidity": 50000.0,
         "end_iso": "2026-08-18T00:00:00Z", "event_iso": "2026-08-11T23:00:00Z"}
    t.update(over)
    return t


def _meta(outcomes, prices, closed=True):
    return {"closed": closed, "outcomes": outcomes, "prices": prices}


def test_a_ticket_becomes_one_open_position_at_the_flat_stake():
    trial = _blank()
    assert record(trial, [_ticket(price=0.4)], stake=100.0) == 1
    row = trial["open"][0]
    assert row["stake"] == 100.0
    assert row["contracts"] == 250.0        # 100 / 0.40
    assert row["price"] == 0.4
    assert row["conviction"] == 0.7         # recorded for slicing, not for sizing


def test_the_same_ticket_republished_is_not_a_second_position():
    # The board reprints a qualifying ticket every 15 minutes. Without dedup the trial
    # would fill with copies of one opinion and `n` would measure uptime, not skill.
    trial = _blank()
    record(trial, [_ticket()], stake=100.0)
    added = record(trial, [_ticket(price=0.55)], stake=100.0)
    assert added == 0
    assert len(trial["open"]) == 1
    assert trial["open"][0]["price"] == 0.5   # first sighting's price, not the later one


def test_a_closed_position_cannot_be_reopened():
    trial = _blank()
    record(trial, [_ticket()], stake=100.0)
    settle(trial, {"0xabc": _meta(["Mets", "Pirates"], [1.0, 0.0])})
    assert record(trial, [_ticket()], stake=100.0) == 0


def test_two_legs_of_one_market_are_two_positions():
    trial = _blank()
    n = record(trial, [_ticket(outcome="Mets"), _ticket(outcome="Pirates")], stake=50.0)
    assert n == 2


def test_an_unpriceable_ticket_is_not_a_data_point():
    trial = _blank()
    assert record(trial, [_ticket(price=0.0), _ticket(mid="0xd", price=1.0)]) == 0
    assert trial["open"] == []


def test_an_open_market_stays_open():
    trial = _blank()
    record(trial, [_ticket()], stake=100.0)
    assert settle(trial, {"0xabc": _meta(["Mets", "Pirates"], [0.6, 0.4], closed=False)}) == 0
    assert len(trial["open"]) == 1


def test_a_closed_but_unresolved_market_stays_open():
    # Closed with both legs still quoting is a UMA dispute or a mid-resolution read.
    # Guessing here is how a paper record quietly becomes fiction, so it waits.
    trial = _blank()
    record(trial, [_ticket()], stake=100.0)
    assert settle(trial, {"0xabc": _meta(["Mets", "Pirates"], [0.5, 0.5])}) == 0
    assert len(trial["open"]) == 1
    assert trial["settled"] == []


def test_a_market_we_know_nothing_about_stays_open():
    trial = _blank()
    record(trial, [_ticket()], stake=100.0)
    assert settle(trial, {}) == 0
    assert len(trial["open"]) == 1


def test_a_win_pays_the_contracts_less_the_fee():
    trial = _blank()
    record(trial, [_ticket(price=0.4)], stake=100.0, fee_multiplier=0.07, maker=True)
    assert settle(trial, {"0xabc": _meta(["Mets", "Pirates"], [1.0, 0.0])}) == 1
    row = trial["settled"][0]
    fee = trade_fee(0.4, 250, multiplier=0.07, maker=True)
    assert row["won"] is True
    assert row["realized"] == round(250.0 * 0.6 - fee, 4)
    assert trial["open"] == []


def test_a_loss_costs_the_stake_plus_the_fee():
    trial = _blank()
    record(trial, [_ticket(price=0.4)], stake=100.0, fee_multiplier=0.07, maker=True)
    settle(trial, {"0xabc": _meta(["Mets", "Pirates"], [0.0, 1.0])})
    row = trial["settled"][0]
    fee = trade_fee(0.4, 250, multiplier=0.07, maker=True)
    assert row["won"] is False
    assert row["realized"] == round(-100.0 - fee, 4)


def test_outcome_matching_ignores_case_and_padding():
    trial = _blank()
    record(trial, [_ticket(outcome=" mets ")], stake=100.0)
    settle(trial, {"0xabc": _meta(["Mets", "Pirates"], [1.0, 0.0])})
    assert trial["settled"][0]["won"] is True


def test_pred_is_the_entry_price_so_the_gap_reads_as_edge():
    # Every ticket bought at 0.50 and exactly half of them win: no edge, and the
    # calibration gap must say so by landing on zero.
    trial = _blank()
    for i in range(4):
        record(trial, [_ticket(mid=f"0x{i}")], stake=100.0)
    for i in range(4):
        won = ["1.0", "0.0"] if i < 2 else ["0.0", "1.0"]
        settle(trial, {f"0x{i}": _meta(["Mets", "Pirates"], [float(won[0]), float(won[1])])})
    s = stats(trial, min_n=2)
    assert s["avg_predicted"] == 0.5
    assert s["win_rate"] == 0.5
    assert s["calibration_gap"] == 0.0


def test_settled_rows_are_exactly_what_backtest_consumes():
    # The point of the whole module: rows that drop straight into the existing gate.
    trial = _blank()
    for i in range(3):
        record(trial, [_ticket(mid=f"0x{i}")], stake=100.0)
        settle(trial, {f"0x{i}": _meta(["Mets", "Pirates"], [1.0, 0.0])})
    res = evaluate(trial["settled"], min_n=2)
    assert res["n"] == 3
    assert res["win_rate"] == 1.0


def test_stats_on_an_empty_trial_refuses_rather_than_reports():
    s = stats(_blank())
    assert s["n"] == 0
    assert s["deploy"] is False
    assert s["open_positions"] == 0


def test_stats_counts_what_is_still_riding():
    trial = _blank()
    record(trial, [_ticket(mid="0x1"), _ticket(mid="0x2")], stake=100.0)
    assert stats(trial)["open_positions"] == 2


def test_the_trial_survives_a_round_trip_through_disk():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "sub", "trial.json")
    trial = _blank()
    record(trial, [_ticket()], stake=100.0)
    save(p, trial)
    again = load(p)
    assert again["open"][0]["key"] == trial["open"][0]["key"]
    assert json.loads(open(p, encoding="utf-8").read())["settled"] == []


def test_a_position_is_dated_when_the_price_was_seen():
    trial = _blank()
    record(trial, [_ticket()], stake=100.0, now=1_700_000_000.0)
    assert trial["open"][0]["opened_at"] == 1_700_000_000.0


def test_a_missing_trial_file_starts_empty_instead_of_exploding():
    t = load(os.path.join(tempfile.mkdtemp(), "nope.json"))
    assert t == {"open": [], "settled": [], "stats": {}}
