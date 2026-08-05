from dataclasses import replace
from datetime import datetime, timezone

from predictionedge.config import Config
from predictionedge.crypto import (
    annualized_vol,
    asset_for,
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


_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
_CFG = replace(Config(), crypto_series=("KXBTCD",))   # single series -> no fake dupes


def test_find_crypto_edges_short_term_atm():
    # ATM, ~5 days out: model ~0.49; Kalshi prices YES at 0.30 -> buy YES.
    m = KalshiMarket("KXBTCD-26JAN06-T60000", "BTC price", 0.28, 0.30, 0.70, 0.72,
                     strike_type="greater", floor_strike=60000,
                     expiration_time="2026-01-06T00:00:00Z")
    edges = find_crypto_edges(_CFG, _FakeKalshi([m]), _FakeData(60000, 0.6), now=_NOW)
    assert len(edges) == 1
    assert edges[0].opp.side == "yes"
    assert 0.45 < edges[0].model_prob < 0.5


def test_find_crypto_edges_skips_long_dated():
    # Same market but 6 months out -> filtered by crypto_max_days.
    m = KalshiMarket("KXBTCD-26JUL01-T60000", "BTC price", 0.28, 0.30, 0.70, 0.72,
                     strike_type="greater", floor_strike=60000,
                     expiration_time="2026-07-01T00:00:00Z")
    assert find_crypto_edges(_CFG, _FakeKalshi([m]), _FakeData(60000, 0.6), now=_NOW) == []


def test_find_crypto_edges_disabled():
    m = KalshiMarket("KXBTCD-x", "", 0.28, 0.30, 0.70, 0.72, strike_type="greater",
                     floor_strike=60000, expiration_time="2026-01-06T00:00:00Z")
    cfg = replace(_CFG, crypto_enabled=False)
    assert find_crypto_edges(cfg, _FakeKalshi([m]), _FakeData(60000, 0.6), now=_NOW) == []
