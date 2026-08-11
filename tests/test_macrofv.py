"""E3 macro fair value. No network: every test drives a fixture through the seam.

The bias of this suite is toward the *refusals*. A wrong number here looks exactly
like a right one - it is a plausible probability either way - so most of what is
pinned below is the set of conditions under which the module declines to answer.
"""

import json
import math
from dataclasses import replace

import pytest

from predictionedge.config import Config
from predictionedge.macrofv import (
    Bracket,
    _MEASURED_NOWCAST_RMSE_PP,
    _ref_month_from,
    bracket_bounds,
    bracket_prob,
    build_brackets,
    classify_cpi_event,
    compare_cpi,
    decompose_meeting,
    effective_sigma,
    fetch_nowcasts,
    fetch_target_range,
    ff_implied_average_rate,
    match_cpi_events,
    nowcast_for,
    one_meeting_outcome_probs,
    partition_is_sound,
    run_cpi,
    two_point_move_probs,
)

CFG = Config()


# ---------------------------------------------------------------------------
# Fed funds futures -> probability
# ---------------------------------------------------------------------------

def test_implied_average_rate_is_the_settlement_identity():
    assert ff_implied_average_rate(96.5) == pytest.approx(3.5)
    assert ff_implied_average_rate(100.0) == pytest.approx(0.0)


@pytest.mark.parametrize("bad", [0.0, -1.0, 100.01, 150.0])
def test_implied_average_rate_rejects_impossible_prices(bad):
    with pytest.raises(ValueError):
        ff_implied_average_rate(bad)


def test_decompose_meeting_hand_computed():
    # September 2026: 30 days, decision effective after day 17, entering at 3.625%.
    # avg = 100 - 96.3235 = 3.6765
    # after = (3.6765*30 - 3.625*17) / 13 = 48.670/13 = 3.74385
    dec = decompose_meeting(96.3235, rate_before=3.625, meeting_day=17, days_in_month=30)
    assert dec.implied_average == pytest.approx(3.6765, abs=1e-6)
    assert dec.rate_after == pytest.approx(3.74385, abs=1e-4)


def test_futures_probability_round_trip():
    """Build a price from a known hike probability; the maths must give it back."""
    r0, step, day, days = 3.625, 0.25, 17, 30
    for p_true in (0.0, 0.2, 0.475, 0.8, 1.0):
        r_after = r0 + step * p_true
        avg = (r0 * day + r_after * (days - day)) / days
        dec = decompose_meeting(100.0 - avg, rate_before=r0,
                                meeting_day=day, days_in_month=days)
        p_hold, p_move = two_point_move_probs(dec.rate_after, r0, step=step)
        assert p_move == pytest.approx(p_true, abs=1e-6)
        assert p_hold + p_move == pytest.approx(1.0)


def test_two_point_probs_direction_and_hold():
    assert two_point_move_probs(3.625, 3.625) == pytest.approx((1.0, 0.0))
    # a full 25bp cut priced with certainty
    assert two_point_move_probs(3.375, 3.625) == pytest.approx((0.0, 1.0))


def test_two_point_probs_refuse_multi_step_move():
    """A 50bp+ move cannot be represented by two points, so it must return None.

    Clipping to 1.0 here would silently assert zero probability on the open-ended
    "50+ bps" legs and manufacture an edge against them.
    """
    assert two_point_move_probs(4.30, 3.625) is None      # >25bp hike priced
    assert two_point_move_probs(3.00, 3.625) is None      # >25bp cut priced


def test_outcome_probs_fill_only_the_side_actually_priced():
    probs = one_meeting_outcome_probs(3.74385, 3.625)
    assert probs["25 bps increase"] == pytest.approx(0.4754, abs=1e-3)
    assert probs["no change"] == pytest.approx(0.5246, abs=1e-3)
    assert probs["25 bps decrease"] == 0.0
    assert probs["50+ bps increase"] == 0.0
    assert sum(probs.values()) == pytest.approx(1.0)

    cut = one_meeting_outcome_probs(3.50, 3.625)
    assert cut["25 bps decrease"] == pytest.approx(0.5)
    assert cut["25 bps increase"] == 0.0


def test_outcome_probs_refuse_rather_than_zero_the_tails():
    assert one_meeting_outcome_probs(4.30, 3.625) is None


