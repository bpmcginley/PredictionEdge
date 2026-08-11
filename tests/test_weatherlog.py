"""The forecast log, whose whole value is that it is not a record of our bets."""

from predictionedge.weather import CITIES
from predictionedge.weatherlog import load, observe, resolve, summarise


def _kalshi(markets):
    def fetch(url, params):
        return {"markets": markets}
    return fetch


def _nws(temp_f):
    celsius = (temp_f - 32) * 5 / 9

    def fetch(url, params=None):
        if "/points/" in url:
            return {"properties": {"forecastGridData": "https://grid"}}
        return {"properties": {"maxTemperature": {"uom": "wmoUnit:degC", "values": [
            {"validTime": "2026-08-12T12:00:00+00:00/PT13H", "value": celsius}]}}}
    return fetch


def _ladder(event="KXHIGHNY-26AUG12", asks=(0.05, 0.42, 0.35, 0.10, 0.06, 0.02)):
    spec = [(None, 83), (83, 84), (85, 86), (87, 88), (89, 90), (90, None)]
    return [{"ticker": f"{event}-{i}", "event_ticker": event, "status": "active",
             "floor_strike": f, "cap_strike": c,
             "yes_bid_dollars": f"{a - 0.01:.4f}", "yes_ask_dollars": f"{a:.4f}"}
            for i, ((f, c), a) in enumerate(zip(spec, asks))]


NY = {"KXHIGHNY": CITIES["KXHIGHNY"]}


def test_a_quiet_day_is_logged_too():
    """The point of the whole module. A day the model passed on is still a data point,
    and a bias fitted only on days we disagreed would measure the wrong quantity."""
    log = load("does-not-exist.json")
    n = observe(log, cities=NY, nws_fetch=_nws(84.5),      # agrees with the market
                kalshi_fetch=_kalshi(_ladder()), now=100.0)
    assert n == 1
    row = log["rows"]["KXHIGHNY:2026-08-12"]
    assert row["first_forecast_f"] == 84.5 and row["observed_f"] is None


def test_the_first_forecast_is_never_overwritten():
    """A 4-day-out call and a same-morning call are different claims. Overwriting would
    relabel every hard forecast as an easy one by the time it resolved."""
    log = load("does-not-exist.json")
    observe(log, cities=NY, nws_fetch=_nws(90.0),
            kalshi_fetch=_kalshi(_ladder()), now=100.0)
    observe(log, cities=NY, nws_fetch=_nws(85.0),
            kalshi_fetch=_kalshi(_ladder()), now=200.0)
    row = log["rows"]["KXHIGHNY:2026-08-12"]
    assert row["first_forecast_f"] == 90.0 and row["first_seen_at"] == 100.0
    assert row["last_forecast_f"] == 85.0 and row["last_seen_at"] == 200.0


def test_truth_is_the_bracket_that_paid_not_an_hourly_observation():
    """Hourly station obs miss the intraday peak - measured 0-3F low against Kalshi's
    own settlements. The settled ladder is the CLI report, so it is the only truth."""
    log = load("does-not-exist.json")
    observe(log, cities=NY, nws_fetch=_nws(88.0),
            kalshi_fetch=_kalshi(_ladder()), now=100.0)
    settled = _ladder()
    settled[3]["result"] = "yes"        # 87-88 paid
    settled[3]["yes_sub_title"] = "87° to 88°"
    assert resolve(log, fetch=_kalshi(settled), now=300.0, today="2026-08-13") == 1
    row = log["rows"]["KXHIGHNY:2026-08-12"]
    assert row["observed_f"] == 87.5 and row["observed_bracket"] == "87° to 88°"


def test_an_open_ended_winner_records_the_bracket_but_invents_no_number():
    """">90" pins the high to a bound, not a value. A made-up tail number would poison
    the fitted bias worse than a smaller honest sample would."""
    log = load("does-not-exist.json")
    observe(log, cities=NY, nws_fetch=_nws(95.0),
            kalshi_fetch=_kalshi(_ladder()), now=100.0)
    settled = _ladder()
    settled[5]["result"] = "yes"        # ">90", floor set, no cap
    resolve(log, fetch=_kalshi(settled), now=300.0, today="2026-08-13")
    row = log["rows"]["KXHIGHNY:2026-08-12"]
    assert row["resolved_at"] == 300.0 and row["observed_f"] is None
    assert summarise(log) == {}         # and it contributes nothing to the fit


def test_an_unsettled_ladder_stays_pending_rather_than_resolving_wrong():
    log = load("does-not-exist.json")
    observe(log, cities=NY, nws_fetch=_nws(88.0),
            kalshi_fetch=_kalshi(_ladder()), now=100.0)
    assert resolve(log, fetch=_kalshi(_ladder()), now=300.0, today="2026-08-13") == 0
    assert log["rows"]["KXHIGHNY:2026-08-12"]["resolved_at"] is None


def test_a_resolved_row_is_not_resolved_twice():
    log = load("does-not-exist.json")
    observe(log, cities=NY, nws_fetch=_nws(88.0),
            kalshi_fetch=_kalshi(_ladder()), now=100.0)
    settled = _ladder()
    settled[3]["result"] = "yes"
    assert resolve(log, fetch=_kalshi(settled), now=300.0, today="2026-08-13") == 1
    assert resolve(log, fetch=_kalshi(settled), now=400.0, today="2026-08-13") == 0
    assert log["rows"]["KXHIGHNY:2026-08-12"]["resolved_at"] == 300.0


def test_bias_is_forecast_minus_observed_so_positive_means_the_grid_runs_warm():
    log = {"rows": {
        "KXHIGHNY:2026-08-10": {"series": "KXHIGHNY", "city": "Central Park, New York",
                                "first_forecast_f": 88.0, "observed_f": 85.5},
        "KXHIGHNY:2026-08-11": {"series": "KXHIGHNY", "city": "Central Park, New York",
                                "first_forecast_f": 87.0, "observed_f": 85.5},
    }}
    s = summarise(log)["KXHIGHNY"]
    assert s["n"] == 2 and s["bias_f"] == 2.0 and s["mae_f"] == 2.0
    assert s["sigma_f"] > 0


def test_an_nws_outage_logs_nothing_rather_than_a_blank_row():
    def boom(url, params=None):
        raise RuntimeError("api.weather.gov down")

    log = load("does-not-exist.json")
    assert observe(log, cities=NY, nws_fetch=boom,
                   kalshi_fetch=_kalshi(_ladder())) == 0
    assert log["rows"] == {}


def test_a_day_that_has_not_happened_yet_is_never_queried():
    """This runs 96 times a day against a log full of future days. Asking Kalshi whether
    tomorrow has happened, four times an hour, forever, is a lot of calls to learn
    nothing."""
    log = load("does-not-exist.json")
    observe(log, cities=NY, nws_fetch=_nws(88.0),
            kalshi_fetch=_kalshi(_ladder()), now=100.0)

    def boom(url, params):
        raise AssertionError("queried a day that has not happened yet")

    assert resolve(log, fetch=boom, now=300.0, today="2026-08-12") == 0
    assert resolve(log, fetch=boom, now=300.0, today="2026-08-11") == 0
