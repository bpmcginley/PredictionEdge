import json
import os
import tempfile

import pytest

from predictionedge.backtest import evaluate
from predictionedge.fees import trade_fee
from predictionedge.papertrial import (load, per_opinion, record, save, settle,
                                       stats)


def _blank():
    return {"open": [], "settled": [], "stats": {}}


def _ticket(mid="0xabc", outcome="Mets", price=0.5, **over):
    # The url is per-market by default because it is now the EVENT key (`_event_of`),
    # and one shared url would silently put every fixture in a test under one per-event
    # contract ceiling. Tests that mean "same event" pass the same url explicitly.
    t = {"market_id": mid, "outcome": outcome, "entry_price": price,
         "title": "Mets vs Pirates", "url": f"https://x/{mid}", "conviction": 0.7,
         "n_wallets": 3, "whale_usd": 12000.0, "drift_c": 2.0,
         "hours_to_resolve": 6.0, "liquidity": 50000.0,
         "end_iso": "2026-08-18T00:00:00Z", "event_iso": "2026-08-11T23:00:00Z"}
    t.update(over)
    return t


def _meta(outcomes, prices, closed=True):
    return {"closed": closed, "outcomes": outcomes, "prices": prices}


def test_a_ticket_is_sized_by_the_house_rules_not_by_a_flat_dollar_amount():
    # $100 at 1% per trade implies a $10,000 account, so 19 contracts per $1,000 caps
    # this market at 190. The risk budget alone would have bought 250 at 40c; the cap
    # is the binding constraint below ~52.6c, which is where most of the book lives.
    trial = _blank()
    assert record(trial, [_ticket(price=0.4)], stake=100.0) == 1
    row = trial["open"][0]
    assert row["contracts"] == 190.0
    assert row["stake"] == 76.0             # 190 * 0.40, what the sized order costs
    assert row["capped_by"] == "per-market-limit"
    assert row["price"] == 0.4
    assert row["conviction"] == 0.7         # recorded for slicing, not for sizing


def test_above_the_crossover_the_risk_budget_binds_instead_of_the_cap():
    # At 80c the budget buys 125 contracts, well under the 190 ceiling - so the stake
    # is the full flat amount and `capped_by` says which rule was actually in charge.
    trial = _blank()
    record(trial, [_ticket(price=0.8)], stake=100.0)
    row = trial["open"][0]
    assert row["contracts"] == 125.0
    assert row["stake"] == 100.0
    assert row["capped_by"] == "risk-budget"


def test_conviction_never_scales_the_stake():
    # The sizer CAN scale by conviction and the trial deliberately does not: one
    # hypothesis at a time, and the settled rows say conviction does not rank anyway.
    trial = _blank()
    record(trial, [_ticket(mid="0x1", price=0.8, conviction=0.30),
                   _ticket(mid="0x2", price=0.8, conviction=0.95)], stake=100.0)
    assert {r["stake"] for r in trial["open"]} == {100.0}


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


def test_both_sides_of_one_market_is_never_two_positions():
    # 40 events in the live record were held on both sides, a median 7.3h apart: a
    # guaranteed loss of two fees, scored as two independent opinions when it is one
    # signal contradicting itself. The second leg is refused and the refusal is logged.
    trial = _blank()
    n = record(trial, [_ticket(outcome="Mets"), _ticket(outcome="Pirates")], stake=50.0)
    assert n == 1
    assert [r["outcome"] for r in trial["open"]] == ["Mets"]
    blocked = list(trial["blocked"].values())
    assert len(blocked) == 1
    assert blocked[0]["outcome"] == "Pirates"
    assert blocked[0]["held"] == ["mets"]


def test_the_opposing_side_is_blocked_across_runs_not_just_within_one():
    # The board already keeps one opinion per event within a run, so every real
    # contradiction happened on a LATER cycle. That is the case this has to catch.
    trial = _blank()
    record(trial, [_ticket(outcome="Mets")], stake=50.0)
    assert record(trial, [_ticket(outcome="Pirates")], stake=50.0) == 0
    assert len(trial["open"]) == 1


def test_a_republished_block_is_one_entry_with_a_count_not_a_growing_list():
    # The board reprints a blocked ticket every 15 minutes. A list would grow forever.
    trial = _blank()
    record(trial, [_ticket(outcome="Mets")], stake=50.0)
    for _ in range(4):
        record(trial, [_ticket(outcome="Pirates")], stake=50.0)
    assert len(trial["blocked"]) == 1
    assert list(trial["blocked"].values())[0]["times"] == 4


def test_the_per_event_cap_stops_a_third_leg_of_the_same_fixture():
    # Same event url, different markets - a moneyline and its totals. 380 contracts per
    # event at this scale, 190 per market, so the third leg has no room left. This is
    # the $600-on-one-baseball-game case the flat stake used to wave through.
    trial = _blank()
    ev = "https://polymarket.com/event/mlb-nym-pit-2026-08-11"
    n = record(trial, [_ticket(mid="0x1", outcome="Mets", url=ev),
                       _ticket(mid="0x2", outcome="Over", url=ev),
                       _ticket(mid="0x3", outcome="Yes", url=ev)], stake=100.0)
    assert n == 2
    assert sum(r["contracts"] for r in trial["open"]) == 380.0
    reasons = [r["reason"] for r in trial["blocked"].values()]
    assert reasons == ["per-event contract cap already full"]


def test_two_legs_of_different_events_are_still_two_positions():
    trial = _blank()
    n = record(trial, [_ticket(mid="0x1", outcome="Mets"),
                       _ticket(mid="0x2", outcome="Pirates")], stake=50.0)
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
    record(trial, [_ticket(price=0.4)], stake=100.0, fee_multiplier=0.07)
    assert settle(trial, {"0xabc": _meta(["Mets", "Pirates"], [1.0, 0.0])}) == 1
    row = trial["settled"][0]
    fee = trade_fee(0.4, 190, multiplier=0.07, maker=False)
    assert row["won"] is True
    assert row["realized"] == round(190.0 * 0.6 - fee, 4)
    assert trial["open"] == []