def test_decompose_rejects_meeting_the_contract_cannot_price():
    # A decision on the final day of the month leaves no post-meeting days in the
    # contract window, so that contract carries no information about it.
    with pytest.raises(ValueError):
        decompose_meeting(96.5, rate_before=3.625, meeting_day=30, days_in_month=30)
    with pytest.raises(ValueError):
        decompose_meeting(96.5, rate_before=3.625, meeting_day=31, days_in_month=30)


def test_fetch_target_range_fails_closed():
    def boom(url, params):
        raise RuntimeError("NY Fed down")

    assert fetch_target_range(fetch=boom) is None
    # a malformed but successful response is just as fatal, and must not raise
    assert fetch_target_range(fetch=lambda u, p: {"refRates": []}) is None
    ok = fetch_target_range(
        fetch=lambda u, p: {"refRates": [{"targetRateFrom": 3.5, "targetRateTo": 3.75}]})
    assert ok == (3.5, 3.75)


# ---------------------------------------------------------------------------
# Bracket labels: the rounding-bin rule
# ---------------------------------------------------------------------------

def test_bracket_bounds_are_rounding_bins_not_points():
    """BLS prints one decimal, so "0.2%" wins on [0.15, 0.25), not on exactly 0.2."""
    assert bracket_bounds("0.2%") == pytest.approx((0.15, 0.25))
    assert bracket_bounds("3.4%") == pytest.approx((3.35, 3.45))
    assert bracket_bounds("0.0%") == pytest.approx((-0.05, 0.05))
    assert bracket_bounds("-0.6%") == pytest.approx((-0.65, -0.55))


def test_bracket_bounds_tails_open_half_a_tick_past_the_label():
    lo, hi = bracket_bounds("≤0.0%")
    assert lo == -math.inf and hi == pytest.approx(0.05)
    lo, hi = bracket_bounds("≥3.1%")
    assert lo == pytest.approx(3.05) and hi == math.inf
    lo, hi = bracket_bounds("0.6%+")
    assert lo == pytest.approx(0.55) and hi == math.inf
    lo, hi = bracket_bounds("≤-0.7%")
    assert lo == -math.inf and hi == pytest.approx(-0.65)


def test_bracket_bounds_accepts_prose_and_ascii_spellings():
    assert bracket_bounds("0.2% or less") == pytest.approx((-math.inf, 0.25))
    assert bracket_bounds("<=0.0%")[0] == -math.inf
    assert bracket_bounds("3.1% or more")[1] == math.inf


def test_bracket_bounds_refuses_junk():
    assert bracket_bounds("") is None
    assert bracket_bounds(None) is None
    assert bracket_bounds("Yes") is None
    assert bracket_bounds("≤ 0.2% ≥") is None       # contradictory: refuse to guess


def test_bracket_prob_matches_the_normal_by_hand():
    # mu=0.2, sigma=0.1, bin [0.15,0.25) -> Phi(0.5) - Phi(-0.5) = 0.382925
    assert bracket_prob(0.15, 0.25, 0.2, 0.1) == pytest.approx(0.382925, abs=1e-6)
    assert bracket_prob(-math.inf, 0.2, 0.2, 0.1) == pytest.approx(0.5)
    assert bracket_prob(0.2, math.inf, 0.2, 0.1) == pytest.approx(0.5)


def test_bracket_prob_rejects_zero_sigma():
    with pytest.raises(ValueError):
        bracket_prob(0.0, 1.0, 0.5, 0.0)


def _bk(label, prob=0.1):
    b = bracket_bounds(label)
    return Bracket(label, b[0], b[1], prob, f"Will it be {label}?", "0xabc", 1000.0)


def test_a_real_ladder_partitions_and_sums_to_one():
    labels = ["≤0.0%", "0.1%", "0.2%", "0.3%", "0.4%", "0.5%", "0.6%+"]
    brackets = [_bk(l) for l in labels]
    assert partition_is_sound(brackets)
    total = sum(bracket_prob(b.lo, b.hi, 0.21, 0.16) for b in brackets)
    assert total == pytest.approx(1.0, abs=1e-9)


