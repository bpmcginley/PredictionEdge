"""Deribit digital-extraction tests.

The point of these is to verify the MATHS, not the plumbing. The fixtures are option
chains generated from a Black-76 formula at a known volatility, so the correct digital
is available in closed form and can be asserted exactly - if the extraction were merely
"plausible" these would fail.
"""

import math
from dataclasses import replace
from statistics import NormalDist

import pytest

from predictionedge.config import Config
from predictionedge.deribit import (
    Chain,
    DeribitPricer,
    Smile,
    black76,
    build_smiles,
    implied_vol,
    interpolate_variance,
    parse_chain,
)

_ND = NormalDist()
_YEAR = 365.25 * 24 * 3600
_CFG = Config()

# Nodes uniform in LOG-MONEYNESS. On a uniform grid the central difference of the
# interpolated variance curve is exact for the quadratic that a linear-in-k vol smile
# produces, so the skewed-chain assertion below can be exact rather than approximate.
_KS = [round(-0.4 + 0.1 * i, 4) for i in range(9)]
_FWD = 100_000.0
_TAU = 0.1
_EXPIRY = _TAU * _YEAR       # with now_ts = 0, tau comes out at exactly 0.1


def _flat(_k: float) -> float:
    return 0.6


def _skewed(k: float) -> float:
    """Vol falling in strike - the sign of the real BTC smile."""
    return 0.6 - 0.4 * k


def _payload(vol_of_k=_flat, *, forward=_FWD, expiry=_EXPIRY, tau=_TAU,
             ks=None, spread=0.10, unquoted=(), code="1JAN27", asset="BTC"):
    """A synthetic Deribit chain: every strike priced by Black-76 at vol_of_k.

    `spread` is the fractional bid/ask width, applied symmetrically so the mid is the
    exact theoretical price. `unquoted` strikes are listed but get no quote - that is
    what a hole in the ladder looks like.
    """
    ks = _KS if ks is None else ks
    summaries, instruments = [], []
    for k in ks:
        strike = round(forward * math.exp(k), 4)
        for tag, is_call in (("C", True), ("P", False)):
            name = f"{asset}-{code}-{strike:g}-{tag}"
            instruments.append({"instrument_name": name, "strike": strike,
                                "option_type": "call" if is_call else "put",
                                "expiration_timestamp": expiry * 1000.0})
            if strike in unquoted:
                continue
            coin = black76(forward, strike, vol_of_k(k), tau, is_call) / forward
            summaries.append({
                "instrument_name": name, "underlying_price": forward,
                "bid_price": coin * (1 - spread / 2), "ask_price": coin * (1 + spread / 2),
            })
    return summaries, instruments


def _chain(**kw) -> Chain:
    s, i = _payload(**kw)
    return parse_chain("BTC", s, i, index=_FWD)


def _smile(**kw) -> Smile:
    smiles = build_smiles(_chain(**kw), _CFG, now_ts=0.0)
    assert len(smiles) == 1
    return smiles[0]


def _lognormal_digital(forward, strike, sigma, tau) -> float:
    """N(d2) worked out independently of anything in deribit.py."""
    sqt = sigma * math.sqrt(tau)
    d1 = (math.log(forward / strike) + 0.5 * sqt * sqt) / sqt
    return _ND.cdf(d1 - sqt)


# ------------------------------------------------------------------ black-76 / IV


def test_black76_matches_hand_worked_value():
    # F=100k, K=110k, sigma=60%, tau=0.1: d1=-0.407459, d2=-0.597196, so
    # C = 100000*N(d1) - 110000*N(d2) = 34182.3 - 30269.5 = 3912.82 (undiscounted).
    assert black76(100_000, 110_000, 0.6, 0.1) == pytest.approx(3912.8234, abs=1e-3)


def test_put_call_parity_holds_undiscounted():
    c = black76(100_000, 90_000, 0.55, 0.3, True)
    p = black76(100_000, 90_000, 0.55, 0.3, False)
    assert c - p == pytest.approx(100_000 - 90_000, abs=1e-6)


def test_implied_vol_round_trips():
    px = black76(100_000, 105_000, 0.73, 0.25)
    assert implied_vol(px, 100_000, 105_000, 0.25) == pytest.approx(0.73, abs=1e-6)