def test_a_loss_costs_the_stake_plus_the_fee():
    trial = _blank()
    record(trial, [_ticket(price=0.4)], stake=100.0, fee_multiplier=0.07)
    settle(trial, {"0xabc": _meta(["Mets", "Pirates"], [0.0, 1.0])})
    row = trial["settled"][0]
    fee = trade_fee(0.4, 190, multiplier=0.07, maker=False)
    assert row["won"] is False
    # A loss costs what the sized order cost - 190 * 0.40 - not the flat stake it was
    # capped down from. `stake` is the denominator of this row's return, so the two
    # must be the same number or the loss reads as worse than 100%.
    assert row["realized"] == round(-76.0 - fee, 4)


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


def test_a_retired_sleeve_leaves_the_headline_but_not_the_file():
    """The go-forward headline must answer "how does the bot that runs tomorrow do",
    so a switched-off sleeve cannot drag it. The rows still have to be THERE: a record
    that deletes its losers is worth nothing, and in a public repo the deletion would
    only publish a diff of losing rows being removed. Excluded, flagged, and still
    reachable via `by_source` and `including_retired`.
    """
    trial = _blank()
    record(trial, [_ticket(mid="0xaaa", outcome="Yes", price=0.5),
                   _ticket(mid="KXHIGHNY-26AUG12-B87.5", outcome="Yes", price=0.1,
                           source="weather", venue="kalshi")])
    settle(trial, {"0xaaa": _meta(["Yes", "No"], [1.0, 0.0]),
                   "KXHIGHNY-26AUG12-B87.5": _meta(["Yes", "No"], [0.0, 1.0])})
    s = stats(trial)

    assert s["n"] == 1 and s["win_rate"] == 1.0        # headline: active sleeve only
    assert s["retired_excluded"] == 1
    assert s["retired_sources"] == ["weather"]
    assert s["including_retired"]["n"] == 2            # all-time record still reported
    assert s["including_retired"]["win_rate"] == 0.5
    assert s["by_source"]["weather"]["retired"] is True
    assert s["by_source"]["weather"]["n"] == 1
    # the row itself is untouched on disk
    assert any(r["market_id"].startswith("KXHIGHNY") for r in trial["settled"])


def test_open_retired_positions_are_held_out_of_the_headline_count_too():
    """The 3 open weather positions were left to settle naturally rather than deleted,
    so they must not inflate the count of what the live bot is currently carrying."""
    trial = _blank()
    record(trial, [_ticket(mid="0xaaa"),
                   _ticket(mid="KXHIGHNY-26AUG12-B87.5", outcome="Yes", price=0.1,
                           source="weather")])
    s = stats(trial)
    assert s["open_positions"] == 1
    assert s["by_source"]["weather"]["open_positions"] == 1
    assert len(trial["open"]) == 2                     # both still recorded


# --- a retired market CLASS, which is not a sleeve --------------------------------

def _esports_ticket(**over):
    base = dict(mid="0xcs2", outcome="Astralis", price=0.5,
                title="Counter-Strike: Astralis vs NIP - Map 1 Winner",
                url="https://polymarket.com/event/cs2-ast10-nip-2026-08-16")
    base.update(over)
    return _ticket(**base)


def test_the_gate_counts_one_opinion_once_however_many_venues_carried_it():
    """The bridge turns one view of one question into two rows. The deploy gate measures
    whether independent draws support an edge, so counting the reflection as a second
    draw hands it evidence that was never collected.
    """
    trial = _blank()
    record(trial, [_ticket(mid="0xm", outcome="Mets", price=0.5),
                   _ticket(mid="0xm-pmus", outcome="Mets", price=0.5,
                           origin_market_id="0xm")])
    settle(trial, {"0xm": _meta(["Mets", "Pirates"], [1.0, 0.0]),
                   "0xm-pmus": _meta(["Mets", "Pirates"], [1.0, 0.0])})
    s = stats(trial)

    assert s["n"] == 1                      # one opinion, not two draws
    assert s["opinions"] == 1
    assert s["settled_rows"] == 2           # both rows still scored and still on file
    assert s["as_recorded"]["n"] == 2       # the per-row view is kept, not replaced
    assert s["by_source"]["whale"]["n"] == 2


def test_collapsing_sums_the_money_across_the_venues_that_carried_it():
    """One position split over two venues earned what both legs earned. Averaging the
    RETURNS instead would quietly reweight a $10 leg to match a $1,000 one.
    """
    trial = _blank()
    rows = per_opinion([
        {"market_id": "0xm", "outcome": "Mets", "opened_at": 10, "stake": 100.0,
         "realized": 50.0, "won": True, "pred": 0.4},
        {"market_id": "0xm-pmus", "origin_market_id": "0xm", "outcome": "Mets",
         "opened_at": 20, "stake": 300.0, "realized": -30.0, "won": True, "pred": 0.6},
    ])
    assert len(rows) == 1
    assert rows[0]["stake"] == 400.0 and rows[0]["realized"] == 20.0
    assert rows[0]["pred"] == pytest.approx(0.55)   # stake-weighted, not 0.5
    assert rows[0]["opened_at"] == 10               # the opinion dates from its first leg
    assert trial == _blank()                        # nothing recorded, nothing touched


def test_opposite_sides_of_one_market_are_two_opinions_not_one():
    """Identity is market AND outcome. Merging YES with NO would net a genuine hedge into
    a single flat row and hide both bets from the gate."""
    rows = per_opinion([
        {"market_id": "0xm", "outcome": "Yes", "opened_at": 1, "stake": 100.0,
         "realized": 10.0, "won": True, "pred": 0.5},
        {"market_id": "0xm", "outcome": "No", "opened_at": 2, "stake": 100.0,
         "realized": -10.0, "won": False, "pred": 0.5},
    ])
    assert len(rows) == 2


def test_a_record_with_no_mirrors_is_unchanged_by_collapsing():
    """A no-op has to be a real no-op: this runs on every publish."""
    rows = [{"market_id": f"0x{i}", "outcome": "Yes", "opened_at": i, "stake": 10.0,
             "realized": 1.0, "won": True, "pred": 0.5} for i in range(5)]
    assert per_opinion(rows) == rows


