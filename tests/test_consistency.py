"""Every fixture here is shaped from a real live Gamma payload seen on 2026-08-06.

The "should be rejected" cases are the important half. Each one is a group whose live
prices, read naively, print a double-digit-cent edge that does not exist:

  - `what-will-the-fed-rate-be-at-the-end-of-2026` is a negRisk set of EXACT buckets
    ("be 3.75%", "be 4.0%") priced 0.0045 .. 0.376. As a ">=" ladder that is a 27c
    inversion on nearly every pair, and all of it is fiction.
  - `netanyahu-out-before-2027` keeps settled legs in a live event with `closed: true`,
    `bestBid: null` and prices ["0","1"]. Read as quotes they invent a violation against
    every remaining rung - the same shape as the market_meta incident, where an empty
    upstream let every check pass instead of failing closed.
  - `presidential-election-winner-2028` shares 70 leg names with BOTH party-nominee
    events, plus filler legs "Person AA".."Person BL". Matching legs by name across
    events automatically asserts P(Vance wins) <= P(Vance wins the DEM nomination).
"""

from dataclasses import dataclass

import pytest

from predictionedge.consistency import (
    DOMINANCE_RULES,
    Leg,
    bracket_violation,
    deadline_from_question,
    dominance_violations,
    event_is_negrisk,
    ladder_rungs,
    ladder_violations,
    numeric_threshold,
    parse_leg,
    scan,
    scan_events,
)

END = "2027-01-01T05:00:00Z"


@dataclass
class Cfg:
    """Only the four knobs the scanner reads, so tests never touch real config."""
    consistency_enabled: bool = True
    consistency_min_edge_c: float = 2.0
    consistency_max_events: int = 400
    consistency_min_liquidity: float = 5_000.0


def mk(name, question, bid, ask, *, liq=50_000.0, end=END, closed=False,
       neg_risk=False, cid=None, **extra):
    m = {
        "conditionId": cid or f"0x{abs(hash((name, question, end))) :064x}"[:66],
        "groupItemTitle": name, "question": question,
        "bestBid": bid, "bestAsk": ask, "liquidity": liq,
        "endDate": end, "closed": closed, "active": True,
        "acceptingOrders": True, "negRisk": neg_risk,
    }
    m.update(extra)
    return m


def ev(slug, markets, *, neg_risk=False):
    return {"slug": slug, "active": True, "closed": False,
            "negRisk": neg_risk, "markets": markets}


def legs_of(event):
    return [l for l in (parse_leg(m, event["slug"]) for m in event["markets"])
            if l is not None]


def run(events, **kw):
    kw.setdefault("min_edge_c", 2.0)
    kw.setdefault("min_liquidity", 5_000.0)
    return scan_events(events, **kw)


# --- threshold parsing ------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("$500M", 500_000_000.0),
    ("$1.5B", 1_500_000_000.0),
    ("40+", 40.0),
    ("3.75%", 3.75),
    (">= 4.5%", 4.5),
    ("150,000", 150_000.0),
])
def test_numeric_threshold_parses_live_label_shapes(text, expected):
    assert numeric_threshold(text) == expected


@pytest.mark.parametrize("text", ["May 31", "Gavin Newsom", "6 or lower", "", "Other"])
def test_non_numeric_labels_return_none(text):
    assert numeric_threshold(text) is None


# --- parse_leg fails closed -------------------------------------------------

def test_settled_leg_in_a_live_event_is_rejected():
    # netanyahu-out-before-2027: closed rung, null bid, prices ["0","1"].
    assert parse_leg(mk("March 31", "Netanyahu out by March 31?", None, 0.001,
                        closed=True, liq=None), "e") is None


@pytest.mark.parametrize("bid,ask,liq", [
    (None, 0.40, 50_000.0),      # no bid: nothing to sell into
    (0.40, None, 50_000.0),      # no ask: nothing to buy
    (0.0, 0.40, 50_000.0),       # empty book on one side
    (0.40, 0.0, 50_000.0),
    (0.50, 0.45, 50_000.0),      # crossed: bad data, not free money
    (0.40, 0.41, None),          # unknown depth is not "enough depth"
])
def test_unpriceable_legs_are_rejected(bid, ask, liq):
    assert parse_leg(mk("$1B", "FDV above $1B?", bid, ask, liq=liq), "e") is None


def test_a_good_leg_parses():
    leg = parse_leg(mk("$1B", "FDV above $1B?", 0.40, 0.41), "opensea")
    assert leg is not None and leg.bid == 0.40 and leg.ask == 0.41
    assert leg.url == "https://polymarket.com/event/opensea"


