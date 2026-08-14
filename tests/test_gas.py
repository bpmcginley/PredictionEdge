"""The gas model, and specifically the guards that stop it inventing edge."""

from datetime import datetime, timezone

from predictionedge import gaslog
from predictionedge.gas import (
    bracket_probability, fetch_national, find_gas_edges, market_distribution,
    parse_national, price_ladder, _event_day,
)

# Trimmed to the load-bearing shape of the live page (probed 2026-08-13): the
# `table-mob` rows with the Regular column first, and the "Price as of" stamp.
AAA_HTML = """
<div class="average-price"><p>Today's AAA<br />National Average </p>
    <p class="numb">$4.0715 <i class="fa fa-caret-up"></i></p>
    <span>Price as of<br />8/13/26</span>
</div>
<div class="tblwrap"><table class="table-mob">
  <thead><tr><th></th><th>Regular</th><th>Mid-Grade</th><th>Premium</th></tr></thead>
  <tbody>
    <tr><td>Current Avg.</td><td>$4.0715</td><td>$4.5842</td><td>$4.9664</td></tr>
    <tr><td>Yesterday Avg.</td><td>$4.0360</td><td>$4.5484</td><td>$4.9333</td></tr>
    <tr><td>Week Ago Avg.</td><td>$4.0633</td><td>$4.5752</td><td>$4.9565</td></tr>
    <tr><td>Month Ago Avg.</td><td>$3.8722</td><td>$4.3664</td><td>$4.7524</td></tr>
    <tr><td>Year Ago Avg.</td><td>$3.1558</td><td>$3.6477</td><td>$4.0038</td></tr>
  </tbody>
</table></div>
"""


# --- scraping ----------------------------------------------------------------

def test_the_aaa_table_parses_regular_grade_and_the_pages_own_date():
    snap = parse_national(AAA_HTML)
    assert snap is not None
    assert snap["current"] == 4.0715          # Regular, never Mid-Grade's 4.5842
    assert snap["yesterday"] == 4.0360
    assert snap["week_ago"] == 4.0633
    assert snap["month_ago"] == 3.8722
    assert snap["as_of"] == "2026-08-13"      # the page's stamp, not our clock


def test_a_page_without_the_table_is_none_not_a_guess():
    assert parse_national("<html><body>maintenance</body></html>") is None
    assert parse_national("") is None


def test_an_absurd_price_is_a_misparse_not_a_print():
    html = "<tr><td>Current Avg.</td><td>$44.99</td></tr>"   # an ad, not gasoline
    assert parse_national(html) is None


def test_a_dead_scrape_is_none_never_an_exception():
    def boom(url):
        raise RuntimeError("gasprices.aaa.com down")
    assert fetch_national(fetch=boom) is None
    assert fetch_national(fetch=lambda u: "<html>changed</html>") is None


# --- the observation log -----------------------------------------------------

def test_the_log_accumulates_prints_and_round_trips(tmp_path):
    log = gaslog.load(tmp_path / "gaslog.json")
    snap = parse_national(AAA_HTML)
    assert gaslog.record_snapshot(log, snap) == 2      # today + yesterday backfill
    assert log["days"]["2026-08-13"] == 4.0715
    assert log["days"]["2026-08-12"] == 4.0360
    gaslog.save(tmp_path / "gaslog.json", log)
    again = gaslog.load(tmp_path / "gaslog.json")
    assert again["days"] == log["days"]


def test_a_direct_print_outranks_the_next_days_echo_of_it():
    """The `yesterday` figure only backfills a day never seen directly."""
    log = {"days": {"2026-08-12": 4.0402}}
    gaslog.record_snapshot(log, {"current": 4.0715, "yesterday": 4.0360,
                                 "as_of": "2026-08-13"})
    assert log["days"]["2026-08-12"] == 4.0402         # not overwritten by 4.0360


def test_a_gap_day_yields_no_delta():
    """A two-day move dressed as a daily delta would inflate sigma."""
    log = {"days": {"2026-08-10": 4.00, "2026-08-11": 4.01,   # consecutive: 1 delta
                    "2026-08-13": 4.05}}                       # gap: no delta
    assert len(gaslog.consecutive_deltas(log)) == 1