def test_a_mirror_of_a_derivative_market_is_held_out_but_its_origin_is_not():
    """The bridge matched a spread onto the destination's PLAIN market, so the row books
    our price against someone else's question - which is why the four such rows on file
    realized -105.7%, -101.2%, +136.9% and +727.2%. Only the MIRROR is held out: the
    bridge refuses to mirror these now, but the whale sleeve still bets the origin, and a
    headline that drops rows the live bot still takes is wrong in the other direction.
    """
    trial = _blank()
    origin = _ticket(mid="0xspread", outcome="Texas Rangers",
                     title="Spread: Texas Rangers (-1.5)")
    mirror = _ticket(mid="0xspread-pmus", outcome="Texas Rangers",
                     title="Spread: Texas Rangers (-1.5)",
                     origin_market_id="0xspread")
    record(trial, [origin, mirror])
    settle(trial, {"0xspread": _meta(["Texas Rangers", "No"], [1.0, 0.0]),
                   "0xspread-pmus": _meta(["Texas Rangers", "No"], [1.0, 0.0])})
    s = stats(trial)

    assert s["n"] == 1                                 # the origin, still live
    assert s["by_source"]["derivative-mirror"]["n"] == 1
    assert s["by_source"]["derivative-mirror"]["retired"] is True
    # The pooled figure counts opinions too, so the retired mirror rejoins its origin
    # rather than arriving as a second draw - the two corrections composing, not fighting.
    assert s["including_retired"]["n"] == 1
    # Nothing left the file: the holdout is a scoring decision, never a deletion.
    assert len(trial["settled"]) == 2
    assert any(r["market_id"] == "0xspread-pmus" for r in trial["settled"])


def test_a_plain_mirror_still_counts_in_the_headline():
    """The gate is a title heuristic, so its blast radius has to stay where it is aimed:
    an ordinary venue mirror is the bridge working as intended and belongs in the record.
    """
    trial = _blank()
    record(trial, [_ticket(mid="0xplain-pmus", outcome="Mets",
                           title="Mets vs Pirates", origin_market_id="0xplain")])
    settle(trial, {"0xplain-pmus": _meta(["Mets", "Pirates"], [1.0, 0.0])})

    assert stats(trial)["n"] == 1


def test_esports_rows_are_held_out_of_the_headline_though_they_are_whale_rows():
    """The cut that `RETIRED_SOURCES` could not express. Esports was switched off at the
    gate, but those rows carry `source="whale"` - so keying the holdout on the sleeve
    left 127 settled rows worth -$1,904 scoring inside a headline whose whole claim is
    that it describes the bot that runs tomorrow. It does not bet them tomorrow.
    """
    trial = _blank()
    record(trial, [_ticket(mid="0xaaa", outcome="Yes", price=0.5), _esports_ticket()])
    settle(trial, {"0xaaa": _meta(["Yes", "No"], [1.0, 0.0]),
                   "0xcs2": _meta(["Astralis", "NIP"], [0.0, 1.0])})
    s = stats(trial)

    assert s["n"] == 1 and s["win_rate"] == 1.0        # the live row only
    assert s["retired_excluded"] == 1
    assert s["retired_classes"] == ["esports", "derivative-mirror"]
    assert s["including_retired"]["n"] == 2            # still on the all-time record
    # and still on file, untouched: a record that deletes its losers is worth nothing
    assert any(r["market_id"] == "0xcs2" for r in trial["settled"])


def test_a_retired_class_is_its_own_slice_not_a_drag_on_the_sleeve_it_came_through():
    """`by_source` has to stay a partition, or the headline and its own breakdown say
    different things: every settled row counted once, the live slices summing to the
    headline and the retired ones to the difference."""
    trial = _blank()
    record(trial, [_ticket(mid="0xaaa", outcome="Yes", price=0.5), _esports_ticket()])
    settle(trial, {"0xaaa": _meta(["Yes", "No"], [1.0, 0.0]),
                   "0xcs2": _meta(["Astralis", "NIP"], [0.0, 1.0])})
    by = stats(trial)["by_source"]

    assert by["whale"]["n"] == 1 and by["whale"]["win_rate"] == 1.0   # esports NOT in it
    assert by["esports"]["n"] == 1 and by["esports"]["retired"] is True
    assert by["whale"]["n"] + by["esports"]["n"] == len(trial["settled"])


def test_open_esports_positions_leave_the_headline_count_too():
    """They were opened before the cut and are left to settle honestly, so they must not
    be counted as what the live bot is carrying."""
    trial = _blank()
    record(trial, [_ticket(mid="0xaaa"), _esports_ticket()])
    s = stats(trial)
    assert s["open_positions"] == 1
    assert s["by_source"]["esports"]["open_positions"] == 1
    assert len(trial["open"]) == 2


def test_the_holdout_reads_the_event_slug_exactly_as_the_gate_does():
    """A derivative market on an esports fixture names no game anywhere in its title -
    only the event slug does. If the record classified on the title alone it would hold
    out less than the gate refuses, which is the flattering direction."""
    trial = _blank()
    record(trial, [_esports_ticket(mid="0xhcap",
                                  title="Game Handicap: NS (-1.5) vs DN SOOPers (+1.5)",
                                  url="https://polymarket.com/event/lol-dnf-ns-2026-08-12")])
    assert stats(trial)["open_positions"] == 0
    # ...and an ordinary sport that merely looks similar stays live
    trial = _blank()
    record(trial, [_ticket(mid="0xsoccer", title="Club Leon vs. Deportivo Toluca",
                           url="https://polymarket.com/event/lec-tig-vwh-2026-08-16")])
    assert stats(trial)["open_positions"] == 1


def test_every_held_out_row_is_named_by_key_for_the_page():
    """The Trial page slices these same rows into its own windows. It can read a retired
    SLEEVE off the `source` field; it cannot read a retired CLASS off anything, and a
    second copy of the classifier in JavaScript would be free to disagree with this one.
    Publishing the keys is what makes the page's holdout provably the same holdout."""
    trial = _blank()
    record(trial, [_ticket(mid="0xaaa"), _esports_ticket(),
                   _ticket(mid="KXHIGHNY-26AUG12-B87.5", outcome="Yes", price=0.1,
                           source="weather")])
    settle(trial, {"0xcs2": _meta(["Astralis", "NIP"], [0.0, 1.0])})
    keys = stats(trial)["retired_keys"]

    assert keys == sorted(["0xcs2:astralis", "KXHIGHNY-26AUG12-B87.5:yes"])
    assert "0xaaa:mets" not in keys                    # settled AND open rows, live ones never


