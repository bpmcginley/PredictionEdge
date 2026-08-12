"""The forecast log, whose whole value is that it is not a record of our bets."""

from predictionedge.weather import CITIES
from predictionedge.weatherlog import (backfill_tails, cli_observed, load, observe,
                                       resolve, summarise)


def _kalshi(markets):
    def fetch(url, params):
        return {"markets": markets}
    return fetch


def _no_cli(url, params=None):
    """A CLI feed with nothing in it - resolution falls back to the bracket."""
    return {}


def _cli_text(day_words="AUGUST 12 2026", maximum="95"):
    return (f"...THE CENTRAL PARK NY CLIMATE SUMMARY FOR {day_words}...\n\n"
            "TEMPERATURE (F)\n YESTERDAY\n"
            f"  MAXIMUM         {maximum}    126 PM 102    1944  84      1\n"
            "  MINIMUM         72    614 AM  56    1962  70      2\n")


def _cli(products):
    """products: list of (issuanceTime, productText), newest first like the API."""
    def fetch(url, params=None):
        if "/types/CLI/locations/" in url:
            return {"@graph": [{"id": f"p{i}", "issuanceTime": t}
                               for i, (t, _) in enumerate(products)]}
        return {"productText": products[int(url.rsplit("/p", 1)[-1])][1]}
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
    assert resolve(log, fetch=_kalshi(settled), cli_fetch=_no_cli,
                   now=300.0, today="2026-08-13") == 1
    row = log["rows"]["KXHIGHNY:2026-08-12"]
    assert row["observed_f"] == 87.5 and row["observed_bracket"] == "87° to 88°"
    assert row["observed_src"] == "bracket"


def test_an_open_ended_winner_records_the_bracket_but_invents_no_number():
    """">90" pins the high to a bound, not a value. A made-up tail number would poison
    the fitted bias worse than a smaller honest sample would."""
    log = load("does-not-exist.json")
    observe(log, cities=NY, nws_fetch=_nws(95.0),
            kalshi_fetch=_kalshi(_ladder()), now=100.0)
    settled = _ladder()
    settled[5]["result"] = "yes"        # ">90", floor set, no cap
    resolve(log, fetch=_kalshi(settled), cli_fetch=_no_cli,
            now=300.0, today="2026-08-13")
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
    assert resolve(log, fetch=_kalshi(settled), cli_fetch=_no_cli,
                   now=300.0, today="2026-08-13") == 1
    assert resolve(log, fetch=_kalshi(settled), cli_fetch=_no_cli,
                   now=400.0, today="2026-08-13") == 0
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


# --- the CLI text supplies the number the bracket cannot -------------------------

import calendar  # noqa: E402

AUG13 = float(calendar.timegm((2026, 8, 13, 12, 0, 0)))


def _resolved_tail(cli_fetch):
    log = load("does-not-exist.json")
    observe(log, cities=NY, nws_fetch=_nws(95.0),
            kalshi_fetch=_kalshi(_ladder()), now=100.0)
    settled = _ladder()
    settled[5]["result"] = "yes"        # ">90" paid: the censored case
    resolve(log, fetch=_kalshi(settled), cli_fetch=cli_fetch,
            now=300.0, today="2026-08-13")
    return log


def test_a_tail_winner_gets_its_exact_temperature_from_the_cli_text():
    """The big misses are exactly the days a calibration needs most; before this they
    were censored, which biases the fitted sigma low - the unaffordable direction."""
    cli = _cli([("2026-08-13T06:22:00+00:00", _cli_text(maximum="95"))])
    log = _resolved_tail(cli)
    row = log["rows"]["KXHIGHNY:2026-08-12"]
    assert row["observed_f"] == 95.0 and row["observed_src"] == "cli"
    assert summarise(log)["KXHIGHNY"]["n"] == 1   # the miss now counts


def test_a_cli_read_outside_the_paying_bracket_is_distrusted():
    """">90" paid but the text says 85: wrong report, wrong parse, or wrong day.
    Whichever source is lying, a number they disagree on must not enter the sample."""
    cli = _cli([("2026-08-13T06:22:00+00:00", _cli_text(maximum="85"))])
    log = _resolved_tail(cli)
    row = log["rows"]["KXHIGHNY:2026-08-12"]
    assert row["observed_f"] is None and row["observed_src"] is None


