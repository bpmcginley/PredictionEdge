from dataclasses import replace
from datetime import datetime, timezone

from predictionedge.config import Config
from predictionedge.crypto import (
    METHOD_DERIBIT,
    METHOD_LOGNORMAL,
    annualized_vol,
    asset_for,
    event_time,
    find_crypto_edges,
    model_prob_yes,
    prob_above,
)
from predictionedge.kalshi import KalshiMarket


def test_asset_for():
    assert asset_for("KXETHY-27JAN0100-T4999.99") == "ETH"
    assert asset_for("KXBTCMAXW-...") == "BTC"
    assert asset_for("KXNBAGAME-...") is None


def test_prob_above_monotone_and_extremes():
    assert prob_above(100000, 5000, 0.6, 0.5) > 0.99   # spot >> strike
    assert prob_above(100, 5000, 0.6, 0.5) < 0.01       # spot << strike
    # at-the-money is just under 0.5 (lognormal median < mean)
    atm = prob_above(5000, 5000, 0.6, 0.5)
    assert 0.3 < atm < 0.5


def test_model_prob_yes_by_strike_type():
    g = KalshiMarket("t", "", 0.5, 0.5, 0.5, 0.5, strike_type="greater", floor_strike=5000)
    le = KalshiMarket("t", "", 0.5, 0.5, 0.5, 0.5, strike_type="less", cap_strike=5000)
    assert abs(model_prob_yes(g, 4000, 0.6, 0.5) + model_prob_yes(le, 4000, 0.6, 0.5) - 1.0) < 1e-9
    bt = KalshiMarket("t", "", 0.5, 0.5, 0.5, 0.5, strike_type="between",
                      floor_strike=4000, cap_strike=5000)
    assert 0.0 <= model_prob_yes(bt, 4500, 0.6, 0.5) <= 1.0


def test_annualized_vol():
    assert annualized_vol([100, 100, 100, 100, 100, 100]) == 0.0  # flat -> no vol
    assert annualized_vol([100, 101]) is None                      # too few points
    v = annualized_vol([100, 102, 99, 103, 101, 104, 100])
    assert v and v > 0


def test_event_time_prefers_close_time_over_expiration_time():
    """Kalshi's expiration_time is the settlement deadline, a WEEK after the event.

    Reading it made hours-out crypto markets look like week-out options, which is a
    large, silent error in any vol-based probability. This pins the fix.
    """
    m = KalshiMarket("t", "", 0.5, 0.5, 0.5, 0.5,
                     expiration_time="2026-08-13T23:00:00Z",
                     close_time="2026-08-06T23:00:00Z")
    assert event_time(m) == datetime(2026, 8, 6, 23, tzinfo=timezone.utc)
    # Falls back only when close_time is absent.
    old = KalshiMarket("t", "", 0.5, 0.5, 0.5, 0.5,
                       expiration_time="2026-08-13T23:00:00Z")
    assert event_time(old) == datetime(2026, 8, 13, 23, tzinfo=timezone.utc)


class _FakeKalshi:
    def __init__(self, markets):
        self._m = markets

    def list_markets(self, *, series_ticker=None, status="open", **kw):
        return self._m


class _FakeData:
    def __init__(self, spot, vol):
        self._s, self._v = spot, vol

    def spot(self, asset):
        return self._s

    def vol(self, asset):
        return self._v


class _FakePricer:
    """Stands in for DeribitPricer so no test touches the network."""

    def __init__(self, prob):
        self._p = prob

    def spot(self, asset):
        return 60000.0

    def prob_above(self, asset, strike, expiry, now_ts=None):
        return self._p


_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
# Single series so the fake Kalshi client cannot return the same market twice, and
# Deribit off so these exercise the LOGNORMAL fallback path they were written for.
_CFG = replace(Config(), crypto_series=("KXBTCD",), deribit_enabled=False)


def test_find_crypto_edges_short_term_atm():
    # ATM, ~5 days out: model ~0.49; Kalshi prices YES at 0.30 -> buy YES.
    m = KalshiMarket("KXBTCD-26JAN06-T60000", "BTC price", 0.28, 0.30, 0.70, 0.72,
                     strike_type="greater", floor_strike=60000,
                     close_time="2026-01-06T00:00:00Z")
    edges = find_crypto_edges(_CFG, _FakeKalshi([m]), _FakeData(60000, 0.6), now=_NOW)
    assert len(edges) == 1
    assert edges[0].opp.side == "yes"
    assert 0.45 < edges[0].model_prob < 0.5
    assert edges[0].method == METHOD_LOGNORMAL


def test_find_crypto_edges_skips_long_dated():
    # Same market but 6 months out -> filtered by crypto_max_days.
    m = KalshiMarket("KXBTCD-26JUL01-T60000", "BTC price", 0.28, 0.30, 0.70, 0.72,
                     strike_type="greater", floor_strike=60000,
                     close_time="2026-07-01T00:00:00Z")
    assert find_crypto_edges(_CFG, _FakeKalshi([m]), _FakeData(60000, 0.6), now=_NOW) == []