# --- honest fees on the weather sleeve -------------------------------------------

def _weather_ticket(**over):
    base = dict(mid="KXHIGHNY-26AUG12-B87.5", outcome="Yes", price=0.08,
                source="weather", venue="kalshi", model_prob=0.19, edge=0.11,
                forecast_f=88.4, sigma_f=3.1, market_mu_f=86.2, market_sigma_f=3.1,
                days_ahead=0.6, market_prob=0.085,
                model="kalshi-ladder-tilted-by-nws-gridpoint", city="Central Park")
    base.update(over)
    return _ticket(**base)


def test_an_instant_fill_is_charged_the_taker_fee_it_incurred():
    """Entries are priced at the ask, so the fill crossed the spread and owes taker.

    This is the whole of the 2026-08-17 correction on the forward side: there is no
    longer a knob that can book this row at a quarter of what it cost.
    """
    trial = _blank()
    record(trial, [_weather_ticket()], stake=100.0, fee_multiplier=0.07)
    row = trial["open"][0]
    assert row["fee"] == trade_fee(0.08, 190, multiplier=0.07, maker=False)
    assert row["fee"] > trade_fee(0.08, 190, multiplier=0.07, maker=True) * 3


def test_a_whale_row_gets_no_maker_discount_either():
    """The whale sleeve is where `assume_maker` did its damage: 287 settled rows."""
    trial = _blank()
    record(trial, [_ticket(price=0.4)], stake=100.0, fee_multiplier=0.07)
    assert trial["open"][0]["fee"] == trade_fee(0.4, 190, multiplier=0.07, maker=False)


def test_retag_recharges_legacy_maker_booked_weather_rows():
    """Rows opened before the fix carry maker fees - 4x too low on a taker cross.

    Built by hand because `record` can no longer produce one: this is the shape of a
    row already in `docs/trial.json`, not a shape the code writes today.
    """
    from predictionedge.papertrial import retag_weather_fees
    trial = _blank()
    record(trial, [_weather_ticket()], stake=100.0, fee_multiplier=0.07)
    trial["open"][0]["fee"] = trade_fee(0.08, 190, multiplier=0.07, maker=True)
    stale = trial["open"][0]["fee"]
    assert retag_weather_fees(trial) == 1
    fixed = trial["open"][0]["fee"]
    assert fixed == trade_fee(0.08, 190, multiplier=0.07, maker=False)
    assert fixed > stale * 3            # the understatement was real money
    assert retag_weather_fees(trial) == 0   # idempotent: taker recomputes to itself


def test_retag_leaves_whale_rows_and_settled_rows_alone():
    """The whale sleeve's fee accounting is not retag's business, and settled rows are
    evidence - a retroactive edit would be worse than the bug it fixes."""
    from predictionedge.papertrial import retag_weather_fees
    trial = _blank()
    record(trial, [_ticket(price=0.4), _weather_ticket()],
           stake=100.0, fee_multiplier=0.07)
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


# --- maker-first entries -----------------------------------------------------------
#
# The single biggest EV lever on a 1-3c edge is not paying the taker fee, so the
# trial must simulate passive entry honestly: join the bid, wait, and record the
# case for the trade AT INTENT TIME - because a record of fills alone drops the
# unfilled winners and keeps the filled losers.

from predictionedge.papertrial import check_fills, maker_stats  # noqa: E402


def _q(bid=None, ask=None):
    return {"KXHIGHNY-26AUG12-B87.5:yes": {"bid": bid, "ask": ask}}


def test_maker_first_records_an_intent_not_a_position():
    trial = _blank()
    added = record(trial, [_weather_ticket(bid=0.06, _maker=False)],
                   stake=100.0, maker_first=True)
    assert added == 1
    assert trial["open"] == []
    row = trial["pending"][0]
    assert row["limit_price"] == 0.06        # joined the bid
    assert row["intended_price"] == 0.06
    assert row["ask_at_intent"] == 0.08      # the taker price it refused to pay
    assert row["fair_at_intent"] == 0.19     # the model's case, frozen at intent
    assert row["edge_at_intent"] == 0.11
    assert row["polls"] == 0


def test_a_republished_ticket_is_not_a_second_intent():
    trial = _blank()
    record(trial, [_weather_ticket(bid=0.06)], maker_first=True)
    assert record(trial, [_weather_ticket(bid=0.05)], maker_first=True) == 0
    assert len(trial["pending"]) == 1


def test_a_ticket_without_a_bid_takes_the_ask_as_before():
    # An empty book has nothing to join; the honest entry is still the taker cross.
    trial = _blank()
    record(trial, [_weather_ticket()], stake=100.0,
           fee_multiplier=0.07, maker_first=True)
    assert trial.get("pending", []) == []
    assert trial["open"][0]["fee"] == trade_fee(0.08, 190, multiplier=0.07, maker=False)


def test_maker_first_off_keeps_the_old_path_bid_or_not():
    trial = _blank()
    record(trial, [_weather_ticket(bid=0.06, _maker=False)], stake=100.0)
    assert trial.get("pending", []) == []
    assert trial["open"][0]["price"] == 0.08


def test_an_intent_does_not_fill_while_the_ask_stays_away():
    trial = _blank()
    record(trial, [_weather_ticket(bid=0.06)], maker_first=True)
    filled, expired = check_fills(trial, _q(bid=0.06, ask=0.08), max_polls=8)
    assert (filled, expired) == (0, 0)
    assert trial["pending"][0]["polls"] == 1
    assert trial["open"] == []


def test_an_intent_fills_when_a_later_ask_comes_to_the_limit():
    trial = _blank()
    record(trial, [_weather_ticket(bid=0.06)], stake=100.0, maker_first=True)
    check_fills(trial, _q(bid=0.06, ask=0.08))
    filled, _ = check_fills(trial, _q(bid=0.05, ask=0.06))
    assert filled == 1
    assert trial["pending"] == []
    row = trial["open"][0]
    assert row["price"] == 0.06              # filled at OUR limit, not the new quote
    assert row["filled_price"] == 0.06
    assert row["intended_price"] == 0.06
    assert row["fill_polls"] == 2
    assert row["maker"] is True
    assert row["contracts"] == 190.0
    assert row["fee"] == 0.0                 # maker fills are free on standard markets


