"""The calibration overlay, and specifically that it can only VETO, never invent.

The overlay corrects the market's published domain x horizon miscalibration out of a
quoted price (Le 2026, arXiv 2602.19520) and suppresses tickets whose edge does not
survive the correction. The properties worth pinning: b<1 pulls extremes toward 0.5
and leaves 0.5 alone, b=1 domains are untouched, shrink=0 or the config flag off is a
pure pass-through, and a veto is counted where the published diagnostics can see it.
"""

from datetime import datetime, timezone

import pytest

from predictionedge.calibration import (
    Assessment, assess, calibrated_prob, effective_b, horizon_bucket, table_b,
)
from predictionedge.config import Config
from predictionedge.weather import (
    CITIES, _calibration_gate, _hours_to_expiry, find_weather_edges,
)


# --- the transform -----------------------------------------------------------

def test_overconfident_market_is_pulled_toward_half():
    """Weather under 48h has b<1: extreme prices are too extreme, so the corrected
    probability sits between the price and 0.5 - on BOTH sides of the coin."""
    hi = calibrated_prob(0.90, "weather", 24.0)
    lo = calibrated_prob(0.10, "weather", 24.0)
    assert 0.5 < hi < 0.90
    assert 0.10 < lo < 0.5
    # Half the published correction (shrink 0.5) is a nudge, not a rewrite.
    assert hi > 0.80


def test_half_is_a_fixed_point():
    assert calibrated_prob(0.5, "weather", 24.0) == pytest.approx(0.5)


def test_the_transform_is_monotone_in_price():
    """A correction that reorders prices would be claiming to know which of two quotes
    is wrong, which is far more than a population-level slope can support."""
    prices = [0.02, 0.10, 0.30, 0.50, 0.70, 0.90, 0.98]
    cal = [calibrated_prob(p, "weather", 24.0) for p in prices]
    assert cal == sorted(cal)


def test_the_transform_is_symmetric():
    """logit with no intercept: correcting YES and correcting NO must agree."""
    assert calibrated_prob(0.10, "weather", 24.0) == pytest.approx(
        1.0 - calibrated_prob(0.90, "weather", 24.0))


def test_calibrated_domains_are_identity():
    """Sports and finance measured ~1.0: the overlay must be a structural no-op there,
    not a small correction that happens to round away."""
    for domain in ("sports", "finance", "crypto"):
        for p in (0.05, 0.30, 0.90):
            assert calibrated_prob(p, domain, 24.0) == pytest.approx(p)


def test_an_unmeasured_domain_gets_no_correction():
    """Correcting a market the study never measured would invent a miscalibration."""
    assert calibrated_prob(0.90, "made-up-domain", 24.0) == pytest.approx(0.90)
    assert calibrated_prob(0.90, "", 24.0) == pytest.approx(0.90)


def test_shrink_zero_is_the_identity_everywhere():
    for domain in ("weather", "politics", "sports"):
        assert effective_b(domain, 24.0, shrink=0.0) == 1.0
        assert calibrated_prob(0.90, domain, 24.0, shrink=0.0) == pytest.approx(0.90)


def test_shrink_applies_half_the_published_slope():
    # weather <48h publishes 0.69-0.97; the table holds the middle, 0.83.
    assert table_b("weather", 24.0) == 0.83
    assert effective_b("weather", 24.0, shrink=0.5) == pytest.approx(0.915)
    assert effective_b("weather", 24.0, shrink=1.0) == pytest.approx(0.83)


# --- horizon bucketing -------------------------------------------------------

def test_bucket_boundaries_belong_to_the_longer_bucket():
    assert horizon_bucket(0.0) == 0
    assert horizon_bucket(47.999) == 0
    assert horizon_bucket(48.0) == 1
    assert horizon_bucket(167.999) == 1
    assert horizon_bucket(168.0) == 2
    assert horizon_bucket(719.999) == 2
    assert horizon_bucket(720.0) == 3


def test_degenerate_horizons_do_not_crash_or_misroute():
    assert horizon_bucket(-5.0) == 0            # already expiring: nearest bucket
    assert horizon_bucket(float("nan")) == 0    # unreadable: no long-horizon claim
    assert horizon_bucket(float("inf")) == 3    # arbitrarily far IS the far bucket