def test_implied_vol_rejects_prices_outside_no_arb_bounds():
    assert implied_vol(50.0, 100_000, 90_000, 0.1) is None      # below intrinsic
    assert implied_vol(120_000.0, 100_000, 90_000, 0.1) is None  # above the forward


# ------------------------------------------------------- the digital, hand-computed


@pytest.mark.parametrize("strike,expected", [
    (90_000.0, 0.6773963211),
    (100_000.0, 0.4622097061),
    (110_000.0, 0.2751879030),
])
def test_digital_on_flat_vol_chain_equals_n_d2(strike, expected):
    """A flat-vol chain has no skew, so -dC/dK must be exactly N(d2)."""
    s = _smile()
    got = s.digital(strike)
    assert got == pytest.approx(_lognormal_digital(_FWD, strike, 0.6, _TAU), abs=1e-9)
    assert got == pytest.approx(expected, abs=1e-9)   # pinned literal


def test_digital_on_skewed_chain_picks_up_the_skew_term():
    """With a falling smile the digital is N(d2) - vega*dsigma/dK, above plain N(d2).

    This is the whole reason the option chain beats a single-vol model: the correction
    term is worth several cents and a flat-vol model cannot see it.
    """
    s = _smile(vol_of_k=_skewed)
    for k in (-0.2, 0.0, 0.2):
        strike = round(_FWD * math.exp(k), 4)
        sigma = _skewed(k)
        sqt = sigma * math.sqrt(_TAU)
        d1 = (math.log(_FWD / strike) + 0.5 * sqt * sqt) / sqt
        vega = _FWD * _ND.pdf(d1) * math.sqrt(_TAU)
        expected = _ND.cdf(d1 - sqt) - vega * (-0.4 / strike)
        assert s.digital(strike) == pytest.approx(expected, abs=1e-6)
        assert s.digital(strike) > _lognormal_digital(_FWD, strike, sigma, _TAU)


def test_digital_is_monotone_decreasing_in_strike():
    s = _smile(vol_of_k=_skewed)
    probs = [s.digital(round(_FWD * math.exp(k), 4)) for k in (-0.2, -0.1, 0.0, 0.1, 0.2)]
    assert all(a > b for a, b in zip(probs, probs[1:]))


def test_digital_matches_a_numeric_slope_of_the_call_curve():
    """Cross-check the analytic form against a literal -dC/dK on the same smile."""
    s = _smile(vol_of_k=_skewed)
    strike, h = 100_000.0, 1.0
    def call(k_strike):
        sig = math.sqrt(s._w(math.log(k_strike / _FWD)) / _TAU)
        return black76(_FWD, k_strike, sig, _TAU)
    numeric = -(call(strike + h) - call(strike - h)) / (2 * h)
    assert s.digital(strike) == pytest.approx(numeric, abs=1e-4)


# --------------------------------------------------------------------- fail closed


def test_thin_ladder_fails_closed():
    """Fewer usable strikes than the gate allows must produce no smile at all."""
    thin = [-0.2, -0.1, 0.0, 0.1]        # 4 strikes, gate wants 8
    assert build_smiles(_chain(ks=thin), _CFG, now_ts=0.0) == []


def test_gappy_ladder_fails_closed_rather_than_interpolating():
    """A listed-but-unquoted strike inside the range is a hole, not something to span."""
    hole = round(_FWD * math.exp(0.0), 4)
    assert build_smiles(_chain(unquoted=(hole,)), _CFG, now_ts=0.0) == []
    # ...and the same chain without the hole is fine, so it is the hole that did it.
    assert len(build_smiles(_chain(), _CFG, now_ts=0.0)) == 1


def test_missing_wing_strikes_are_not_a_hole():
    """Unquoted strikes OUTSIDE the usable range just shrink the range."""
    wings = [-0.6, -0.5] + _KS + [0.5, 0.6]
    unquoted = tuple(round(_FWD * math.exp(k), 4) for k in (-0.6, -0.5, 0.5, 0.6))
    smiles = build_smiles(_chain(unquoted=unquoted, ks=wings), _CFG, now_ts=0.0)
    assert len(smiles) == 1
    assert len(smiles[0].k_nodes) == len(_KS)


