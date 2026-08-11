"""Kalshi settlement metadata, in the shape the paper trial already understands."""

from predictionedge.kalshi import _event_of, market_meta
from predictionedge.papertrial import _winner


def _mkt(ticker, status="active", result="", **over):
    m = {"ticker": ticker, "event_ticker": _event_of(ticker), "status": status,
         "result": result, "title": f"high temp {ticker}", "yes_bid": 45,
         "yes_ask": 46, "last_price": 46, "volume": 1081, "liquidity": 0,
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
    """Every live weather market reads $0.00 liquidity while the book holds 1,510
    contracts. The field is recorded, but nothing may gate on it."""
    fetch = _api({"KXHIGHNY-26AUG12": [_mkt("KXHIGHNY-26AUG12-B85.5", liquidity=0)]})
    meta = market_meta(["KXHIGHNY-26AUG12-B85.5"], fetch=fetch)["KXHIGHNY-26AUG12-B85.5"]
    assert meta["liquidity"] == 0.0 and meta["volume"] == 1081.0