def test_a_market_that_left_the_board_counts_the_poll_toward_expiry():
    # No quote reads as "did not come to us" - never as a free fill.
    trial = _blank()
    record(trial, [_weather_ticket(bid=0.06)], maker_first=True)
    filled, expired = check_fills(trial, {}, max_polls=2)
    assert (filled, expired) == (0, 0)
    filled, expired = check_fills(trial, {}, max_polls=2)
    assert (filled, expired) == (0, 1)


def test_an_unfilled_intent_expires_into_the_log_with_its_foregone_edge():
    trial = _blank()
    record(trial, [_weather_ticket(bid=0.06)], maker_first=True)
    for _ in range(2):
        check_fills(trial, _q(bid=0.06, ask=0.08), max_polls=2)
    assert trial["pending"] == []
    assert trial["open"] == []               # cancel is the default: no trade happened
    dead = trial["maker"]["expired"][0]
    assert dead["edge_at_intent"] == 0.11
    assert "expired_at" in dead
    assert maker_stats(trial)["foregone_edge"] == 0.11


def test_an_expired_intent_blocks_reentry():
    # Re-arming on the same market is the adverse-selection machine itself:
    # keep placing the order and you fill only when the market falls through you.
    trial = _blank()
    record(trial, [_weather_ticket(bid=0.06)], maker_first=True)
    check_fills(trial, {}, max_polls=1)
    assert record(trial, [_weather_ticket(bid=0.06)], maker_first=True) == 0


def test_taker_fallback_crosses_at_the_then_current_ask():
    trial = _blank()
    record(trial, [_weather_ticket(bid=0.06)], stake=100.0, maker_first=True)
    filled, expired = check_fills(trial, _q(bid=0.06, ask=0.09), max_polls=1,
                                  fallback_taker=True, fee_multiplier=0.07)
    assert (filled, expired) == (1, 0)
    row = trial["open"][0]
    assert row["price"] == 0.09              # the ask NOW, not the ask at intent
    assert row["maker"] is False
    assert row["maker_fallback"] is True
    # 190 contracts were ORDERED at the 6c limit; a taker fallback fills that same
    # count at 9c, so the cost rises with the price and the count does not change.
    assert row["contracts"] == 190.0
    assert row["stake"] == round(190.0 * 0.09, 4)
    assert row["fee"] == trade_fee(0.09, 190, multiplier=0.07, maker=False)


def test_a_fallback_with_no_quote_cancels_instead_of_inventing_a_price():
    trial = _blank()
    record(trial, [_weather_ticket(bid=0.06)], maker_first=True)
    filled, expired = check_fills(trial, {}, max_polls=1, fallback_taker=True)
    assert (filled, expired) == (0, 1)
    assert trial["open"] == []


def test_a_maker_fill_settles_through_the_same_machinery():
    trial = _blank()
    record(trial, [_weather_ticket(bid=0.06)], stake=100.0, maker_first=True)
    check_fills(trial, _q(ask=0.06))
    assert settle(trial, {"KXHIGHNY-26AUG12-B87.5": _meta(["Yes", "No"], [1.0, 0.0])}) == 1
    row = trial["settled"][0]
    assert row["won"] is True
    assert row["pred"] == 0.06               # the fill price is the honest null
    contracts = 190.0
    assert row["realized"] == round(contracts * (1.0 - 0.06) - 0.0, 4)
    assert stats(trial)["by_source"]["weather"]["n"] == 1


def test_retag_leaves_maker_fills_alone():
    # A maker fill's zero fee is the point of the whole path; the taker recharge
    # exists for instant crosses booked wrong, not for fills the simulator priced.
    from predictionedge.papertrial import retag_weather_fees
    trial = _blank()
    record(trial, [_weather_ticket(bid=0.06)], stake=100.0, maker_first=True)
    check_fills(trial, _q(ask=0.06))
    assert retag_weather_fees(trial) == 0
    assert trial["open"][0]["fee"] == 0.0


def test_designated_markets_charge_a_quarter_of_taker_on_maker_fills(monkeypatch):
    from predictionedge import fees
    monkeypatch.setattr(fees, "DESIGNATED_MAKER_SERIES", ("KXHIGH",))
    trial = _blank()
    record(trial, [_weather_ticket(bid=0.06)], stake=100.0,
           fee_multiplier=0.07, maker_first=True)
    check_fills(trial, _q(ask=0.06), fee_multiplier=0.07)
    assert trial["open"][0]["fee"] == trade_fee(0.06, 190,
                                                multiplier=0.07, maker=True)


def test_pending_orders_round_trip_through_disk():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "trial.json")
    trial = _blank()
    record(trial, [_weather_ticket(bid=0.06)], maker_first=True)
    check_fills(trial, _q(bid=0.06, ask=0.08))
    save(p, trial)
    again = load(p)
    assert again["pending"][0]["limit_price"] == 0.06
    assert again["pending"][0]["polls"] == 1
    filled, _ = check_fills(again, _q(ask=0.06))   # state survives the reload intact
    assert filled == 1


def test_the_stats_block_reports_the_maker_scoreboard():
    trial = _blank()
    record(trial, [_weather_ticket(bid=0.06),
                   _weather_ticket(mid="KXHIGHCHI-26AUG12-B90.5", bid=0.05,
                                   entry_price=0.07, edge=0.09),
                   _weather_ticket(mid="KXHIGHAUS-26AUG12-B99.5", bid=0.04,
                                   entry_price=0.06, edge=0.08)],
                  stake=100.0, maker_first=True)
    check_fills(trial, {**_q(ask=0.06),
                        "KXHIGHCHI-26AUG12-B90.5:yes": {"ask": 0.08}}, max_polls=1)
    mk = stats(trial)["maker"]
    assert mk["pending"] == 0
    assert mk["filled"] == 1                 # NY came to us
    assert mk["expired"] == 2                # CHI never did, AUS had no quote...
    assert mk["fill_rate"] == round(1 / 3, 4)
    assert mk["avg_fill_polls"] == 1.0
    assert mk["foregone_edge"] == round(0.09 + 0.08, 4)