def test_wide_quotes_are_discarded():
    assert build_smiles(_chain(spread=0.60), _CFG, now_ts=0.0) == []


def test_crossed_and_one_sided_quotes_are_dropped():
    summaries, instruments = _payload()
    summaries[0]["bid_price"], summaries[0]["ask_price"] = 0.5, 0.4   # crossed
    summaries[1]["bid_price"] = None                                 # one-sided
    chain = parse_chain("BTC", summaries, instruments)
    live = {s["instrument_name"] for s in summaries[2:]}
    assert {q.instrument for q in chain.quotes} == live


def test_strike_outside_the_ladder_returns_none():
    s = _smile()
    assert s.digital(10_000.0) is None       # far below the quoted range
    assert s.digital(10_000_000.0) is None   # far above it


def test_strike_at_the_very_edge_returns_none():
    """No room for a slope at the boundary node - that is an extrapolation."""
    s = _smile()
    assert s.digital(round(_FWD * math.exp(_KS[-1]), 4)) is None


# ------------------------------------------------------- variance-time interpolation


def _two_expiry_pricer(vol_of_k=_flat, tau_a=0.1, tau_b=0.4):
    sa, ia = _payload(vol_of_k, expiry=tau_a * _YEAR, tau=tau_a, code="1JAN27")
    sb, ib = _payload(vol_of_k, expiry=tau_b * _YEAR, tau=tau_b, code="1APR27")
    return parse_chain("BTC", sa + sb, ia + ib)


def test_interpolation_is_in_variance_not_in_time_or_price():
    """With the same vol at both expiries the interpolated smile must reprice exactly.

    Total variance is linear in time when vol is flat, so a correct implementation
    lands on the closed-form answer. Interpolating the *probability* linearly - the
    common shortcut - lands somewhere else, and the second assertion pins that the two
    genuinely differ, so this test cannot pass by accident.
    """
    smiles = build_smiles(_two_expiry_pricer(), _CFG, now_ts=0.0)
    assert len(smiles) == 2
    a, b = smiles
    mid_tau = 0.25
    mid = interpolate_variance(a, b, mid_tau * _YEAR, 0.0, _CFG.deribit_min_strikes)
    assert mid is not None
    strike = 110_000.0
    exact = _lognormal_digital(_FWD, strike, 0.6, mid_tau)
    assert mid.digital(strike) == pytest.approx(exact, abs=1e-6)

    naive = 0.5 * (a.digital(strike) + b.digital(strike))
    assert abs(naive - exact) > 0.005      # the shortcut is materially wrong


def test_interpolated_forward_is_log_linear():
    sa, ia = _payload(expiry=0.1 * _YEAR, tau=0.1, forward=100_000.0, code="1JAN27")
    sb, ib = _payload(expiry=0.4 * _YEAR, tau=0.4, forward=110_000.0, code="1APR27")
    a, b = build_smiles(parse_chain("BTC", sa + sb, ia + ib), _CFG, now_ts=0.0)
    mid = interpolate_variance(a, b, 0.25 * _YEAR, 0.0, _CFG.deribit_min_strikes)
    assert mid.forward == pytest.approx(math.sqrt(100_000.0 * 110_000.0), rel=1e-9)


def test_interpolation_refuses_a_date_outside_the_bracket():
    smiles = build_smiles(_two_expiry_pricer(), _CFG, now_ts=0.0)
    a, b = smiles
    assert interpolate_variance(a, b, 0.5 * _YEAR, 0.0, _CFG.deribit_min_strikes) is None


def test_interpolation_refuses_when_the_ladders_barely_overlap():
    sa, ia = _payload(expiry=0.1 * _YEAR, tau=0.1, code="1JAN27")
    sb, ib = _payload(expiry=0.4 * _YEAR, tau=0.4, code="1APR27",
                      ks=[0.35 + 0.1 * i for i in range(9)])   # disjoint wing
    a, b = build_smiles(parse_chain("BTC", sa + sb, ia + ib), _CFG, now_ts=0.0)
    assert interpolate_variance(a, b, 0.25 * _YEAR, 0.0, _CFG.deribit_min_strikes) is None


# -------------------------------------------------------------------------- pricer