def test_find_crypto_edges_disabled():
    m = KalshiMarket("KXBTCD-x", "", 0.28, 0.30, 0.70, 0.72, strike_type="greater",
                     floor_strike=60000, close_time="2026-01-06T00:00:00Z")
    cfg = replace(_CFG, crypto_enabled=False)
    assert find_crypto_edges(cfg, _FakeKalshi([m]), _FakeData(60000, 0.6), now=_NOW) == []


# ---------------------------------------------------------------- method selection


_BTC_MARKET = KalshiMarket("KXBTCD-26JAN06-T60000", "BTC price", 0.28, 0.30, 0.70, 0.72,
                           strike_type="greater", floor_strike=60000,
                           close_time="2026-01-06T00:00:00Z")


def test_btc_prices_off_deribit_and_is_labelled():
    cfg = replace(Config(), crypto_series=("KXBTCD",))
    edges = find_crypto_edges(cfg, _FakeKalshi([_BTC_MARKET]), _FakeData(60000, 0.6),
                              now=_NOW, pricer=_FakePricer(0.62))
    assert len(edges) == 1
    assert edges[0].model_prob == 0.62          # Deribit's number, not the lognormal's
    assert edges[0].method == METHOD_DERIBIT
    assert edges[0].label == "BTC (deribit-rnd)"


def test_btc_fails_closed_rather_than_falling_back_to_the_lognormal():
    """A Deribit miss on BTC must produce nothing.

    Dropping back to the realized-vol model would silently mix two different
    distributions under one number - the failure mode this whole workstream exists to
    remove. Note the lognormal WOULD have found an edge here, so the empty result is
    the fail-closed and not an absent opportunity.
    """
    cfg = replace(Config(), crypto_series=("KXBTCD",))
    assert find_crypto_edges(cfg, _FakeKalshi([_BTC_MARKET]), _FakeData(60000, 0.6),
                             now=_NOW, pricer=_FakePricer(None)) == []
    lognormal_only = replace(cfg, deribit_enabled=False)
    assert len(find_crypto_edges(lognormal_only, _FakeKalshi([_BTC_MARKET]),
                                 _FakeData(60000, 0.6), now=_NOW)) == 1


def test_non_deribit_asset_stays_on_the_lognormal_and_says_so():
    """SOL has no meaningful Deribit chain, so it keeps the old model - labelled."""
    m = KalshiMarket("KXSOLD-26JAN06-T100", "SOL price", 0.28, 0.30, 0.70, 0.72,
                     strike_type="greater", floor_strike=100,
                     close_time="2026-01-06T00:00:00Z")
    cfg = replace(Config(), crypto_series=("KXSOLD",))
    edges = find_crypto_edges(cfg, _FakeKalshi([m]), _FakeData(100, 0.6), now=_NOW,
                              pricer=_FakePricer(0.99))
    assert len(edges) == 1
    assert edges[0].method == METHOD_LOGNORMAL
    assert edges[0].label == "SOL (lognormal-realized-vol)"


# ------------------------------------------------------------- rejection reporting

def test_report_explains_an_empty_result():
    """An empty edge list must be readable: no number, or a number with no edge."""
    long_dated = KalshiMarket("KXBTCD-26JUL01-T60000", "BTC price", 0.28, 0.30, 0.70, 0.72,
                              strike_type="greater", floor_strike=60000,
                              close_time="2026-07-01T00:00:00Z")
    rep = {}
    assert find_crypto_edges(_CFG, _FakeKalshi([long_dated]), _FakeData(60000, 0.6),
                             now=_NOW, report=rep) == []
    assert rep == {"resolves beyond 10d": 1}


def test_report_separates_priced_no_edge_from_could_not_price():
    # Priced fine, but the market agrees with the model -> no edge, not a failure.
    agreed = KalshiMarket("KXBTCD-26JAN06-T60000", "BTC price", 0.48, 0.50, 0.50, 0.52,
                          strike_type="greater", floor_strike=60000,
                          close_time="2026-01-06T00:00:00Z")
    rep = {}
    find_crypto_edges(_CFG, _FakeKalshi([agreed]), _FakeData(60000, 0.6),
                      now=_NOW, report=rep)
    assert rep.get("priced, no edge over the market") == 1


def test_report_is_optional_and_absent_by_default():
    m = KalshiMarket("KXBTCD-26JAN06-T60000", "BTC price", 0.28, 0.30, 0.70, 0.72,
                     strike_type="greater", floor_strike=60000,
                     close_time="2026-01-06T00:00:00Z")
    # No report kwarg: must not raise, must still return the edge.
    assert len(find_crypto_edges(_CFG, _FakeKalshi([m]), _FakeData(60000, 0.6),
                                 now=_NOW)) == 1