# --- family 1: ladder monotonicity ------------------------------------------

def _fdv(*rungs, slug="opensea-fdv-above-one-day-after-launch", neg_risk=False):
    """rungs: (label, bid, ask[, kwargs]) - the live 'FDV above $X' ladder shape."""
    return ev(slug, [mk(label, f"Opensea FDV above {label} one day after launch?",
                        bid, ask, **kw)
                     for label, bid, ask, *rest in rungs
                     for kw in [rest[0] if rest else {}]], neg_risk=neg_risk)


def test_ladder_inversion_fires_with_the_hand_checked_gap():
    # P(above $1B) must be <= P(above $100M), yet $1B bids 0.40 while $100M offers 0.30.
    # Buy YES $100M at 0.30 + NO $1B at 0.60 = 0.90 for a basket that never pays under
    # 1.00 -> 10.00c, already net of both spreads.
    rep = run([_fdv(("$100M", 0.29, 0.30), ("$1B", 0.40, 0.41))])
    assert len(rep.violations) == 1
    v = rep.violations[0]
    assert v.family == "ladder" and v.edge_c == pytest.approx(10.0)
    assert [l.name for l in v.legs] == ["$100M", "$1B"]


def test_correctly_priced_ladder_is_silent():
    rep = run([_fdv(("$100M", 0.29, 0.30), ("$1B", 0.10, 0.11), ("$5B", 0.04, 0.05))])
    assert rep.violations == []


def test_violation_smaller_than_the_spread_is_not_an_edge():
    # ask(low)=0.30, bid(high)=0.31 -> 1.0c, under the 2.0c floor.
    rep = run([_fdv(("$100M", 0.29, 0.30), ("$1B", 0.31, 0.33))])
    assert rep.violations == []
    assert rep.rejected.get("ladder inversion smaller than the spread") == 1


def test_exclusive_buckets_never_reach_the_ladder_family():
    """The fed-rate lesson: negRisk beats wording, even when the labels look laddered.

    These prices would print a 40c ladder 'edge'. They are exact buckets: exactly one
    resolves YES, so P(bucket $1B) > P(bucket $100M) is perfectly legal.
    """
    event = _fdv(("$100M", 0.28, 0.30), ("$1B", 0.70, 0.72), neg_risk=True)
    assert event_is_negrisk(event)
    rep = run([event])
    assert rep.violations == []          # and no bracket edge either: 1.02 / 0.98


def test_exact_bucket_wording_is_rejected_even_without_the_negrisk_flag():
    # Belt and braces: "be 3.75% at the end of 2026" states no direction at all.
    event = ev("fed-rate", [
        mk("3.75%", "Will the upper bound of the target federal funds rate be 3.75% "
                    "at the end of 2026?", 0.26, 0.29),
        mk("4.0%", "Will the upper bound of the target federal funds rate be 4.0% "
                   "at the end of 2026?", 0.34, 0.40),
    ])
    rungs, _kind, why = ladder_rungs(event, legs_of(event))
    assert rungs is None and "one-sided wording" in why


def test_below_ladder_runs_the_other_way():
    # P(below $1B) >= P(below $100M): the LARGER threshold is the easier claim.
    event = ev("x-below", [
        mk("$100M", "Will FDV be below $100M?", 0.29, 0.30),
        mk("$1B", "Will FDV be below $1B?", 0.10, 0.11),
    ])
    rungs, kind, why = ladder_rungs(event, legs_of(event))
    assert kind == "numeric" and [l.name for l in rungs] == ["$1B", "$100M"]
    # ask($1B)=0.11 < bid($100M)=0.29 -> the easier claim is 18c cheaper. Violation.
    hits = ladder_violations(rungs, min_edge_c=2.0, min_liquidity=5_000.0)
    assert len(hits) == 1 and hits[0].edge_c == pytest.approx(18.0)


def test_rungs_with_different_end_dates_are_different_questions():
    event = _fdv(("$100M", 0.29, 0.30), ("$1B", 0.40, 0.41, {"end": "2028-01-01T00:00:00Z"}))
    rungs, _kind, why = ladder_rungs(event, legs_of(event))
    assert rungs is None and "different end dates" in why


