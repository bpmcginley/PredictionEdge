"""Kalshi settlement metadata, in the shape the paper trial already understands."""

import pytest

from predictionedge import gas, spxdensity, weather, weatherlog
from predictionedge.kalshi import _event_of, event_day, market_meta
from predictionedge.papertrial import _winner


# Every ticker below is a REAL event ticker read off api.elections.kalshi.com on
# 2026-08-16, one per series that a sleeve actually reads. The originals are the point:
# this parser shipped for the life of the SPX sleeve reading the whole tail as a date,
# so `KXINX-26AUG17H1600` scored None, the day filter matched nothing, and the sleeve
# produced zero rows without ever raising. A fixture ticker with the suffix trimmed off
# is what let that survive, so no ticker here may be tidied up.
LIVE_EVENT_TICKERS = [
    # sleeve        event ticker                      settles on
    ("spxdensity",  "KXINX-26AUG17H1600",             "2026-08-17"),
    ("spxdensity",  "KXNASDAQ100-26AUG17H1600",       "2026-08-17"),
    ("weather",     "KXHIGHNY-26AUG12",               "2026-08-12"),
    ("weather",     "KXHIGHCHI-26AUG12",              "2026-08-12"),
    ("weather",     "KXHIGHMIA-26AUG12",              "2026-08-12"),
    ("weather",     "KXHIGHDEN-26AUG12",              "2026-08-12"),
    ("gas",         "KXAAAGASD-26AUG14",              "2026-08-14"),
    # Not read by a sleeve today, but they are the two suffix shapes that would break a
    # tail-parser next: digits with no letter at all, and free-form team codes.
    ("none",        "KXBTCD-26AUG1719",               "2026-08-17"),
    ("none",        "KXNFLGAME-26AUG20LVHOU",         "2026-08-20"),
]


@pytest.mark.parametrize("sleeve,ticker,expected", LIVE_EVENT_TICKERS)
def test_every_sleeve_reads_the_day_off_a_real_suffixed_ticker(sleeve, ticker,
                                                               expected):
    assert event_day(ticker) == expected


def test_the_three_sleeves_share_one_parser_rather_than_copying_it():
    """The duplication WAS the bug: three copies, one silently broken for a year.

    Asserted by identity, not by behaviour, because a fresh copy-paste would pass a
    behavioural check on the day it was made and then drift. `weatherlog` is in here
    because it imports the name through `weather` rather than from `kalshi`.
    """
    assert spxdensity._event_day is event_day
    assert weather._event_day is event_day
    assert gas._event_day is event_day
    assert weatherlog._event_day is event_day


def test_an_unreadable_day_is_None_so_the_caller_skips_the_market():
    assert event_day("KXINX-garbage") is None
    assert event_day("KXINX-2026-08-17") is None
    assert event_day("KXINX-26AUG99H1600") is None   # real shape, impossible day
    assert event_day("KXINX") is None
    assert event_day("") is None


def test_the_month_is_read_case_insensitively_as_strptime_always_did():
    """The copies this replaced used `%b`, which never cared about case.

    A shared parser has to accept everything the copies accepted, or the dedup
    quietly narrows a sleeve instead of fixing one.
    """
    assert event_day("KXHIGHNY-26aug12") == "2026-08-12"
    assert event_day("KXHIGHNY-26Aug12") == "2026-08-12"


def _mkt(ticker, status="active", result="", **over):
    """A market in the shape the LIVE API serves (verified 2026-08-16).

    `volume_fp` / `liquidity_dollars` are decimal strings, and the bare `volume` /
    `liquidity` names this fixture used to carry are gone from the wire entirely -
    which is exactly why reading them scored every market at the 0.0 default.
    """
    m = {"ticker": ticker, "event_ticker": _event_of(ticker), "status": status,
         "result": result, "title": f"high temp {ticker}", "yes_bid": 45,
         "yes_ask": 46, "last_price": 46,
         "volume_fp": "1081.00", "liquidity_dollars": "0.0000",
         "expiration_time": "2026-08-19T14:00:00Z",
         "close_time": "2026-08-13T04:59:00Z"}
    m.update(over)
    return m