def test_the_table_reads_across_domain_and_horizon():
    assert table_b("weather", 24.0) == 0.83     # the paper's measured bucket
    assert table_b("weather", 48.0) == 1.0      # unmeasured: no correction
    assert table_b("politics", 100.0) == 1.38   # mid of 0.93-1.83, longer horizon
    assert table_b("politics", 24.0) == 1.0     # near resolution: no license
    # >1mo compresses toward 50 in every measured domain (favorite-longshot bias).
    for domain in ("weather", "sports", "politics", "finance", "crypto"):
        assert table_b(domain, 800.0) == 0.90


# --- clamps and pathological prices ------------------------------------------

def test_output_is_clamped_to_one_and_ninetynine():
    # politics at a week-plus has b_eff>1, pushing extremes MORE extreme.
    assert calibrated_prob(0.995, "politics", 300.0) <= 0.99
    assert calibrated_prob(0.005, "politics", 300.0) >= 0.01


def test_degenerate_prices_are_handled_not_raised():
    for p in (0.0, 1.0, -0.1, 1.1):
        out = calibrated_prob(p, "weather", 24.0)
        assert 0.01 <= out <= 0.99


# --- the veto ----------------------------------------------------------------

def test_veto_fires_when_the_corrected_market_agrees_with_the_model():
    """Price 10c, model 11c: a 1c "edge". The correction says the market's true read
    is ~11.8c - the model and the corrected price are on the same side of the quote,
    so the edge WAS the miscalibration, priced backwards. Suppress."""
    a = assess(0.11, 0.10, "weather", 24.0, shrink=0.5)
    assert isinstance(a, Assessment) and a.ok is False
    assert a.cal_prob > 0.10                   # pulled toward 0.5
    assert a.cal_edge < 0 < 0.11 - 0.10        # the sign flipped


def test_veto_fires_when_the_corrected_edge_is_below_the_fee():
    a = assess(0.125, 0.10, "weather", 24.0, shrink=0.5, min_abs_edge=0.0175)
    assert a.ok is False and 0 < a.cal_edge < 0.0175


def test_no_veto_when_the_model_still_disagrees_after_correction():
    a = assess(0.30, 0.10, "weather", 24.0, shrink=0.5, min_abs_edge=0.0175)
    assert a.ok is True and a.reason == ""
    # The correction shrank the edge without erasing it - both numbers recorded.
    assert 0 < a.cal_edge < 0.30 - 0.10
    assert a.cal_b == pytest.approx(0.915)


def test_shrink_zero_never_vetoes_a_ticket_the_sleeve_approved():
    """With shrink=0 the corrected price IS the price, so any edge that cleared the
    sleeve's own bar (which exceeds the fee) must pass untouched."""
    a = assess(0.17, 0.10, "weather", 24.0, shrink=0.0, min_abs_edge=0.0175)
    assert a.ok is True
    assert a.cal_prob == pytest.approx(0.10)
    assert a.cal_edge == pytest.approx(0.07)


# --- the weather wiring ------------------------------------------------------

_NOW = datetime(2026, 8, 12, 16, 0, tzinfo=timezone.utc)
_NY = {"KXHIGHNY": CITIES["KXHIGHNY"]}


def _kalshi_markets(close_time="2026-08-13T23:00:00Z"):
    """The full NYC-shaped ladder as the Kalshi API serves it, for AUG 13."""
    spec = [(None, 83), (83, 84), (85, 86), (87, 88), (89, 90), (90, None)]
    asks = [0.05, 0.42, 0.35, 0.10, 0.06, 0.02]
    markets = [{
        "ticker": f"KXHIGHNY-26AUG13-{i}", "event_ticker": "KXHIGHNY-26AUG13",
        "status": "active", "title": f"{f}-{c}", "close_time": close_time,
        "floor_strike": f, "cap_strike": c,
        "yes_bid": round((a - 0.01) * 100), "yes_ask": round(a * 100),
    } for i, ((f, c), a) in enumerate(zip(spec, asks))]
    return lambda url, params=None: {"markets": markets}


def _grid(high_f):
    def fetch(url, params=None):
        if "/points/" in url:
            return {"properties": {"forecastGridData": "https://grid"}}
        return {"properties": {"maxTemperature": {"uom": "wmoUnit:degF", "values": [
            {"validTime": "2026-08-13T12:00:00+00:00/PT13H", "value": high_f}]}}}
    return fetch