def test_duplicate_thresholds_are_dropped_not_compared():
    """opensea lists "$100M" twice. Equal rungs assert equality, not an ordering.

    Two live markets on one rung either duplicate each other or differ in a way the
    payload does not show. Either reading makes them useless as a monotonicity test, and
    the 15c gap between the two $100M quotes below would otherwise pair against $1B.
    """
    event = _fdv(("$100M", 0.29, 0.30, {"cid": "0xa"}),
                 ("$100M", 0.44, 0.45, {"cid": "0xb"}),
                 ("$1B", 0.10, 0.11), ("$5B", 0.20, 0.21))
    rungs, _kind, _why = ladder_rungs(event, legs_of(event))
    assert [l.name for l in rungs] == ["$1B", "$5B"]
    # And what survives is still checked: ask($1B)=0.11 < bid($5B)=0.20 -> 9c.
    hits = ladder_violations(rungs, min_edge_c=2.0, min_liquidity=5_000.0)
    assert len(hits) == 1 and hits[0].edge_c == pytest.approx(9.0)


def test_duplicates_that_leave_one_rung_reject_the_group():
    event = _fdv(("$100M", 0.29, 0.30, {"cid": "0xa"}),
                 ("$100M", 0.44, 0.45, {"cid": "0xb"}), ("$1B", 0.40, 0.41))
    rungs, _kind, why = ladder_rungs(event, legs_of(event))
    assert rungs is None and "too few rungs" in why


def test_an_unpriceable_rung_cannot_be_compared_and_the_rest_still_can():
    event = _fdv(("$100M", 0.29, 0.30),
                 ("$500M", None, 0.35),          # dead book: never a leg
                 ("$1B", 0.40, 0.41))
    rep = run([event])
    assert len(rep.violations) == 1
    assert {l.name for l in rep.violations[0].legs} == {"$100M", "$1B"}


def test_a_group_we_cannot_read_is_never_declared_monotone():
    event = _fdv(("$100M", 0.29, 0.30), ("$1B", 0.40, 0.41))
    event["markets"].append(mk("Other", "Opensea FDV something else?", 0.05, 0.06))
    rungs, _kind, why = ladder_rungs(event, legs_of(event))
    assert rungs is None and "not thresholds or deadlines" in why


# --- family 1b: deadline ladders --------------------------------------------

def _deadline(*rungs, slug="will-russia-capture-lyman-in-2025"):
    """rungs: (label, deadline-in-the-question, bid, ask).

    Every rung deliberately carries the SAME stale ``endDate``, copied from the live
    lyman event where "by December 31, 2026" ships with an endDate of 2025-12-31. Any
    implementation that keys the ladder off endDate cannot order these fixtures.
    """
    return ev(slug, [mk(label, f"Will Russia capture Lyman by {when}?", bid, ask,
                        end="2025-12-31T23:55:00Z")
                     for label, when, bid, ask in rungs])


def test_later_deadline_cannot_be_cheaper_than_an_earlier_one():
    # P(by Sep 30) <= P(by Dec 31). Sep bids 0.50 while Dec offers 0.31 -> 19c.
    event = _deadline(("September 30", "September 30, 2026", 0.50, 0.51),
                      ("December 31", "December 31, 2026", 0.30, 0.31))
    rep = run([event])
    assert len(rep.violations) == 1
    v = rep.violations[0]
    assert v.edge_c == pytest.approx(19.0)
    assert [l.name for l in v.legs] == ["December 31", "September 30"]


def test_correctly_ordered_deadlines_are_silent():
    """The exact live shape that six false positives were printed for.

    `will-russia-capture-lyman-in-2025` prices Sep 30 at 0.13 and Dec 31 at 0.53. That
    is *correct* - a later deadline is a superset - but with endDate as the sort key both
    rungs share 2025-12-31, the order came out backwards, and the scanner reported a
    confident 40c edge on a perfectly consistent ladder.
    """
    event = _deadline(("September 30", "September 30, 2026", 0.12, 0.13),
                      ("December 31", "December 31, 2026", 0.53, 0.54))
    rep = run([event])
    assert rep.violations == []


def test_a_rung_with_no_year_rejects_the_group():
    # Live: "by December 31?" (2025) and "by December 31, 2026?" sit in one event.
    event = _deadline(("September 30", "September 30, 2026", 0.12, 0.13),
                      ("December 31", "December 31", 0.53, 0.54))
    rungs, _kind, why = ladder_rungs(event, legs_of(event))
    assert rungs is None and "not thresholds or deadlines" in why


def test_end_of_year_wording_parses():
    assert deadline_from_question("Netanyahu out by end of 2026?").year == 2026
    assert deadline_from_question("Netanyahu out by May 31?") is None