def test_an_intent_records_polls_even_while_the_stats_show_it_pending():
    trial = _blank()
    record(trial, [_weather_ticket(bid=0.06)], maker_first=True)
    assert stats(trial)["maker"]["pending"] == 1
    assert stats(trial)["maker"]["fill_rate"] is None   # nothing resolved yet


def test_the_exposure_block_is_published_with_the_stats():
    # A cap that never fires does nothing; a cap that fires on everything is an off
    # switch. Neither is visible from the settled rows, because a block leaves no row.
    trial = _blank()
    record(trial, [_ticket(outcome="Mets")], stake=50.0)
    record(trial, [_ticket(outcome="Pirates")], stake=50.0)
    record(trial, [_ticket(outcome="Pirates")], stake=50.0)
    exp = stats(trial)["exposure"]
    assert exp["blocked"] == 1
    reason = "opposing side of this market already held"
    assert exp["by_reason"][reason] == {"distinct": 1, "times": 2}


from predictionedge.papertrial import CHANGE_POINTS  # noqa: E402


def test_every_change_point_carries_what_the_page_needs_to_label_it():
    # The Trial page renders these directly. A missing label is a nameless button and a
    # missing note is a date with no explanation of why anyone should slice there.
    for c in CHANGE_POINTS:
        assert set(c) == {"key", "at", "commit", "label", "note"}
        assert c["label"] and c["note"] and c["commit"]
        assert isinstance(c["at"], int)


def test_change_points_are_in_order_and_distinct():
    # Out-of-order entries would render "before X" windows that contain rows opened
    # after X, which is the one thing a window selector must never do quietly.
    ats = [c["at"] for c in CHANGE_POINTS]
    assert ats == sorted(ats)
    assert len(set(ats)) == len(ats)
    assert len({c["key"] for c in CHANGE_POINTS}) == len(CHANGE_POINTS)


def test_the_change_points_ship_with_the_stats():
    # Published, not hardcoded in the page: the windows can then only ever name changes
    # that are actually recorded here.
    assert stats(_blank())["changes"] == [dict(c) for c in CHANGE_POINTS]


def test_a_change_point_predates_the_rows_it_is_meant_to_split():
    # The trial started 2026-08-11; a boundary before that would produce an empty
    # "before" window that reads as "the old rules never won" rather than "no data".
    assert all(c["at"] > 1786459633 for c in CHANGE_POINTS)


# --- the fee correction: forward-only in the rows, backward at scoring time ---------
#
# The trial charged the Kalshi MAKER fee - a quarter of taker - on fills that crossed
# the spread by construction, for the whole of its first sample. The forward half of
# the fix is above (`record` has no maker knob left). This is the backward half, and
# its hard constraint is that a settled row is NEVER edited: the correction happens on
# copies, at scoring time, every time the stats are built.

from predictionedge.papertrial import (corrected_fee, on_taker_fees,  # noqa: E402
                                       rested_and_filled)


def _maker_booked(price=0.4, contracts=190, won=True):
    """A settled row of the shape already in `docs/trial.json`: taker fill, maker fee."""
    fee = trade_fee(price, contracts, multiplier=0.07, maker=True)
    payout = contracts * (1.0 - price) if won else -contracts * price
    return {"key": "0xabc:mets", "market_id": "0xabc", "outcome": "Mets",
            "title": "Mets vs Pirates", "url": "https://x/0xabc", "source": "whale",
            "price": price, "contracts": float(contracts),
            "stake": round(contracts * price, 4), "fee": fee,
            "opened_at": 1786500000.0, "settled_at": 1786600000.0,
            "won": won, "pred": price, "realized": round(payout - fee, 4)}


def test_the_scoring_layer_recharges_a_maker_booked_row_at_taker():
    row = _maker_booked()
    taker = trade_fee(0.4, 190, multiplier=0.07, maker=False)
    assert corrected_fee(row) == taker
    scored = on_taker_fees([row])[0]
    assert scored["fee"] == taker
    assert scored["fee_as_recorded"] == row["fee"]
    assert scored["realized"] == round(row["realized"] + row["fee"] - taker, 4)
    assert scored["realized"] < row["realized"]        # the correction costs money


def test_the_scoring_layer_does_not_mutate_the_row_it_corrects():
    # The one claim this record has over a spreadsheet is that a row is written once and
    # never touched. A correction that edits the row would forfeit exactly that.
    row = _maker_booked()
    before = json.dumps(row, sort_keys=True)
    on_taker_fees([row])
    assert json.dumps(row, sort_keys=True) == before


def test_a_row_already_charged_taker_is_left_exactly_alone():
    trial = _blank()
    record(trial, [_ticket(price=0.4)], stake=100.0, fee_multiplier=0.07)
    row = trial["open"][0]
    assert corrected_fee(row) == row["fee"]
    assert on_taker_fees([row])[0] is row


def test_a_genuine_resting_fill_keeps_its_maker_fee_through_scoring():
    # `_open_from_pending` is the one path that EARNS the discount: it rested at a bid
    # and an ask came to it. Recharging that at taker would be as wrong as the bug.
    trial = _blank()
    record(trial, [_weather_ticket(bid=0.06)], stake=100.0, maker_first=True)
    check_fills(trial, _q(ask=0.06))
    row = trial["open"][0]
    assert rested_and_filled(row) is True
    assert corrected_fee(row) == row["fee"] == 0.0
    assert on_taker_fees([row])[0] is row


def test_a_taker_fallback_is_not_treated_as_a_resting_fill():
    # It has `fill_polls` because it came through the pending path, but it crossed.
    trial = _blank()
    record(trial, [_weather_ticket(bid=0.06)], stake=100.0, maker_first=True)
    check_fills(trial, _q(ask=0.09), max_polls=1, fallback_taker=True)
    row = trial["open"][0]
    assert rested_and_filled(row) is False
    assert corrected_fee(row) == row["fee"]     # already taker, so nothing to correct


def test_an_unreproducible_row_falls_back_to_what_was_recorded():
    # None means "we cannot establish what this should have cost", never "zero". Any
    # other answer would put a guess into the headline in the name of correcting it.
    for bad in ({"price": 0.0}, {"price": 1.0}, {"price": None}, {"contracts": None},
                {"fee": 999.0}):
        row = {**_maker_booked(), **bad}
        assert corrected_fee(row) is None
        assert on_taker_fees([row])[0] is row


