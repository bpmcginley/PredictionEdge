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


def test_whether_a_ticket_was_actually_shown_is_recorded():
    trial = _blank()
    record(trial, [_ticket(mid="0x1", _shown=True), _ticket(mid="0x2", _shown=False)])
    assert [r["shown"] for r in trial["open"]] == [True, False]


def test_a_ticket_with_no_shown_flag_counts_as_shown():
    trial = _blank()
    record(trial, [_ticket()])
    assert trial["open"][0]["shown"] is True


def test_a_position_is_dated_when_the_price_was_seen():
    trial = _blank()
    record(trial, [_ticket()], stake=100.0, now=1_700_000_000.0)
    assert trial["open"][0]["opened_at"] == 1_700_000_000.0


def test_a_missing_trial_file_starts_empty_instead_of_exploding():
    t = load(os.path.join(tempfile.mkdtemp(), "nope.json"))
    assert t == {"open": [], "settled": [], "stats": {}}


# --- venue routing -------------------------------------------------------------

def test_legacy_rows_without_a_venue_are_still_polymarket():
    """`docs/trial.json` has live rows written before Kalshi existed in the trial.

    Relabelling them would rewrite history; routing them to the wrong exchange would
    park them open forever, because Gamma answers a Kalshi ticker with silence rather
    than an error.
    """
    from predictionedge.papertrial import venue_of
    assert venue_of({"market_id": "0x" + "ab" * 32}) == "polymarket"
    assert venue_of({"market_id": "KXHIGHNY-26AUG10-T89"}) == "kalshi"
    assert venue_of({"market_id": "KXHIGHNY-26AUG10-T89",
                     "venue": "polymarket"}) == "polymarket"   # explicit wins
    assert venue_of({"market_id": ""}) == "polymarket"          # corrupt keeps old path


def test_a_recorded_kalshi_ticket_carries_its_venue():
    trial = _blank()
    record(trial, [_ticket(mid="KXHIGHNY-26AUG12-B85.5", outcome="Yes", price=0.45)])
    assert trial["open"][0]["venue"] == "kalshi"


def test_a_voided_market_leaves_the_trial_instead_of_hanging_open():
    trial = _blank()
    record(trial, [_ticket(mid="KXHIGHNY-26AUG10-T89", outcome="Yes", price=0.45)])
    assert settle(trial, {"KXHIGHNY-26AUG10-T89": {"voided": True}}) == 0
    assert trial["open"] == []                 # not held forever
    assert len(trial["voided"]) == 1           # and not silently deleted either
    assert trial["voided"][0]["outcome"] == "Yes"


def test_a_void_is_not_counted_as_a_settled_bet():
    trial = _blank()
    record(trial, [_ticket(mid="KXHIGHNY-26AUG10-T89", outcome="Yes", price=0.45)])
    settle(trial, {"KXHIGHNY-26AUG10-T89": {"voided": True}})
    assert trial["settled"] == []
    assert stats(trial).get("n", 0) == 0


# --- two sleeves, scored apart ---------------------------------------------------

def test_a_row_without_a_source_is_a_whale_row():
    """`docs/trial.json` holds live rows written before the weather sleeve existed."""
    from predictionedge.papertrial import source_of
    assert source_of({}) == "whale"
    assert source_of({"source": "weather"}) == "weather"
    trial = _blank()
    record(trial, [_ticket()])
    assert trial["open"][0]["source"] == "whale"


def test_a_weather_ticket_keeps_its_source():
    trial = _blank()
    record(trial, [_ticket(mid="KXHIGHNY-26AUG12-B87.5", outcome="Yes", price=0.09,
                           source="weather", venue="kalshi")])
    row = trial["open"][0]
    assert row["source"] == "weather" and row["venue"] == "kalshi"