def _api(markets_by_event, calls=None):
    def fetch(url, params):
        if calls is not None:
            calls.append(params)
        event = params.get("event_ticker")
        return {"markets": markets_by_event.get(event, [])}
    return fetch


def test_event_ticker_is_the_query_key_and_status_is_never_pinned():
    """The regression that would silently break settlement.

    Kalshi's `tickers=` filter returns an EMPTY array rather than an error, and
    `list_markets` defaults to `status="open"` - which hides exactly the finalized
    markets settlement needs. Either mistake yields "no resolved markets" forever,
    which is indistinguishable from "nothing has resolved yet".
    """
    calls: list[dict] = []
    fetch = _api({"KXHIGHNY-26AUG10": [_mkt("KXHIGHNY-26AUG10-T89")]}, calls)
    market_meta(["KXHIGHNY-26AUG10-T89"], fetch=fetch)
    assert len(calls) == 1
    assert calls[0]["event_ticker"] == "KXHIGHNY-26AUG10"
    assert "status" not in calls[0] and "tickers" not in calls[0]


def test_a_finalized_yes_settles_as_the_yes_leg():
    fetch = _api({"KXHIGHNY-26AUG10": [
        _mkt("KXHIGHNY-26AUG10-T89", status="finalized", result="yes")]})
    meta = market_meta(["KXHIGHNY-26AUG10-T89"], fetch=fetch)["KXHIGHNY-26AUG10-T89"]
    assert meta["closed"] is True
    assert meta["outcomes"] == ["Yes", "No"] and meta["prices"] == [1.0, 0.0]
    # The whole point of the shared shape: the Polymarket settlement test works as-is.
    assert _winner(meta) == "Yes"


def test_a_finalized_no_settles_as_the_no_leg():
    fetch = _api({"KXHIGHNY-26AUG10": [
        _mkt("KXHIGHNY-26AUG10-B91.5", status="finalized", result="no")]})
    meta = market_meta(["KXHIGHNY-26AUG10-B91.5"], fetch=fetch)["KXHIGHNY-26AUG10-B91.5"]
    assert meta["prices"] == [0.0, 1.0] and _winner(meta) == "No"


def test_an_open_market_is_not_settled():
    fetch = _api({"KXHIGHNY-26AUG12": [_mkt("KXHIGHNY-26AUG12-B85.5")]})
    meta = market_meta(["KXHIGHNY-26AUG12-B85.5"], fetch=fetch)["KXHIGHNY-26AUG12-B85.5"]
    assert meta["closed"] is False and meta["prices"] == []
    assert _winner(meta) is None


def test_one_call_covers_every_strike_of_a_day_and_spare_legs_are_ignored():
    """An event query returns the whole ladder; only the held strikes are reported."""
    calls: list[dict] = []
    ladder = [_mkt("KXHIGHNY-26AUG10-T89", status="finalized", result="yes"),
              _mkt("KXHIGHNY-26AUG10-B91.5", status="finalized", result="no"),
              _mkt("KXHIGHNY-26AUG10-T96", status="finalized", result="no")]
    fetch = _api({"KXHIGHNY-26AUG10": ladder}, calls)
    meta = market_meta(["KXHIGHNY-26AUG10-T89", "KXHIGHNY-26AUG10-B91.5"], fetch=fetch)
    assert set(meta) == {"KXHIGHNY-26AUG10-T89", "KXHIGHNY-26AUG10-B91.5"}
    assert len(calls) == 1


def test_a_voided_market_is_flagged_rather_than_left_hanging():
    """A void never resolves, so "still open" would hold it forever."""
    fetch = _api({"KXHIGHNY-26AUG10": [
        _mkt("KXHIGHNY-26AUG10-T89", status="finalized", result="void")]})
    meta = market_meta(["KXHIGHNY-26AUG10-T89"], fetch=fetch)["KXHIGHNY-26AUG10-T89"]
    assert meta["voided"] is True and meta["closed"] is False