def test_the_index_fee_schedule_is_corrected_on_its_own_multiplier():
    # S&P/Nasdaq ladders are 0.035, not 0.07. Correcting one at the general rate would
    # double its modelled cost, so the schedule is identified by exact match.
    from predictionedge.fees import INDEX_FEE_MULTIPLIER
    row = {**_maker_booked(),
           "fee": trade_fee(0.4, 190, multiplier=INDEX_FEE_MULTIPLIER, maker=True)}
    assert corrected_fee(row) == trade_fee(0.4, 190, multiplier=INDEX_FEE_MULTIPLIER,
                                           maker=False)


def test_the_headline_is_scored_on_corrected_fees_and_publishes_the_old_basis():
    trial = _blank()
    trial["settled"] = [{**_maker_booked(), "key": f"0x{i}:mets"} for i in range(40)]
    out = stats(trial)
    taker = trade_fee(0.4, 190, multiplier=0.07, maker=False)
    corrected = on_taker_fees(trial["settled"])
    assert out["n"] == 40
    assert out["mean_return"] == evaluate(corrected)["mean_return"]
    # Both bases published; the headline is the corrected one and is strictly worse.
    assert out["as_recorded"]["mean_return"] == evaluate(trial["settled"])["mean_return"]
    assert out["mean_return"] < out["as_recorded"]["mean_return"]
    # The gate, the DSR and the bootstrap CI all read the corrected rows.
    for k in ("deploy", "dsr", "psr_vs_zero", "bootstrap_ci", "sharpe_per_bet"):
        assert out[k] == evaluate(corrected)[k]
    # Per-row deltas, published so the page can slice its own windows on this basis.
    assert out["fee_adjustment"]["0x0:mets"] == round(
        trial["settled"][0]["fee"] - taker, 4)
    assert len(out["fee_adjustment"]) == 40


def test_the_sleeve_breakdown_is_corrected_too():
    # A headline on one basis and its own breakdown on another is the failure mode.
    trial = _blank()
    trial["settled"] = [{**_maker_booked(), "key": f"0x{i}:mets"} for i in range(40)]
    out = stats(trial)
    assert out["by_source"]["whale"]["mean_return"] == out["mean_return"]


# --- the copy signal's own inputs are recorded ------------------------------------

def test_the_wallets_and_the_signal_age_are_recorded_on_the_row():
    # "A good wallet bought this" is the whole thesis of the whale sleeve, and the
    # record held neither the wallet nor how stale the buy was when we copied it.
    trial = _blank()
    wallets = [["0xF00d", 8000.0], ["0xBeeF", 4000.0]]
    record(trial, [_ticket(minutes_ago=42, wallet_usd=wallets)], stake=100.0)
    row = trial["open"][0]
    assert row["minutes_ago"] == 42
    assert row["wallet_usd"] == wallets       # plaintext: joinable to the leaderboard


def test_the_new_fields_are_forward_only_and_never_invented():
    # `bridge._carry` copies by key and writes None where the origin had nothing; a row
    # claiming "wallet_usd: null" would assert knowledge it does not have.
    trial = _blank()
    record(trial, [_ticket()], stake=100.0)
    record(trial, [_ticket(mid="0xdef", minutes_ago=None, wallet_usd=None)], stake=100.0)
    for row in trial["open"]:
        assert "wallet_usd" not in row and "minutes_ago" not in row


# --- price marks on open rows ------------------------------------------------------
# The record held an entry price and a settlement and nothing between them, so "was
# this ever underwater" was not a hard question, it was an unaskable one.

from predictionedge.papertrial import (MARK_CAP, _thin_marks,  # noqa: E402
                                       mark_open)


def _live(bid, ask, outcomes=("Mets", "Pirates")):
    """A settlement meta for a market that has NOT resolved but is quoting a book."""
    return {"closed": False, "outcomes": list(outcomes), "prices": [],
            "best_bid": bid, "best_ask": ask}


def test_marks_accumulate_across_polls():
    trial = _blank()
    record(trial, [_ticket(price=0.5)], stake=100.0)
    metas = {"0xabc": _live(0.49, 0.51)}
    for i in range(3):
        assert mark_open(trial, metas, now=1000 + 900 * i) == 1
    assert trial["open"][0]["marks"] == [
        {"t": 1000, "best_bid": 0.49, "best_ask": 0.51},
        {"t": 1900, "best_bid": 0.49, "best_ask": 0.51},
        {"t": 2800, "best_bid": 0.49, "best_ask": 0.51}]


def test_the_mark_is_the_book_for_the_leg_this_row_bought():
    # Both adapters quote the FIRST outcome's token. Half the open rows are on the
    # other leg, whose book is the complement with the spread flipped - storing the
    # first leg's numbers on them would invert the sign of "underwater".
    trial = _blank()
    record(trial, [_ticket(mid="0xa", outcome="Mets", price=0.5),
                   _ticket(mid="0xb", outcome="Pirates", price=0.5)], stake=100.0)
    mark_open(trial, {"0xa": _live(0.40, 0.44), "0xb": _live(0.40, 0.44)}, now=1000)
    mets, pirates = trial["open"]
    assert mets["marks"] == [{"t": 1000, "best_bid": 0.40, "best_ask": 0.44}]
    assert pirates["marks"] == [{"t": 1000, "best_bid": 0.56, "best_ask": 0.60}]


@pytest.mark.parametrize("meta", [
    {"closed": False, "outcomes": ["Mets", "Pirates"], "best_bid": 0.0, "best_ask": 0.0},
    {"closed": False, "outcomes": ["Mets", "Pirates"], "best_bid": 0.6, "best_ask": 0.4},
    {"closed": False, "outcomes": ["Yankees", "Pirates"], "best_bid": 0.4, "best_ask": 0.5},
    {"closed": False, "outcomes": [], "best_bid": 0.4, "best_ask": 0.5},
    {"closed": False, "outcomes": ["Mets", "Pirates"]},
    {},
])
def test_an_unreadable_quote_leaves_a_gap_rather_than_a_wrong_mark(meta):
    # All-zero (an absent field is indistinguishable from an empty book), crossed, a
    # leg this row never bought, no legs at all, no book at all. A missing mark is a
    # gap in the series; a guessed one is a false answer to the question marks exist for.
    trial = _blank()
    record(trial, [_ticket(price=0.5)], stake=100.0)
    assert mark_open(trial, {"0xabc": meta}, now=1000) == 0
    assert "marks" not in trial["open"][0]