def test_settled_deadline_rungs_do_not_manufacture_a_violation():
    """The live netanyahu event: settled rungs at ["0","1"] alongside one real quote."""
    event = _deadline(("December 31", "December 31, 2026", 0.39, 0.40))
    for label, when in [("March 31", "March 31, 2026"), ("April 30", "April 30, 2026"),
                        ("May 31", "May 31, 2026")]:
        event["markets"].append(
            mk(label, f"Will Russia capture Lyman by {when}?", None, 0.001,
               end="2025-12-31T23:55:00Z", closed=True, liq=None))
    rep = run([event])
    assert rep.violations == []
    assert any("fewer than two live" in r for r in rep.rejected)


# --- family 2: bracket sums -------------------------------------------------

def _bracket(*prices, slug="how-many-strikes"):
    return ev(slug, [mk(str(i), f"Will there be exactly {i} strikes in 2026?",
                        bid, ask, neg_risk=True)
                     for i, (bid, ask) in enumerate(prices)], neg_risk=True)


def test_underround_bracket_fires():
    # 3 exclusive outcomes offered at 0.90 total for a set that pays exactly 1.00.
    rep = run([_bracket((0.28, 0.30), (0.28, 0.30), (0.28, 0.30))])
    assert len(rep.violations) == 1
    v = rep.violations[0]
    assert v.family == "bracket" and v.edge_c == pytest.approx(10.0)
    assert all(a.startswith("BUY YES") for a in v.actions) and len(v.actions) == 3


def test_overround_bracket_fires_the_other_way():
    rep = run([_bracket((0.40, 0.42), (0.40, 0.42), (0.40, 0.42))])
    v = rep.violations[0]
    assert v.edge_c == pytest.approx(20.0)
    assert all(a.startswith("BUY NO") for a in v.actions)


def test_balanced_bracket_is_silent():
    assert run([_bracket((0.32, 0.35), (0.32, 0.35), (0.32, 0.35))]).violations == []


def test_bracket_underround_inside_the_spread_is_not_an_edge():
    # sum(asks) = 0.99 -> 1.0c, under the floor.
    assert run([_bracket((0.31, 0.33), (0.31, 0.33), (0.31, 0.33))]).violations == []


def test_one_unpriceable_leg_rejects_the_whole_bracket():
    """A partial set is not a partition. This is the fail-closed rule, not a nicety:

    dropping the leg you cannot price turns a hedge into a directional bet carrying an
    arbitrage's position size.
    """
    event = _bracket((0.28, 0.30), (0.28, 0.30), (0.28, 0.30))
    event["markets"][1]["bestBid"] = None
    rep = run([event])
    assert rep.violations == []
    assert rep.rejected.get("bracket has legs we cannot price (fail closed)") == 1


def test_bracket_too_wide_to_place_by_hand_is_rejected():
    rep = run([_bracket(*[(0.02, 0.03)] * 14)], max_bracket_legs=12)
    assert rep.violations == []
    assert rep.rejected.get("bracket has too many legs to place by hand") == 1


def test_illiquid_bracket_leg_rejects_the_set():
    event = _bracket((0.28, 0.30), (0.28, 0.30), (0.28, 0.30))
    event["markets"][2]["liquidity"] = 100.0
    rep = run([event])
    assert rep.violations == []
    assert rep.rejected.get("bracket leg too illiquid to fill") == 1


def test_bracket_with_impossible_book_is_data_error_not_edge():
    legs = [Leg("0x1", "e", "q1", "A", bid=0.60, ask=0.30, liquidity=50_000.0, end_iso=END),
            Leg("0x2", "e", "q2", "B", bid=0.60, ask=0.30, liquidity=50_000.0, end_iso=END)]
    assert bracket_violation(legs, min_edge_c=2.0, event_slug="e") is None


# --- family 3: dominance ----------------------------------------------------

GOP = next(r for r in DOMINANCE_RULES
           if r.easier_event == "republican-presidential-nominee-2028")


def _dom_leg(slug, name, question, bid, ask, liq=50_000.0):
    return Leg(f"0x{name}", slug, question, name, bid, ask, liq, END)


def _pair(name, *, win_bid, win_ask, nom_bid, nom_ask, **kw):
    harder = _dom_leg(GOP.harder_event, name,
                      f"Will {name} win the 2028 US Presidential Election?",
                      win_bid, win_ask, **kw)
    easier = _dom_leg(GOP.easier_event, name,
                      f"Will {name} win the 2028 Republican presidential nomination?",
                      nom_bid, nom_ask, **kw)
    return harder, easier