def test_an_unreachable_event_is_reported_not_read_as_unresolved():
    def fetch(url, params):
        if params["event_ticker"] == "KXHIGHNY-26AUG10":
            raise RuntimeError("connection reset")
        return {"markets": [_mkt("KXHIGHCHI-26AUG10-T89", status="finalized",
                                 result="yes")]}

    failures: set[str] = set()
    meta = market_meta(["KXHIGHNY-26AUG10-T89", "KXHIGHCHI-26AUG10-T89"],
                       fetch=fetch, failures=failures)
    assert set(meta) == {"KXHIGHCHI-26AUG10-T89"}      # the healthy event still settles
    assert failures == {"KXHIGHNY-26AUG10-T89"}


def test_a_decided_market_with_an_unrecognised_result_is_not_guessed():
    fetch = _api({"KXHIGHNY-26AUG10": [
        _mkt("KXHIGHNY-26AUG10-T89", status="finalized", result="maybe")]})
    meta = market_meta(["KXHIGHNY-26AUG10-T89"], fetch=fetch)["KXHIGHNY-26AUG10-T89"]
    assert meta["closed"] is False and _winner(meta) is None


def test_liquidity_is_carried_but_is_known_to_be_unpopulated():
    """`liquidity_dollars` reads $0.0000 on every live market (0 of ~900 nonzero on
    2026-08-16) while the book holds real resting size. Correcting the key name fixed
    the lookup; it did not make the field populated. Nothing may gate on it."""
    fetch = _api({"KXHIGHNY-26AUG12": [_mkt("KXHIGHNY-26AUG12-B85.5")]})
    meta = market_meta(["KXHIGHNY-26AUG12-B85.5"], fetch=fetch)["KXHIGHNY-26AUG12-B85.5"]
    assert meta["liquidity"] == 0.0 and meta["volume"] == 1081.0


def test_volume_and_liquidity_read_the_live_field_names_at_face_value():
    """The renamed fields, and their SCALE.

    `volume` -> `volume_fp` and `liquidity` -> `liquidity_dollars`; the old names are
    absent from every live market, so reading them returned 0.0 for both, always.

    The suffixes name the encoding, not a divisor - both are decimal strings already
    in natural units. Asserted explicitly because a stray /100 here would filter real
    markets out just as silently as the bug it replaced. Ground truth: the trades feed
    for KXINX-26AUG11H1600-B7737 sums to 115702.77 contracts, exactly its `volume_fp`.
    """
    fetch = _api({"KXINX-26AUG11H1600": [
        _mkt("KXINX-26AUG11H1600-B7737", volume_fp="115702.77",
             liquidity_dollars="4250.5000")]})
    meta = market_meta(["KXINX-26AUG11H1600-B7737"],
                       fetch=fetch)["KXINX-26AUG11H1600-B7737"]
    assert meta["volume"] == 115702.77       # contracts, NOT cents-of-a-contract
    assert meta["liquidity"] == 4250.50      # dollars, NOT cents


def test_the_legacy_field_names_still_read_if_kalshi_ever_serves_them_again():
    """The migration is not symmetric across endpoints, so the old names stay wired
    at the same units they always had rather than being deleted outright."""
    fetch = _api({"KXHIGHNY-26AUG12": [
        {"ticker": "KXHIGHNY-26AUG12-B85.5", "event_ticker": "KXHIGHNY-26AUG12",
         "status": "active", "result": "", "volume": 1081, "liquidity": 20.0}]})
    meta = market_meta(["KXHIGHNY-26AUG12-B85.5"], fetch=fetch)["KXHIGHNY-26AUG12-B85.5"]
    assert meta["volume"] == 1081.0 and meta["liquidity"] == 20.0


def test_a_missing_depth_field_is_loud_rather_than_a_silent_zero(caplog):
    """The whole shape of this bug: an absent field read as 0.0 is indistinguishable
    from a genuinely empty book, so the next rename has to be visible in the log."""
    fetch = _api({"KXHIGHNY-26AUG12": [
        {"ticker": "KXHIGHNY-26AUG12-B85.5", "event_ticker": "KXHIGHNY-26AUG12",
         "status": "active", "result": ""}]})
    with caplog.at_level("WARNING"):
        meta = market_meta(["KXHIGHNY-26AUG12-B85.5"],
                           fetch=fetch)["KXHIGHNY-26AUG12-B85.5"]
    assert meta["volume"] == 0.0 and meta["liquidity"] == 0.0   # shape is preserved
    assert "volume_fp" in caplog.text and "liquidity_dollars" in caplog.text