def test_hours_to_expiry_prefers_the_markets_own_close_time():
    assert _hours_to_expiry("2026-08-13T20:00:00Z", "2026-08-13", _NOW) == \
        pytest.approx(28.0)
    # No close_time: end of the target day, which for a daily high is hours off.
    assert _hours_to_expiry("", "2026-08-13", _NOW) == pytest.approx(32.0)
    # Unparseable close_time falls back rather than raising.
    assert _hours_to_expiry("not-a-date", "2026-08-13", _NOW) == pytest.approx(32.0)


def test_passing_tickets_carry_the_calibration_verdict():
    tickets = find_weather_edges(cities=_NY, kalshi_fetch=_kalshi_markets(),
                                 nws_fetch=_grid(89.5), min_edge=0.05, now=_NOW)
    assert tickets, "a 4.5F disagreement should still clear 5c with the overlay on"
    for t in tickets:
        assert "cal_prob" in t and "cal_edge" in t and "cal_b" in t
        assert t["cal_b"] == pytest.approx(0.915)          # weather <48h, shrink 0.5
        assert 0 < t["cal_edge"] <= t["edge"] + 1e-9       # veto passed, edge shrank
        # And the trial will carry all three fields onto its rows.
        from predictionedge.papertrial import MODEL_FIELDS
        assert {"cal_prob", "cal_edge", "cal_b"} <= set(MODEL_FIELDS)


def test_flag_off_is_a_pure_pass_through():
    on = find_weather_edges(cities=_NY, kalshi_fetch=_kalshi_markets(),
                            nws_fetch=_grid(89.5), min_edge=0.05, now=_NOW)
    off = find_weather_edges(cities=_NY, kalshi_fetch=_kalshi_markets(),
                             nws_fetch=_grid(89.5), min_edge=0.05, now=_NOW,
                             cal_overlay=False)
    assert off, "the sleeve must behave exactly as before the overlay existed"
    assert all("cal_prob" not in t and "cal_edge" not in t and "cal_b" not in t
               for t in off)
    assert [t["market_id"] for t in off] == [t["market_id"] for t in on]
    for t_on, t_off in zip(on, off):
        assert t_on["model_prob"] == t_off["model_prob"]   # never a price editor
        assert t_on["edge"] == t_off["edge"]


def test_a_veto_is_counted_where_the_diagnostics_can_see_it():
    diag: dict = {}
    t = {"market_id": "KXHIGHNY-26AUG13-1", "model_prob": 0.11,
         "entry_price": 0.10, "edge": 0.01}
    assert _calibration_gate(t, 24.0, shrink=0.5, diag=diag) is False
    assert diag["cal_vetoed"] == 1
    assert "cal_prob" not in t                 # a suppressed ticket is not annotated
    keep = {"market_id": "KXHIGHNY-26AUG13-4", "model_prob": 0.30,
            "entry_price": 0.10, "edge": 0.20}
    assert _calibration_gate(keep, 24.0, shrink=0.5, diag=diag) is True
    assert diag["cal_vetoed"] == 1             # keeping does not touch the counter
    assert keep["cal_prob"] > 0.10 and keep["cal_edge"] > 0


def test_publish_passes_config_and_diag_through(monkeypatch, tmp_path):
    from predictionedge import publish

    seen = {}

    def fake_edges(**kw):
        seen.update(kw)
        kw["diag"]["cal_vetoed"] = 3
        return []

    monkeypatch.setattr("predictionedge.weather.find_weather_edges", fake_edges)
    # weather_enabled is explicit because the sleeve was retired and now defaults to
    # False. This test is about the calibration knobs reaching the finder, not about
    # the kill switch, so it arms the sleeve rather than riding on the default.
    cfg = Config(weather_enabled=True,
                 calibration_overlay=True, calibration_shrink=0.25)
    diag: dict = {"cal_vetoed": 0}
    assert publish.weather_tickets(cfg, nbm_cache_path=str(tmp_path / "nbm.json"),
                                   diag=diag) == []
    assert seen["cal_overlay"] is True and seen["cal_shrink"] == 0.25
    assert diag["cal_vetoed"] == 3


def test_config_defaults_and_kill_switches_exist():
    cfg = Config()
    assert cfg.calibration_overlay is True
    assert cfg.calibration_shrink == 0.5
    # Both knobs must be settable without a deploy, like weather_enabled is.
    assert Config(calibration_overlay=False).calibration_overlay is False
    assert Config(calibration_shrink=0.0).calibration_shrink == 0.0
