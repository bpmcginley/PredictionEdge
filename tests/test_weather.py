"""The weather model, and specifically the guards that stop it inventing edge."""

import math

from predictionedge.weather import (
    CITIES, SIGMA_FLOOR_F, WeatherCity, bracket_probability, c_to_f,
    find_weather_edges, forecast_highs, market_distribution, price_ladder,
    pricing_sigma, sigma_for_lead,
)


def _leg(floor, cap, bid, ask, ticker="KX-T"):
    return {"ticker": ticker, "title": f"{floor}-{cap}", "url": "u",
            "floor_strike": floor, "cap_strike": cap,
            "bid": bid, "ask": ask, "mid": (bid + ask) / 2}


def _full_ladder(asks=None):
    """A complete NYC-shaped partition: <83, 83-84, 85-86, 87-88, 89-90, >90."""
    spec = [(None, 83), (83, 84), (85, 86), (87, 88), (89, 90), (90, None)]
    asks = asks or [0.05, 0.42, 0.35, 0.10, 0.06, 0.02]
    return [_leg(f, c, a - 0.01, a, f"KXHIGHNY-26AUG12-{i}")
            for i, ((f, c), a) in enumerate(zip(spec, asks))]


# --- the half-degree, which is most of the answer on a narrow bracket ---------

def test_an_integer_bracket_spans_two_degrees_not_one():
    """The CLI report states whole degrees, so "85-86" is the event {85, 86}.

    Reading it as the interval [85, 86] spans 1 degree where the real event spans 2,
    understating every centre bracket by roughly half - which makes all of them look
    overpriced at once. That is the signature of invented edge.
    """
    mu, sigma = 85.5, 4.0
    p = bracket_probability(85, 86, mu, sigma)
    naive = 0.5 * (1 + math.erf((86 - mu) / (sigma * math.sqrt(2)))) - \
        0.5 * (1 + math.erf((85 - mu) / (sigma * math.sqrt(2))))
    assert p > naive * 1.8


def test_open_ended_brackets_match_how_kalshi_words_them():
    # ">90" means 91 or above; "<83" means 82 or below.
    assert bracket_probability(90, None, 90.0, 4.0) < 0.5
    assert bracket_probability(None, 83, 83.0, 4.0) < 0.5


def test_a_bracket_with_no_strikes_is_unreadable_not_certain():
    assert bracket_probability(None, None, 85.0, 4.0) is None


def test_the_whole_ladder_sums_to_one():
    mu, sigma = 85.9, 4.2
    spec = [(None, 83), (83, 84), (85, 86), (87, 88), (89, 90), (90, None)]
    total = sum(bracket_probability(f, c, mu, sigma) for f, c in spec)
    assert abs(total - 1.0) < 1e-9


# --- the guards --------------------------------------------------------------

def test_width_comes_from_the_market_not_from_the_prior():
    """The model never picks its own width, in EITHER direction.

    An earlier version took the wider of the prior and the market, on the belief that
    too-wide is the safe way to be wrong. It is not: extra width pushes mass into the
    tails and makes every tail bracket look cheap. Fed the market's own mean, that
    version printed 27c of edge on a 5c tail.
    """
    assert pricing_sigma(3.0, days_ahead=0.0) == 3.0     # market narrower than the prior
    assert pricing_sigma(6.0, days_ahead=0.0) == 6.0     # and wider: still the market
    assert pricing_sigma(0.4, days_ahead=0.0) == SIGMA_FLOOR_F   # never below the floor
    # A ladder claiming to know tomorrow to a fraction of a degree is a broken book, not
    # a conviction. No opinion beats an enormous fake edge against a stale quote.
    assert pricing_sigma(sigma_for_lead(1.0) * 3, days_ahead=1.0) is None
    assert pricing_sigma(None, days_ahead=1.0) is None


def test_sigma_grows_with_lead_time():
    assert sigma_for_lead(3) > sigma_for_lead(0)


def test_an_incomplete_ladder_yields_no_opinion():
    """A missing leg understates the market's width, which would lower our own floor.

    Same lesson as `odds.require_complete_market`: de-vigging an incomplete market
    inflated a favourite by 11 points because the survivors summed to under one.
    """
    partial = _full_ladder()[:3]                 # sums to ~0.82 of the probability mass
    for leg in partial:
        leg["mid"] = leg["ask"]
    assert market_distribution(partial) is None
    assert price_ladder(partial, 86.0, days_ahead=1.0) == []


def test_a_complete_ladder_is_read_as_a_distribution():
    m = market_distribution(_full_ladder())
    assert m is not None
    mu, sigma = m
    assert 83.0 < mu < 87.0 and sigma > 0.5