def test_the_series_is_capped_however_long_the_position_is_held():
    # 96 polls a day against a file committed every 15 minutes: uncapped, one row
    # would add ~9.6 KB a day to the public record forever.
    trial = _blank()
    record(trial, [_ticket(price=0.5)], stake=100.0)
    for i in range(400):
        price = 0.30 + (i % 17) / 100.0
        mark_open(trial, {"0xabc": _live(round(price, 2), round(price + 0.02, 2))},
                  now=1000 + 900 * i)
        assert len(trial["open"][0]["marks"]) <= MARK_CAP
    assert len(trial["open"][0]["marks"]) == MARK_CAP


def test_thinning_keeps_the_first_the_last_and_both_extremes():
    # The extremes ARE the question. An even sample would delete the one spike it was
    # asked about and keep the flat hours either side of it.
    marks = [{"t": 1000 + 900 * i, "best_bid": 0.50, "best_ask": 0.52}
             for i in range(200)]
    marks[57] = {"t": marks[57]["t"], "best_bid": 0.11, "best_ask": 0.13}    # the low
    marks[132] = {"t": marks[132]["t"], "best_bid": 0.88, "best_ask": 0.90}  # the high
    kept = _thin_marks(marks, MARK_CAP)
    assert len(kept) == MARK_CAP
    assert kept[0] == marks[0] and kept[-1] == marks[-1]
    assert marks[57] in kept and marks[132] in kept
    assert kept == sorted(kept, key=lambda m: m["t"])   # still a series, still in order


def test_the_extremes_survive_hundreds_of_polls_not_just_one_thinning():
    # Thinning runs on every poll, so the worst mid ever printed has to survive being
    # re-thinned dozens of times, not once.
    trial = _blank()
    record(trial, [_ticket(price=0.5)], stake=100.0)
    for i in range(300):
        bid = 0.05 if i == 40 else (0.95 if i == 210 else 0.50)
        mark_open(trial, {"0xabc": _live(bid, round(bid + 0.02, 2))},
                  now=1000 + 900 * i)
    mids = [(m["best_bid"] + m["best_ask"]) / 2 for m in trial["open"][0]["marks"]]
    assert min(mids) == pytest.approx(0.06)
    assert max(mids) == pytest.approx(0.96)


def test_a_settled_rows_marks_are_frozen():
    # A mark is data appended to a position with no result yet. A settled row is a
    # committed result, and this module never edits one - `settle` has already moved
    # it out of `open` by the time marks are taken, so it is not even reachable.
    trial = _blank()
    record(trial, [_ticket(price=0.5)], stake=100.0)
    mark_open(trial, {"0xabc": _live(0.49, 0.51)}, now=1000)
    settle(trial, {"0xabc": _meta(["Mets", "Pirates"], [1.0, 0.0])})
    frozen = [dict(m) for m in trial["settled"][0]["marks"]]
    assert mark_open(trial, {"0xabc": _live(0.97, 0.99)}, now=1900) == 0
    assert trial["settled"][0]["marks"] == frozen


def test_marking_never_reaches_the_network(monkeypatch):
    # The claim is that marks ride on the batch the settle path already fetched. If a
    # quote were ever pulled per row, this run would be 30 round trips and would raise.
    from predictionedge import gamma, kalshi

    def boom(*a, **k):
        raise AssertionError("a mark must never cost its own network call")
    monkeypatch.setattr(gamma, "_default_fetch", boom)
    monkeypatch.setattr(kalshi, "_default_fetch", boom)

    trial = _blank()
    record(trial, [_ticket(mid=f"0x{i}", price=0.5) for i in range(30)], stake=100.0)
    metas = {f"0x{i}": _live(0.49, 0.51) for i in range(30)}
    assert mark_open(trial, metas, now=1000) == 30


def test_a_pmus_row_is_not_marked_with_the_market_it_was_copied_from():
    # PM-US rows settle against their origin market because PM-US answers no
    # resolution endpoint. That is sound for "which way did it resolve" and unsound
    # for a price: the venue basis is real, and it is not this row's drift.
    trial = _blank()
    record(trial, [_ticket(mid="pmus-mets", price=0.5, venue="polymarket-us",
                           origin_market_id="0xabc")], stake=100.0)
    assert mark_open(trial, {"pmus-mets": _live(0.49, 0.51)}, now=1000) == 0
    assert "marks" not in trial["open"][0]


def test_one_batch_fetch_covers_every_open_row(monkeypatch, tmp_path):
    """End to end: 40 open rows, one metadata call, 40 rows marked.

    The whole design of `mark_open` is that it is free - it reads the batch the settle
    path fetched anyway. A per-row fetch would still pass every test above and would be
    a 40x regression in a job that runs every 15 minutes, so the call count is asserted.
    """
    from predictionedge import gamma, papertrial
    from predictionedge.config import Config

    monkeypatch.setattr(Config, "from_env", classmethod(
        lambda cls: Config(bridge_enabled=False, maker_first=False)))
    calls = []

    def one_batch(ids, **kw):
        calls.append(list(ids))
        return {i: _live(0.49, 0.51) for i in ids}
    monkeypatch.setattr(gamma, "market_meta", one_batch)

    trial = _blank()
    record(trial, [_ticket(mid=f"0x{i}", price=0.5) for i in range(40)], stake=100.0)
    trial_path = tmp_path / "trial.json"
    save(trial_path, trial)
    board = tmp_path / "board.json"
    board.write_text(json.dumps({"tickets": [], "generated_at": 1000}), encoding="utf-8")

    assert papertrial.main(["--board", str(board), "--trial", str(trial_path)]) == 0
    assert len(calls) == 1 and len(calls[0]) == 40
    out = load(trial_path)
    assert len(out["open"]) == 40
    assert all(len(r["marks"]) == 1 for r in out["open"])