def test_an_interior_bracket_is_upgraded_to_the_exact_value():
    log = load("does-not-exist.json")
    observe(log, cities=NY, nws_fetch=_nws(88.0),
            kalshi_fetch=_kalshi(_ladder()), now=100.0)
    settled = _ladder()
    settled[2]["result"] = "yes"        # 85-86 paid
    cli = _cli([("2026-08-13T06:22:00+00:00", _cli_text(maximum="86"))])
    resolve(log, fetch=_kalshi(settled), cli_fetch=cli, now=300.0, today="2026-08-13")
    row = log["rows"]["KXHIGHNY:2026-08-12"]
    assert row["observed_f"] == 86.0 and row["observed_src"] == "cli"


def test_the_report_for_a_different_day_is_never_read_as_truth():
    """Matching is on the summary line inside the text, never on issuance time alone."""
    cli = _cli([("2026-08-13T06:22:00+00:00", _cli_text(day_words="AUGUST 13 2026",
                                                        maximum="99"))])
    log = _resolved_tail(cli)
    assert log["rows"]["KXHIGHNY:2026-08-12"]["observed_f"] is None


def test_a_missing_value_yields_nothing_rather_than_a_guess():
    cli = _cli([("2026-08-13T06:22:00+00:00", _cli_text(maximum="MM"))])
    assert cli_observed("NYC", "2026-08-12", fetch=cli) is None


def test_the_final_report_supersedes_the_preliminary():
    """Both editions summarise the same day; newest-first means the final wins."""
    cli = _cli([("2026-08-13T06:22:00+00:00", _cli_text(maximum="96")),   # final
                ("2026-08-12T20:32:00+00:00", _cli_text(maximum="94"))])  # preliminary
    assert cli_observed("NYC", "2026-08-12", fetch=cli) == 96.0


def test_backfill_fills_a_tail_the_first_attempt_missed():
    """Kalshi can settle hours before the final CLI is issued, so the first attempt
    may honestly find nothing - without a retry the day is censored after all."""
    log = _resolved_tail(_no_cli)
    assert log["rows"]["KXHIGHNY:2026-08-12"]["observed_f"] is None
    cli = _cli([("2026-08-13T06:22:00+00:00", _cli_text(maximum="95"))])
    assert backfill_tails(log, cli_fetch=cli, now=AUG13) == 1
    row = log["rows"]["KXHIGHNY:2026-08-12"]
    assert row["observed_f"] == 95.0 and row["observed_src"] == "cli"


def test_backfill_still_distrusts_an_out_of_bracket_read():
    log = _resolved_tail(_no_cli)
    cli = _cli([("2026-08-13T06:22:00+00:00", _cli_text(maximum="85"))])
    assert backfill_tails(log, cli_fetch=cli, now=AUG13) == 0
    assert log["rows"]["KXHIGHNY:2026-08-12"]["observed_f"] is None


def test_backfill_accepts_a_legacy_row_that_never_stored_its_bounds():
    """Rows resolved before the bounds rode along can only be day-validated - and the
    CLI is the settlement source itself, so its value stands. This is the NYC 85F row."""
    log = {"rows": {"KXHIGHNY:2026-08-12": {
        "series": "KXHIGHNY", "day": "2026-08-12", "event_ticker": "KXHIGHNY-26AUG12",
        "resolved_at": 300.0, "observed_f": None, "observed_bracket": ">90°"}}}
    cli = _cli([("2026-08-13T06:22:00+00:00", _cli_text(maximum="95"))])
    assert backfill_tails(log, cli_fetch=cli, now=AUG13) == 1
    assert log["rows"]["KXHIGHNY:2026-08-12"]["observed_f"] == 95.0


def test_backfill_gives_up_outside_the_products_window():
    log = {"rows": {"KXHIGHNY:2026-07-01": {
        "series": "KXHIGHNY", "day": "2026-07-01", "event_ticker": "KXHIGHNY-26JUL01",
        "resolved_at": 300.0, "observed_f": None}}}

    def boom(url, params=None):
        raise AssertionError("fetched a day the feed no longer serves")

    assert backfill_tails(log, cli_fetch=boom, now=AUG13) == 0