def _dom(harder, easier, **kw):
    kw.setdefault("min_edge_c", 2.0)
    kw.setdefault("min_liquidity", 5_000.0)
    return dominance_violations(GOP, [harder], [easier], **kw)


def test_dominance_fires_when_the_harder_claim_outbids_its_prerequisite():
    # P(Rubio wins the election) cannot exceed P(Rubio is the nominee), yet the election
    # leg bids 0.40 while the nomination leg offers 0.30 -> 10c.
    hits = _dom(*_pair("Marco Rubio", win_bid=0.40, win_ask=0.42,
                       nom_bid=0.29, nom_ask=0.30))
    assert len(hits) == 1 and hits[0].edge_c == pytest.approx(10.0)
    assert "third-party" in " ".join(hits[0].warnings)


def test_dominance_is_silent_when_the_ordering_holds():
    assert _dom(*_pair("Marco Rubio", win_bid=0.21, win_ask=0.22,
                       nom_bid=0.43, nom_ask=0.44)) == []


def test_unaudited_legs_are_never_paired():
    """The crossvenue failure, reproduced exactly - and blocked.

    'Gavin Newsom' really is listed in the Republican nominee event (at 0.0005) and in
    the election-winner event (at 0.1095). Name-matching automatically prints a 10c edge
    on the claim that Newsom cannot win without the GOP nomination. He is not on this
    rule's audited leg list, so no pair is formed.
    """
    assert _dom(*_pair("Gavin Newsom", win_bid=0.40, win_ask=0.42,
                       nom_bid=0.29, nom_ask=0.30)) == []
    assert _dom(*_pair("Person AA", win_bid=0.40, win_ask=0.42,
                       nom_bid=0.29, nom_ask=0.30)) == []


def test_a_stale_rule_whose_questions_stopped_differing_is_dropped():
    same = "Will Marco Rubio win the 2028 Republican presidential nomination?"
    harder = _dom_leg(GOP.harder_event, "Marco Rubio", same, 0.40, 0.42)
    easier = _dom_leg(GOP.easier_event, "Marco Rubio", same, 0.29, 0.30)
    assert _dom(harder, easier) == []


def test_punctuation_does_not_split_one_candidate_into_two():
    harder = _dom_leg(GOP.harder_event, "JD Vance",
                      "Will JD Vance win the 2028 US Presidential Election?", 0.40, 0.42)
    easier = _dom_leg(GOP.easier_event, "J.D. Vance",
                      "Will J.D. Vance win the 2028 Republican presidential nomination?",
                      0.29, 0.30)
    assert len(_dom(harder, easier)) == 1


def test_dominance_gap_inside_the_spread_is_not_an_edge():
    assert _dom(*_pair("Marco Rubio", win_bid=0.31, win_ask=0.33,
                       nom_bid=0.29, nom_ask=0.30)) == []


def test_illiquid_dominance_pair_is_rejected():
    assert _dom(*_pair("Marco Rubio", win_bid=0.40, win_ask=0.42,
                       nom_bid=0.29, nom_ask=0.30, liq=100.0)) == []


def test_missing_prerequisite_event_rejects_rather_than_assumes():
    rep = run([])
    assert rep.violations == []
    assert rep.rejected.get("dominance rule: one of its events was not in the crawl") \
        == len(DOMINANCE_RULES)


# --- whole-scan behaviour ---------------------------------------------------

def test_empty_upstream_is_inconclusive_not_clean():
    """The market_meta incident: a silently-empty feed must never read as 'all clear'."""
    rep = scan(Cfg(), fetch=lambda url, params: [])
    assert rep.violations == []
    assert any("inconclusive" in n for n in rep.notes)


def test_disabled_scanner_does_nothing():
    rep = scan(Cfg(consistency_enabled=False),
               fetch=lambda url, params: (_ for _ in ()).throw(AssertionError("fetched")))
    assert rep.violations == [] and rep.events_scanned == 0


def test_single_binary_market_is_not_a_group():
    rep = run([ev("xi-jinping-out-before-2027",
                  [mk("", "Xi Jinping out before 2027?", 0.04, 0.05)])])
    assert rep.events_scanned == 0 and rep.violations == []


def test_module_has_no_order_path():
    """Advisory only. This module reads and prints, full stop."""
    from pathlib import Path

    import predictionedge.consistency as mod
    src = Path(mod.__file__).read_text(encoding="utf-8")
    for forbidden in ("LiveExecutor", "place_order", "submit_order", "executor"):
        assert forbidden not in src