def test_drift_and_sigma_are_computed_from_a_known_series():
    days = {f"2026-08-{d:02d}": p for d, p in
            zip(range(8, 14), [4.000, 4.010, 4.014, 4.022, 4.024, 4.036])}
    log = {"days": days}
    deltas = gaslog.consecutive_deltas(log)
    assert [round(d, 3) for d in deltas] == [0.010, 0.004, 0.008, 0.002, 0.012]
    drift, sigma = gaslog.drift_sigma(log, ewma_window=5, sigma_window=10,
                                      sigma_floor=0.0001)
    # Hand-rolled EWMA, alpha = 2/(5+1): seeded on the first delta.
    expect = deltas[0]
    for d in deltas[1:]:
        expect = (1 / 3) * d + (2 / 3) * expect
    assert abs(drift - expect) < 1e-12
    mean = sum(deltas) / len(deltas)
    var = sum((d - mean) ** 2 for d in deltas) / (len(deltas) - 1)
    assert abs(sigma - var ** 0.5) < 1e-12


def test_a_few_quiet_days_cannot_imply_certainty():
    """The sigma floor: three near-identical deltas do not mean tomorrow is known."""
    days = {f"2026-08-{d:02d}": 4.000 + (d - 8) * 0.0001 for d in range(8, 12)}
    _, sigma = gaslog.drift_sigma({"days": days}, sigma_floor=0.0015)
    assert sigma == 0.0015


def test_no_history_means_no_opinion():
    assert gaslog.drift_sigma({"days": {}}) is None
    two = {"days": {"2026-08-11": 4.0, "2026-08-12": 4.01, "2026-08-13": 4.02}}
    assert gaslog.drift_sigma(two, min_deltas=3) is None      # 2 deltas < 3


def test_forecast_rows_resolve_against_the_later_print():
    log = {"days": {}, "rows": {}}
    gaslog.record_forecast(log, "2026-08-14", forecast=4.078, aaa_today=4.0715,
                           drift=0.006, sigma=0.004, made_at=1000.0)
    # First sighting wins on the forecast fields; the freshest read rides alongside.
    gaslog.record_forecast(log, "2026-08-14", forecast=4.090, aaa_today=4.080,
                           drift=0.009, sigma=0.004, made_at=2000.0)
    row = log["rows"]["2026-08-14"]
    assert row["first_forecast"] == 4.078 and row["last_forecast"] == 4.090
    assert gaslog.resolve(log) == 0                    # print not out yet: pending
    log["days"]["2026-08-14"] = 4.0800
    assert gaslog.resolve(log) == 1
    assert row["observed"] == 4.08
    s = gaslog.summarise(log)
    assert s["n"] == 1 and abs(s["bias"] - (-0.002)) < 1e-9


# --- the ladder --------------------------------------------------------------

def _leg(floor, cap, bid, ask, ticker="KX-T"):
    mid = None if (bid is None or ask is None) else (bid + ask) / 2
    return {"ticker": ticker, "title": f"{floor}-{cap}", "url": "u",
            "floor_strike": floor, "cap_strike": cap,
            "bid": bid, "ask": ask, "mid": mid}


SPEC = [(None, 4.06), (4.06, 4.065), (4.065, 4.07),
        (4.07, 4.075), (4.075, 4.08), (4.08, None)]


def _full_ladder(asks=None):
    """A complete half-cent partition around $4.07, mids summing to ~0.97."""
    asks = asks or [0.08, 0.25, 0.32, 0.20, 0.10, 0.05]
    return [_leg(f, c, a - 0.01, a, f"KXAAAGASD-26AUG14-{i}")
            for i, ((f, c), a) in enumerate(zip(SPEC, asks))]


def test_the_whole_ladder_sums_to_one():
    total = sum(bracket_probability(f, c, 4.068, 0.004) for f, c in SPEC)
    assert abs(total - 1.0) < 1e-9


def test_a_bracket_with_no_strikes_is_unreadable_not_certain():
    assert bracket_probability(None, None, 4.07, 0.004) is None


def test_an_incomplete_ladder_yields_no_opinion():
    partial = _full_ladder()[1:4]              # interior legs only: mass ~0.72
    assert market_distribution(partial) is None
    assert price_ladder(partial, 4.08, 0.004) == []


def test_a_complete_ladder_is_read_as_a_distribution():
    m = market_distribution(_full_ladder())
    assert m is not None
    mu, sigma = m
    assert 4.06 < mu < 4.075 and sigma > 0.001


# --- the agreement-cannot-ticket invariant -----------------------------------