def test_partition_rejects_gaps_overlaps_and_missing_tails():
    good = [_bk("≤0.0%"), _bk("0.1%"), _bk("0.2%+")]
    assert partition_is_sound(good)
    # gap: 0.1% bin removed
    assert not partition_is_sound([_bk("≤0.0%"), _bk("0.2%+")])
    # no bottom tail
    assert not partition_is_sound([_bk("0.1%"), _bk("0.2%+")])
    # no top tail
    assert not partition_is_sound([_bk("≤0.0%"), _bk("0.1%")])
    # overlap
    overlapping = [_bk("≤0.1%"), _bk("0.1%"), _bk("0.2%+")]
    assert not partition_is_sound(overlapping)
    assert not partition_is_sound([_bk("≤0.0%")])


# ---------------------------------------------------------------------------
# Claim matching
# ---------------------------------------------------------------------------

def test_classify_real_polymarket_titles():
    assert classify_cpi_event("Core CPI MoM - July 2026") == ("core_cpi", "mom", "2026-7")
    assert classify_cpi_event("Core CPI YoY - July 2026") == ("core_cpi", "yoy", "2026-7")
    d_ann = "inflation over the 12-month period ending July 2026, before seasonal adjustment"
    assert classify_cpi_event("July Inflation US - Annual", d_ann) == ("cpi", "yoy", "2026-7")
    d_mom = "the one-month percent change in the CPI in July 2026, seasonally adjusted"
    assert classify_cpi_event("July Inflation US - Monthly", d_mom) == ("cpi", "mom", "2026-7")


def test_classify_rejects_non_us_inflation():
    """These share nearly every token with the US markets and are nowcast by nobody here."""
    for title in ("Japan Core CPI YoY in 2026", "Brazil Annual Inflation 2026",
                  "U.K. Annual Inflation 2026", "Eurozone 2026 Annual Inflation",
                  "Canada Annual Inflation 2026", "Argentina Monthly Inflation - July 2026"):
        assert classify_cpi_event(title) is None, title


def test_classify_rejects_schedule_and_undated_markets():
    assert classify_cpi_event("BLS delays another CPI release before 2027?") is None
    assert classify_cpi_event("How high will inflation get in 2026?") is None
    assert classify_cpi_event("Core CPI MoM") is None            # no reference month
    assert classify_cpi_event("Core CPI - July 2026") is None    # basis not stated
    assert classify_cpi_event("Will the Fed cut in September 2026?") is None


def test_ref_month_ignores_the_release_date_in_the_description():
    """The description names two months; only the reference one may win.

    A naive calendar-order scan gets July/August right by luck and would return
    January for a December print. Pin the December case explicitly.
    """
    # "published January 2027" is a bare month-year with no reference cue in front of
    # it. A scan that accepted any month followed by a year would return 2027-1 here,
    # because January is reached first in calendar order.
    dec = ("percentage change in the CPI over the 12-month period ending December 2026, "
           "published January 2027")
    assert _ref_month_from("December Inflation US - Annual", dec) == "2026-12"

    jul = ("the one-month percent change in the CPI in July 2026 ... "
           "scheduled to be released on August 12, 2026")
    assert _ref_month_from("July Inflation US - Monthly", jul) == "2026-7"


def test_ref_month_requires_a_year_and_refuses_ambiguity():
    assert _ref_month_from("July Inflation US", "") is None
    conflicting = "for July 2026 and also for August 2026"
    assert _ref_month_from("Inflation", conflicting) is None


# ---------------------------------------------------------------------------
# Fixtures for the wired-together paths
# ---------------------------------------------------------------------------

def _market(label, yes, description="", **over):
    m = {
        "groupItemTitle": label,
        "outcomePrices": json.dumps([str(yes), str(round(1 - yes, 4))]),
        "question": f"Will Core CPI MoM be {label} in July?",
        "conditionId": "0x" + label.encode("utf-8").hex()[:8],
        "volume": "12345",
        "description": description,
        "active": True,
        "closed": False,
    }
    m.update(over)
    return m


_LADDER = [("≤0.0%", 0.044), ("0.1%", 0.264), ("0.2%", 0.547),
           ("0.3%", 0.165), ("0.4%", 0.014), ("0.5%", 0.001), ("0.6%+", 0.001)]


def _event(title="Core CPI MoM - July 2026", slug="core-cpi-mom-july-2026",
           ladder=_LADDER, description=""):
    return {"slug": slug, "title": title,
            "markets": [_market(l, p, description) for l, p in ladder]}