class _CannedFetch:
    def __init__(self, chain_payload):
        self._s, self._i = chain_payload
        self.calls = 0

    def __call__(self, url, params):
        self.calls += 1
        if "get_book_summary" in url:
            return {"result": self._s}
        if "get_instruments" in url:
            return {"result": self._i}
        return {"result": {"index_price": _FWD}}


def _pricer(payload=None):
    return DeribitPricer(_CFG, fetch=_CannedFetch(payload or _payload()))


def test_pricer_prices_an_exact_expiry():
    from datetime import datetime, timezone
    p = _pricer()
    exp = datetime.fromtimestamp(_EXPIRY, timezone.utc)
    got = p.prob_above("BTC", 110_000.0, exp, now_ts=0.0)
    assert got == pytest.approx(_lognormal_digital(_FWD, 110_000.0, 0.6, _TAU), abs=1e-9)


def test_pricer_refuses_to_extrapolate_past_the_last_expiry():
    from datetime import datetime, timezone
    p = _pricer()
    beyond = datetime.fromtimestamp(_EXPIRY * 3, timezone.utc)
    assert p.prob_above("BTC", 110_000.0, beyond, now_ts=0.0) is None


def test_pricer_refuses_assets_off_the_allow_list():
    from datetime import datetime, timezone
    p = _pricer()
    exp = datetime.fromtimestamp(_EXPIRY, timezone.utc)
    assert not p.supports("SOL")
    assert p.prob_above("SOL", 100.0, exp, now_ts=0.0) is None


def test_pricer_returns_none_when_the_feed_is_down():
    from datetime import datetime, timezone

    def boom(url, params):
        raise RuntimeError("deribit down")

    p = DeribitPricer(_CFG, fetch=boom)
    exp = datetime.fromtimestamp(_EXPIRY, timezone.utc)
    assert p.prob_above("BTC", 110_000.0, exp, now_ts=0.0) is None


def test_pricer_caches_the_chain():
    fetch = _CannedFetch(_payload())
    p = DeribitPricer(_CFG, fetch=fetch)
    p.chain("BTC")
    p.chain("BTC")
    assert fetch.calls == 3      # summaries + instruments + index, fetched once


def test_disabled_config_prices_nothing():
    from datetime import datetime, timezone
    cfg = replace(_CFG, deribit_enabled=False)
    p = DeribitPricer(cfg, fetch=_CannedFetch(_payload()))
    exp = datetime.fromtimestamp(_EXPIRY, timezone.utc)
    assert p.prob_above("BTC", 110_000.0, exp, now_ts=0.0) is None


# ------------------------------------------------------- coverage (why a miss missed)

def test_coverage_names_the_before_front_case():
    """The structural mismatch with Kalshi's crypto complex, pinned.

    Measured 2026-08-07: every open KXBTCD/KXETHD market expired in 0.03 or 0.20 days
    while Deribit's front expiry sat 0.66 days out, so all 12 in-window markets fell
    before the front. No window setting fixes that, and an empty scan must say so
    rather than read as "priced everything, found nothing".
    """
    from datetime import datetime, timezone
    p = _pricer()
    early = datetime.fromtimestamp(_EXPIRY / 2, timezone.utc)
    assert p.prob_above("BTC", 110_000.0, early, now_ts=0.0) is None
    assert p.coverage("BTC", early, now_ts=0.0) == "expires before Deribit's front expiry"


def test_coverage_ok_when_the_expiry_is_priceable():
    from datetime import datetime, timezone
    p = _pricer()
    exp = datetime.fromtimestamp(_EXPIRY, timezone.utc)
    assert p.coverage("BTC", exp, now_ts=0.0) == "ok"


def test_coverage_distinguishes_past_the_last_expiry():
    from datetime import datetime, timezone
    p = _pricer()
    beyond = datetime.fromtimestamp(_EXPIRY * 3, timezone.utc)
    assert p.coverage("BTC", beyond, now_ts=0.0) == "expires after Deribit's last expiry"


def test_coverage_names_an_unsupported_asset():
    from datetime import datetime, timezone
    p = _pricer()
    exp = datetime.fromtimestamp(_EXPIRY, timezone.utc)
    assert p.coverage("SOL", exp, now_ts=0.0) == "asset not covered by Deribit"