def test_a_forecast_that_agrees_with_the_market_produces_nothing():
    """The invariant the weather sleeve learned the hard way, asserted at a ZERO
    threshold: with agreement every likelihood ratio is exactly 1, the tilted fair
    is the quoted mid, and the edge is exactly minus the half-spread minus fee -
    so this must hold structurally, not merely at the configured bar."""
    ladder = _full_ladder()
    mu, _ = market_distribution(ladder)
    assert price_ladder(ladder, mu, 0.004, min_edge=0.0) == []
    assert price_ladder(ladder, mu, 0.001, min_edge=0.0) == []   # any sigma


def test_a_tenth_of_a_cent_is_not_a_trade():
    """Edge must scale with disagreement, not appear the moment there is any."""
    ladder = _full_ladder()
    mu, _ = market_distribution(ladder)
    assert price_ladder(ladder, mu + 0.001, 0.004, min_edge=0.05) == []


def test_a_forecast_that_disagrees_produces_a_ticket_on_the_right_leg():
    # Market centred near 4.0665; forecast says the print lands at/above 4.08.
    tickets = price_ladder(_full_ladder(), 4.082, 0.004, min_edge=0.03)
    assert tickets, "a 1.5c disagreement should clear a 3c bar"
    assert all(t["venue"] == "kalshi" and t["source"] == "gas" for t in tickets)
    assert all(t["validated"] is False for t in tickets)
    # Only legs covering the forecast may win - never the market's favourite.
    winners = {t["market_id"].rsplit("-", 1)[-1] for t in tickets}
    assert winners <= {"4", "5"}               # the 4.075-4.08 and >4.08 legs


def test_a_no_book_bracket_is_skipped_not_imputed():
    ladder = _full_ladder()
    ladder[4]["ask"] = None                    # book pulled on the 4.075-4.08 leg
    ladder[4]["mid"] = 0.095                   # mid still readable from last trade
    tickets = price_ladder(ladder, 4.082, 0.004, min_edge=0.03)
    assert all(t["market_id"] != ladder[4]["ticker"] for t in tickets)


def test_penny_markets_are_excluded_whatever_the_model_claims():
    tickets = price_ladder(_full_ladder(), 4.30, 0.004, min_edge=0.01)
    assert all(t["entry_price"] >= 0.05 for t in tickets)


def test_every_ticket_carries_what_is_needed_to_refit_the_model_later():
    t = price_ladder(_full_ladder(), 4.082, 0.004, min_edge=0.03)[0]
    for field in ("forecast", "sigma", "lr_tilt", "market_mu", "market_sigma",
                  "market_prob", "model"):
        assert field in t


# --- the wired sleeve --------------------------------------------------------

_NOW = datetime(2026, 8, 13, 16, 0, tzinfo=timezone.utc)


def _seed_log():
    """Six prints through 08-12; the scrape supplies 08-13."""
    return {"days": {f"2026-08-{d:02d}": p for d, p in
                     zip(range(7, 13),
                         [4.010, 4.016, 4.022, 4.028, 4.031, 4.036])},
            "rows": {}}


def _kalshi_markets(cents=True):
    """The AUG-14 ladder as the API serves it, centred BELOW the model's forecast.

    `cents=True` serves integer-cent fields; False serves the fixed-point dollar
    strings of Kalshi's dual schema. Both must read identically.
    """
    asks = [0.08, 0.25, 0.32, 0.20, 0.10, 0.05]
    markets = []
    for i, ((f, c), a) in enumerate(zip(SPEC, asks)):
        m = {"ticker": f"KXAAAGASD-26AUG14-{i}", "event_ticker": "KXAAAGASD-26AUG14",
             "status": "active", "title": f"{f}-{c}",
             "floor_strike": f, "cap_strike": c}
        if cents:
            m["yes_bid"], m["yes_ask"] = round((a - 0.01) * 100), round(a * 100)
        else:
            m["yes_bid_dollars"], m["yes_ask_dollars"] = f"{a - 0.01:.2f}", f"{a:.2f}"
        markets.append(m)
    # A far-future event and an already-printed day: both must be ignored.
    markets.append({"ticker": "KXAAAGASD-26AUG30-0",
                    "event_ticker": "KXAAAGASD-26AUG30", "status": "active",
                    "floor_strike": 4.08, "cap_strike": None,
                    "yes_bid": 4, "yes_ask": 5})
    markets.append({"ticker": "KXAAAGASD-26AUG12-0",
                    "event_ticker": "KXAAAGASD-26AUG12", "status": "active",
                    "floor_strike": 4.02, "cap_strike": None,
                    "yes_bid": 90, "yes_ask": 95})
    return lambda url, params=None: {"markets": markets}