def _chart(ref, series_name, values, actual=None):
    ds = [{"seriesname": series_name, "data": [{"value": str(v)} for v in values]}]
    ds.append({"seriesname": f"Actual {series_name}",
               "data": [{"value": "" if actual is None else str(actual)}]})
    return {"chart": {"subcaption": ref, "_comment": "2026-08-06 00:00"},
            "categories": [{"category": [{"label": f"d{i}"} for i in range(len(values))]}],
            "dataset": ds}


def _fetcher(events=None, charts=None):
    """Dispatch a fake fetch on URL, so no test touches the network."""
    def fetch(url, params):
        if "public-search" in url:
            return {"events": events if events is not None else []}
        if "nowcast_month" in url or "nowcast_year" in url:
            return charts if charts is not None else []
        raise AssertionError(f"unexpected URL {url}")
    return fetch


# ---------------------------------------------------------------------------
# Nowcast ingestion
# ---------------------------------------------------------------------------

def test_fetch_nowcasts_takes_the_latest_value_per_vintage():
    charts = [_chart("2026-7", "Core CPI Inflation", [0.18, 0.20, 0.211432])]
    got = fetch_nowcasts("core_cpi", "mom", fetch=_fetcher(charts=charts))
    assert got["2026-7"].value_pp == pytest.approx(0.211432)
    assert got["2026-7"].printed is False


def test_fetch_nowcasts_fails_closed_on_a_dead_source():
    def boom(url, params):
        raise RuntimeError("Cleveland Fed down")

    assert fetch_nowcasts("cpi", "mom", fetch=boom) == {}
    # an unknown series/basis pair must not silently pick a file
    assert fetch_nowcasts("pce", "mom", fetch=_fetcher(charts=[])) == {}
    assert fetch_nowcasts("cpi", "weekly", fetch=_fetcher(charts=[])) == {}


def test_nowcast_for_refuses_a_vintage_that_already_printed():
    """Once the actual is out the nowcast is not a forecast of anything."""
    charts = [_chart("2026-6", "CPI Inflation", [0.05, -0.061], actual=-0.4225),
              _chart("2026-7", "CPI Inflation", [0.09, 0.0865])]
    got = fetch_nowcasts("cpi", "mom", fetch=_fetcher(charts=charts))
    assert nowcast_for(got, "2026-6") is None          # printed
    assert nowcast_for(got, "2026-7") is not None      # still open
    assert nowcast_for(got, "2026-9") is None          # no such vintage


# ---------------------------------------------------------------------------
# Bracket construction fail-closed paths
# ---------------------------------------------------------------------------

def test_build_brackets_reads_a_clean_ladder():
    got = build_brackets(_event())
    assert len(got) == 7
    assert got[0].market_prob == pytest.approx(0.044)
    assert partition_is_sound(got)


def test_build_brackets_rejects_an_unpriceable_leg():
    """An unpriceable leg is a rejection, not an assumed zero."""
    ev = _event()
    ev["markets"][2]["outcomePrices"] = None
    assert build_brackets(ev) == []
    ev2 = _event()
    ev2["markets"][2]["outcomePrices"] = "not json"
    assert build_brackets(ev2) == []


def test_build_brackets_rejects_an_unreadable_label():
    ev = _event()
    ev["markets"][3]["groupItemTitle"] = "Somewhere in the middle"
    assert build_brackets(ev) == []


def test_build_brackets_skips_closed_legs():
    ev = _event()
    ev["markets"][6]["closed"] = True
    got = build_brackets(ev)
    assert len(got) == 6
    # dropping the top tail breaks the partition, which the matcher must then reject
    assert not partition_is_sound(got)


def test_match_rejects_an_event_whose_ladder_does_not_tile():
    ev = _event(ladder=[("≤0.0%", 0.1), ("0.2%", 0.4), ("0.3%+", 0.5)])   # 0.1% missing
    assert match_cpi_events([ev]) == []


def test_match_accepts_a_sound_event():
    got = match_cpi_events([_event()])
    assert len(got) == 1
    assert (got[0].series, got[0].basis, got[0].ref_month) == ("core_cpi", "mom", "2026-7")