def test_the_two_sleeves_are_scored_apart():
    """Pooling a next-day temperature with a six-month Polymarket resolution produces
    arithmetic, not evidence. Whichever sleeve reaches n first must be judgeable alone.
    """
    trial = _blank()
    record(trial, [_ticket(mid="0xaaa", outcome="Yes", price=0.5),
                   _ticket(mid="KXHIGHNY-26AUG12-B87.5", outcome="Yes", price=0.1,
                           source="weather", venue="kalshi")])
    settle(trial, {"0xaaa": _meta(["Yes", "No"], [1.0, 0.0]),
                   "KXHIGHNY-26AUG12-B87.5": _meta(["Yes", "No"], [0.0, 1.0])})
    by = stats(trial)["by_source"]
    assert by["whale"]["n"] == 1 and by["whale"]["win_rate"] == 1.0
    assert by["weather"]["n"] == 1 and by["weather"]["win_rate"] == 0.0


def test_open_positions_are_counted_per_sleeve_before_anything_settles():
    """The first useful number this reports, and it must not need a settlement first."""
    trial = _blank()
    record(trial, [_ticket(mid="0xaaa"),
                   _ticket(mid="KXHIGHNY-26AUG12-B87.5", outcome="Yes", price=0.1,
                           source="weather")])
    by = stats(trial)["by_source"]
    assert by["whale"]["open_positions"] == 1
    assert by["weather"]["open_positions"] == 1


# --- honest fees on the weather sleeve -------------------------------------------

def _weather_ticket(**over):
    base = dict(mid="KXHIGHNY-26AUG12-B87.5", outcome="Yes", price=0.08,
                source="weather", venue="kalshi", model_prob=0.19, edge=0.11,
                forecast_f=88.4, sigma_f=3.1, market_mu_f=86.2, market_sigma_f=3.1,
                days_ahead=0.6, market_prob=0.085,
                model="kalshi-ladder-tilted-by-nws-gridpoint", city="Central Park")
    base.update(over)
    return _ticket(**base)


def test_a_maker_False_ticket_is_charged_taker_fees_despite_the_default():
    trial = _blank()
    record(trial, [_weather_ticket(_maker=False)], stake=100.0,
           fee_multiplier=0.07, maker=True)
    row = trial["open"][0]
    assert row["fee"] == trade_fee(0.08, 1250, multiplier=0.07, maker=False)


def test_retag_recharges_legacy_maker_booked_weather_rows():
    """Rows opened before the fix carry maker fees - 4x too low on a taker cross."""
    from predictionedge.papertrial import retag_weather_fees
    trial = _blank()
    record(trial, [_weather_ticket()], stake=100.0, fee_multiplier=0.07, maker=True)
    stale = trial["open"][0]["fee"]
    assert retag_weather_fees(trial) == 1
    fixed = trial["open"][0]["fee"]
    assert fixed == trade_fee(0.08, 1250, multiplier=0.07, maker=False)
    assert fixed > stale * 3            # the understatement was real money
    assert retag_weather_fees(trial) == 0   # idempotent: taker recomputes to itself


def test_retag_leaves_whale_rows_and_settled_rows_alone():
    """The whale sleeve's maker accounting must not change mid-sample, and settled
    rows are evidence - a retroactive edit would be worse than the bug it fixes."""
    from predictionedge.papertrial import retag_weather_fees
    trial = _blank()
    record(trial, [_ticket(price=0.4), _weather_ticket()],
           stake=100.0, fee_multiplier=0.07, maker=True)
    settle(trial, {"0xabc": _meta(["Mets", "Pirates"], [1.0, 0.0])})
    whale_settled_fee = trial["settled"][0]["fee"]
    retag_weather_fees(trial)
    assert trial["settled"][0]["fee"] == whale_settled_fee


# --- the model's inputs ride along ------------------------------------------------

def test_weather_model_fields_are_recorded_on_the_row():
    """`model_prob` against `won` is the calibration check the sleeve exists to earn;
    a trial that drops it can validate the picks while the model stays unfalsifiable."""
    trial = _blank()
    record(trial, [_weather_ticket()])
    row = trial["open"][0]
    assert row["model_prob"] == 0.19
    assert row["sigma_f"] == 3.1
    assert row["forecast_f"] == 88.4
    assert row["market_prob"] == 0.085
    assert row["model"] == "kalshi-ladder-tilted-by-nws-gridpoint"


def test_whale_rows_do_not_grow_null_model_fields():
    trial = _blank()
    record(trial, [_ticket()])
    assert "model_prob" not in trial["open"][0]
    assert "sigma_f" not in trial["open"][0]