def test_the_sleeve_prices_tomorrow_off_todays_print_plus_drift():
    log = _seed_log()
    tickets = find_gas_edges(gaslog=log, kalshi_fetch=_kalshi_markets(),
                             aaa_fetch=lambda u: AAA_HTML, min_edge=0.02,
                             sigma_floor=0.004, now=_NOW)
    assert tickets, "a ~1.5c model-vs-market gap should clear a 2c bar"
    t = tickets[0]
    assert t["source"] == "gas" and t["venue"] == "kalshi"
    assert t["aaa_today"] == 4.0715
    assert t["aaa_target_date"] == "2026-08-14"
    assert t["drift"] > 0 and t["sigma"] >= 0.004
    assert t["forecast"] > 4.0715              # rising history: drift is upward
    assert "lr_tilt" in t and t["days_ahead"] > 0
    assert t["settles_by"].startswith("https://gasprices.aaa.com")
    # Only the AUG-14 event was priceable: AUG-30 is beyond max_days and AUG-12
    # already printed.
    assert all(t["aaa_target_date"] == "2026-08-14" for t in tickets)
    # The scrape's print entered the log and the forecast was recorded for the
    # unbiased sample, whether or not any ticket fired.
    assert log["days"]["2026-08-13"] == 4.0715
    assert log["rows"]["2026-08-14"]["first_forecast"] == t["forecast"]


def test_cents_and_dollar_payloads_price_identically():
    a = find_gas_edges(gaslog=_seed_log(), kalshi_fetch=_kalshi_markets(True),
                       aaa_fetch=lambda u: AAA_HTML, min_edge=0.02,
                       sigma_floor=0.004, now=_NOW)
    b = find_gas_edges(gaslog=_seed_log(), kalshi_fetch=_kalshi_markets(False),
                       aaa_fetch=lambda u: AAA_HTML, min_edge=0.02,
                       sigma_floor=0.004, now=_NOW)
    assert [(t["market_id"], t["entry_price"]) for t in a] == \
        [(t["market_id"], t["entry_price"]) for t in b]


def test_a_dead_scrape_means_no_tickets_never_a_crash():
    def boom(url):
        raise RuntimeError("gasprices.aaa.com down")
    log = _seed_log()
    assert find_gas_edges(gaslog=log, kalshi_fetch=_kalshi_markets(),
                          aaa_fetch=boom, now=_NOW) == []
    assert "2026-08-13" not in log["days"]     # nothing invented either


def test_sparse_history_means_silence_not_confidence():
    """Below `min_deltas` observed moves the sleeve has no opinion at all."""
    log = {"days": {}, "rows": {}}             # day one: the scrape seeds 2 prints
    assert find_gas_edges(gaslog=log, kalshi_fetch=_kalshi_markets(),
                          aaa_fetch=lambda u: AAA_HTML, now=_NOW) == []
    assert log["days"]["2026-08-13"] == 4.0715     # but the history still accrues


def test_a_dead_kalshi_feed_means_no_tickets_never_a_crash():
    def boom(url, params=None):
        raise RuntimeError("kalshi down")
    assert find_gas_edges(gaslog=_seed_log(), kalshi_fetch=boom,
                          aaa_fetch=lambda u: AAA_HTML, now=_NOW) == []


# --- plumbing ----------------------------------------------------------------

def test_event_day_parses_the_kalshi_date_suffix():
    assert _event_day("KXAAAGASD-26AUG14") == "2026-08-14"
    assert _event_day("KXAAAGASD-garbage") is None


def test_gas_rows_carry_their_model_metadata_into_the_trial():
    """`papertrial.record` must spread the gas fields onto the row - a trial that
    drops them can validate the picks while leaving the model unfalsifiable."""
    from predictionedge import papertrial

    ticket = price_ladder(_full_ladder(), 4.082, 0.004, min_edge=0.03)[0]
    ticket.update(aaa_today=4.0715, drift=0.006, days_ahead=0.8,
                  aaa_target_date="2026-08-14")
    trial = {"open": [], "settled": [], "voided": [], "stats": {}}
    assert papertrial.record(trial, [ticket], now=1000.0) == 1
    row = trial["open"][0]
    assert row["source"] == "gas"
    for field in ("aaa_today", "aaa_target_date", "drift", "sigma", "forecast",
                  "lr_tilt", "market_mu", "market_sigma", "model_prob", "edge"):
        assert field in row