# ---------------------------------------------------------------------------
# Sigma policy and comparison
# ---------------------------------------------------------------------------

def test_effective_sigma_never_tighter_than_the_measured_track_record():
    """Config may widen the uncertainty but never narrow it below what history supports.

    Too-narrow sigma spikes centre brackets and invents edge; too-wide only flattens
    toward 50/50. Only one of those can lose money.
    """
    tight = replace(CFG, macro_cpi_nowcast_sigma=0.02)
    assert effective_sigma(tight) == pytest.approx(_MEASURED_NOWCAST_RMSE_PP)
    wide = replace(CFG, macro_cpi_nowcast_sigma=0.40)
    assert effective_sigma(wide) == pytest.approx(0.40)


def test_compare_flags_large_gaps_as_suspect():
    match = match_cpi_events([_event()])[0]
    nc = fetch_nowcasts("core_cpi", "mom",
                        fetch=_fetcher(charts=[_chart("2026-7", "Core CPI Inflation", [0.211432])]))
    cmp_ = compare_cpi(match, nc["2026-7"], replace(CFG, macro_max_gap_c=25.0))
    by_label = {r.bracket.label: r for r in cmp_.rows}
    # market has 0.547 on the 0.2% bin against a much flatter model -> >25c gap
    assert by_label["0.2%"].suspect is True
    assert by_label["0.3%"].suspect is False
    # a tiny threshold makes almost everything suspect - the knob really is the gate
    loose = compare_cpi(match, nc["2026-7"], replace(CFG, macro_max_gap_c=1.0))
    assert sum(r.suspect for r in loose.rows) > sum(r.suspect for r in cmp_.rows)


def test_compare_names_a_variance_disagreement_instead_of_calling_it_edge():
    """One disagreement about spread must not print as N independent edges."""
    match = match_cpi_events([_event()])[0]
    nc = fetch_nowcasts("core_cpi", "mom",
                        fetch=_fetcher(charts=[_chart("2026-7", "Core CPI Inflation", [0.211432])]))
    cmp_ = compare_cpi(match, nc["2026-7"], CFG)
    assert "VARIANCE DISAGREEMENT" in cmp_.model_sigma_note


def test_compare_model_probabilities_still_sum_to_one():
    match = match_cpi_events([_event()])[0]
    nc = fetch_nowcasts("core_cpi", "mom",
                        fetch=_fetcher(charts=[_chart("2026-7", "Core CPI Inflation", [0.211432])]))
    cmp_ = compare_cpi(match, nc["2026-7"], CFG)
    assert sum(r.model_prob for r in cmp_.rows) == pytest.approx(1.0, abs=1e-9)


# ---------------------------------------------------------------------------
# End to end, still no network
# ---------------------------------------------------------------------------

def test_run_cpi_end_to_end():
    fetch = _fetcher(events=[_event()],
                     charts=[_chart("2026-7", "Core CPI Inflation", [0.20, 0.211432])])
    out = run_cpi(CFG, fetch=fetch)
    assert len(out) == 1
    assert out[0].nowcast.value_pp == pytest.approx(0.211432)
    assert len(out[0].rows) == 7


def test_run_cpi_yields_nothing_when_the_nowcast_is_missing():
    """No reference price means no opinion - not a default one."""
    fetch = _fetcher(events=[_event()], charts=[])
    assert run_cpi(CFG, fetch=fetch) == []


def test_run_cpi_yields_nothing_when_the_vintage_already_printed():
    fetch = _fetcher(events=[_event()],
                     charts=[_chart("2026-7", "Core CPI Inflation", [0.211432], actual=0.3)])
    assert run_cpi(CFG, fetch=fetch) == []


def test_run_cpi_survives_a_dead_search():
    def fetch(url, params):
        if "public-search" in url:
            raise RuntimeError("gamma down")
        return []

    assert run_cpi(CFG, fetch=fetch) == []


def test_run_cpi_ignores_foreign_markets_mixed_into_the_search():
    fetch = _fetcher(
        events=[_event(title="Japan Core CPI YoY - July 2026", slug="japan-core-cpi"),
                _event()],
        charts=[_chart("2026-7", "Core CPI Inflation", [0.211432])])
    out = run_cpi(CFG, fetch=fetch)
    assert len(out) == 1
    assert "Japan" not in out[0].match.title