def test_a_ladder_that_does_not_sum_to_one_is_rejected():
    doubled = _full_ladder([0.9, 0.9, 0.9, 0.9, 0.9, 0.9])
    assert market_distribution(doubled) is None


# --- ticket generation -------------------------------------------------------

def test_a_forecast_that_agrees_with_the_market_produces_nothing():
    """The common, correct outcome. An empty board is the product working.

    This is the test that killed the first design. Pricing brackets directly off a
    normal centred on the forecast compares SHAPES as well as means - a real ladder is
    fatter in the centre and thinner in the tails than the moment-matched normal - so
    even fed the market's own mean it claimed 8c on the bottom bracket. Tilting the
    market's prices by a likelihood RATIO cancels the shape error, and the bar here is
    tiny on purpose: with agreement the ratio is exactly 1 and the edge is exactly minus
    the half-spread, so this must hold at any threshold, not merely at 7c.
    """
    ladder = _full_ladder()
    mu, _ = market_distribution(ladder)
    assert price_ladder(ladder, mu, days_ahead=1.0, min_edge=0.001) == []


def test_a_tenth_of_a_degree_is_not_a_trade():
    """Edge must scale with disagreement, not appear the moment there is any."""
    ladder = _full_ladder()
    mu, _ = market_distribution(ladder)
    assert price_ladder(ladder, mu + 0.1, days_ahead=1.0, min_edge=0.07) == []


def test_a_forecast_that_disagrees_produces_a_ticket_on_the_right_leg():
    # Market centred near 85; forecast says 89-90.
    tickets = price_ladder(_full_ladder(), 89.5, days_ahead=0.0, min_edge=0.05)
    assert tickets, "a 5F disagreement should clear a 5c bar"
    assert all(t["venue"] == "kalshi" and t["source"] == "weather" for t in tickets)
    assert all(t["validated"] is False for t in tickets)
    # The winning leg must be one covering the forecast, never the market's favourite.
    assert any("89" in t["title"] or "90" in t["title"] for t in tickets)


def test_penny_markets_are_excluded_whatever_the_model_claims():
    """A 2c contract cannot be beaten by 7c of claimed edge without the claim being
    about the model rather than the market, and the fee eats the position anyway."""
    ladder = _full_ladder()
    tickets = price_ladder(ladder, 120.0, days_ahead=0.0, min_edge=0.01)
    assert all(t["entry_price"] >= 0.05 for t in tickets)


def test_every_ticket_carries_what_is_needed_to_refit_the_prior_later():
    t = price_ladder(_full_ladder(), 89.5, days_ahead=2.0, min_edge=0.05)[0]
    for field in ("forecast_f", "sigma_f", "market_mu_f", "market_sigma_f",
                  "days_ahead", "model"):
        assert field in t


def test_every_ticket_carries_the_bid_so_the_trial_can_rest_an_order_there():
    """Maker-first simulation needs a passive price to join; without the bid the
    trial can only ever model crossing the spread."""
    t = price_ladder(_full_ladder(), 89.5, days_ahead=0.0, min_edge=0.05)[0]
    assert 0.0 < t["bid"] < t["entry_price"]


# --- plumbing ----------------------------------------------------------------

# `_event_day` is the shared Kalshi ticker parser, tested once in test_kalshi_meta.py
# against a real suffixed ticker per series - including this sleeve's. This module used
# to own a copy of it, and the copy was correct only because the weather ladders happen
# to hang no suffix off the day.


def test_celsius_is_converted():
    assert abs(c_to_f(30.0) - 86.0) < 1e-9


def test_forecast_reads_celsius_and_keys_by_day():
    def fetch(url, params=None):
        if "/points/" in url:
            return {"properties": {"forecastGridData": "https://grid"}}
        return {"properties": {"maxTemperature": {"uom": "wmoUnit:degC", "values": [
            {"validTime": "2026-08-12T12:00:00+00:00/PT13H", "value": 30.0},
            {"validTime": "2026-08-13T12:00:00+00:00/PT13H", "value": 29.44},
            {"validTime": "2026-08-13T18:00:00+00:00/PT6H", "value": 99.0},
        ]}}}

    highs = forecast_highs(CITIES["KXHIGHNY"], fetch=fetch)
    assert abs(highs["2026-08-12"] - 86.0) < 0.01
    assert abs(highs["2026-08-13"] - 84.99) < 0.05      # first reading wins, not 99


def test_a_missing_forecast_is_not_a_free_opinion():
    """Fail closed. No forecast must mean no ticket, never a default view."""
    def boom(url, params=None):
        raise RuntimeError("api.weather.gov down")

    city = {"KXHIGHNY": CITIES["KXHIGHNY"]}
    assert find_weather_edges(cities=city, nws_fetch=boom,
                              kalshi_fetch=lambda u, p: {"markets": []}) == []


def test_every_registered_city_names_its_settlement_document():
    """Chicago settles on MIDWAY, not O'Hare. The registry is hand-audited because the
    obvious inference is wrong at least once."""
    assert "MDW" in CITIES["KXHIGHCHI"].cli_url
    assert CITIES["KXHIGHCHI"].name == "Chicago Midway, IL"
    for city in CITIES.values():
        assert city.wfo and city.cli and city.cli_url.startswith("https://")
        assert city.station.startswith("K")   # verified live in the NBM bulletins


# --- the NBM station switch --------------------------------------------------

from datetime import datetime, timezone  # noqa: E402


def _kalshi_markets():
    """The full NYC-shaped ladder as the Kalshi API would serve it, for AUG 13."""
    spec = [(None, 83), (83, 84), (85, 86), (87, 88), (89, 90), (90, None)]
    asks = [0.05, 0.42, 0.35, 0.10, 0.06, 0.02]
    markets = [{
        "ticker": f"KXHIGHNY-26AUG13-{i}", "event_ticker": "KXHIGHNY-26AUG13",
        "status": "active", "title": f"{f}-{c}", "close_time": "",
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


_NOW = datetime(2026, 8, 12, 16, 0, tzinfo=timezone.utc)
_NY = {"KXHIGHNY": CITIES["KXHIGHNY"]}
_no_nbm = lambda url, peek=False: None  # noqa: E731  (bulletin host offline)


def test_the_station_number_outranks_the_grid_cell():
    """The grid cell agrees with the market; the thermometer's own guidance does not.

    Settlement is the thermometer, so the thermometer's number prices the day - and
    the grid's number rides along, which is what turns the grid-versus-station gap
    from folklore into a measured column."""
    cache = {"stations": {"KNYC": {"2026-08-13": {
        "f": 89.5, "sd": 2.0, "cycle": "2026-08-12T07Z"}}},
        "cycles_seen": ["2026-08-12T07Z"]}
    tickets = find_weather_edges(cities=_NY, kalshi_fetch=_kalshi_markets(),
                                 nws_fetch=_grid(85.0), nbm_cache=cache,
                                 nbm_fetch=_no_nbm, min_edge=0.05, now=_NOW)
    assert tickets, "a 4.5F station-vs-market disagreement should clear 5c"
    t = tickets[0]
    assert t["forecast_f"] == 89.5
    assert t["model"] == "kalshi-ladder-tilted-by-nbm-station"
    assert t["forecast_src"] == "nbm-station"
    assert t["nbm_cycle"] == "2026-08-12T07Z" and t["nbm_sd_f"] == 2.0
    assert t["forecast_grid_f"] == 85.0


def test_the_grid_cell_still_prices_a_day_the_bulletin_does_not_cover():
    cache = {"stations": {}, "cycles_seen": []}
    tickets = find_weather_edges(cities=_NY, kalshi_fetch=_kalshi_markets(),
                                 nws_fetch=_grid(89.5), nbm_cache=cache,
                                 nbm_fetch=_no_nbm, min_edge=0.05, now=_NOW)
    assert tickets and tickets[0]["forecast_src"] == "gridpoint"
    assert tickets[0]["model"] == "kalshi-ladder-tilted-by-nws-gridpoint"


def test_a_dead_gridpoint_does_not_silence_a_cached_station_number():
    """The old code failed closed on a gridpoint outage. With station guidance cached
    the day is still priceable - and still fails closed when BOTH are missing."""
    def boom(url, params=None):
        raise RuntimeError("api.weather.gov down")

    cache = {"stations": {"KNYC": {"2026-08-13": {
        "f": 89.5, "sd": 2.0, "cycle": "2026-08-12T07Z"}}},
        "cycles_seen": ["2026-08-12T07Z"]}
    tickets = find_weather_edges(cities=_NY, kalshi_fetch=_kalshi_markets(),
                                 nws_fetch=boom, nbm_cache=cache,
                                 nbm_fetch=_no_nbm, min_edge=0.05, now=_NOW)
    assert tickets and tickets[0]["forecast_src"] == "nbm-station"
    assert "forecast_grid_f" not in tickets[0]


def test_the_retired_sleeve_stays_retired_until_someone_arms_it():
    """The sleeve was retired on measured evidence (see config.weather_enabled), so
    the DEFAULT is what carries the decision - not a comment. A future edit that flips
    the default back would silently resume betting a thesis that was tested and failed,
    which is exactly the failure this pins. The env override is deliberately preserved:
    the kill must be reversible by whoever re-opens the case, just not by accident.
    """
    from predictionedge.config import Config
    from predictionedge import publish

    assert Config().weather_enabled is False
    assert publish.weather_tickets(Config()) == []       # gate short-circuits, no I/O
    assert Config(weather_enabled=True).weather_enabled is True
