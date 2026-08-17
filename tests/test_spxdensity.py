"""The 0DTE index-density sleeve, and specifically its two honesty corrections.

The fixture is a synthetic CBOE delayed-quotes payload: ~26 usable SPXW strikes
around spot 6400 priced off a known Black-76 smile (base vol 0.25, mild put skew),
three hours before the 4pm close. Because the generating distribution is known,
the tests can assert the reconstruction moves the RIGHT way - shrink thins the
tails, re-centering shifts mass toward the new spot - rather than merely that
numbers came out.
"""

import math
from datetime import datetime, timezone

from predictionedge import papertrial, publish
from predictionedge.config import Config
from predictionedge.deribit import black76
from predictionedge.fees import INDEX_FEE_MULTIPLIER, fee_per_contract, trade_fee
from predictionedge.spxdensity import (
    MODEL_TAG, atm_sigma, bracket_prob, build_smile, close_ts_utc,
    et_now, find_spx_edges, in_blackout, parse_chain, parse_option_symbol,
)

_YEAR_SECONDS = 365.25 * 24 * 3600

# 13:00 EDT on a Thursday: mid-session, three hours before the close.
NOW = datetime(2026, 8, 13, 17, 0, tzinfo=timezone.utc)
EXPIRY_TS = close_ts_utc(et_now(NOW).date())
TAU = (EXPIRY_TS - NOW.timestamp()) / _YEAR_SECONDS
SPOT = 6400.0

# A partition of the close, Kalshi-ladder shaped, sitting inside the quoted strikes.
SPEC = [(None, 6370), (6370, 6390), (6390, 6410), (6410, 6430), (6430, None)]


def _sym(strike, is_call, root="SPXW", ymd="260813"):
    return f"{root}{ymd}{'C' if is_call else 'P'}{int(strike * 1000):08d}"


def _payload(spot=SPOT, sigma0=0.25, skew=-0.6, curv=4.0):
    """Both legs at every strike, priced off a known smile; deep tails lose their bid
    to the rounding exactly the way the real payload's dead wings do."""
    opts = []
    for strike in range(6300, 6501, 5):
        k = math.log(strike / spot)
        sig = sigma0 + skew * k + curv * k * k
        for is_call in (True, False):
            p = black76(spot, strike, sig, TAU, is_call)
            half = max(0.02, p * 0.015)
            opts.append({"option": _sym(strike, is_call),
                         "bid": round(max(0.0, p - half), 2),
                         "ask": round(p + half, 2)})
    return {"timestamp": "2026-08-13 17:00:00", "symbol": "_SPX",
            "data": {"current_price": spot, "options": opts}}


def _smile(shrink=1.0, spot_used=None, payload=None, min_strikes=8):
    chain = parse_chain(payload or _payload(), "_SPX", "SPXW", "2026-08-13")
    return build_smile(chain, expiry_ts=EXPIRY_TS, now_ts=NOW.timestamp(),
                       vrp_shrink=shrink, min_strikes=min_strikes,
                       max_spread_frac=0.25, spot_used=spot_used)


def _probs(smile):
    return [bracket_prob(smile, f, c) for f, c in SPEC]


# --- parsing -----------------------------------------------------------------

# `_event_day` is the shared Kalshi ticker parser, tested once in test_kalshi_meta.py
# against a real suffixed ticker per series. It is NOT re-tested per sleeve on purpose:
# three copies of the test would have passed just as happily as three copies of the
# parser did, one of which was broken for the life of this sleeve.


def test_option_symbol_parses_root_expiry_side_strike():
    assert parse_option_symbol("SPXW260813C06400000") == \
        ("SPXW", "2026-08-13", True, 6400.0)
    assert parse_option_symbol("NDXP260813P23450000") == \
        ("NDXP", "2026-08-13", False, 23450.0)
    assert parse_option_symbol("garbage") is None


def test_parse_chain_keeps_only_the_daily_root_and_the_day():
    payload = _payload()
    # Contaminate with an AM-settled monthly and a different day; both must vanish.
    payload["data"]["options"] += [
        {"option": _sym(6400, True, root="SPX"), "bid": 5.0, "ask": 6.0},
        {"option": _sym(6400, True, ymd="260814"), "bid": 5.0, "ask": 6.0},
    ]
    chain = parse_chain(payload, "_SPX", "SPXW", "2026-08-13")
    assert chain.spot == SPOT and chain.chain_ts == "2026-08-13 17:00:00"
    assert all(6300 <= q.strike <= 6500 for q in chain.quotes)
    # The monthly root did not sneak in as a duplicate 6400 quote.
    assert sum(1 for q in chain.quotes if q.strike == 6400 and q.is_call) == 1


def test_parse_chain_drops_zero_bid_tails():
    """A zero-bid deep tail is the absence of a price. Fitting the smile through it
    would let the least reliable quotes shape exactly the region the Kalshi tails
    pay on."""
    chain = parse_chain(_payload(), "_SPX", "SPXW", "2026-08-13")
    assert chain.quotes, "sanity: the fixture produced quotes"
    assert all(q.bid > 0 for q in chain.quotes)
    # The far OTM wings really were dropped, not merely absent from the fixture.
    # (The ITM leg at the same strike keeps its bid - intrinsic value is real -
    # which is exactly why the smile builder prices the OTM side only.)
    assert not any(q.strike == 6300 and not q.is_call for q in chain.quotes)
    assert not any(q.strike == 6500 and q.is_call for q in chain.quotes)


# --- the density -------------------------------------------------------------

def test_bracket_probabilities_partition_to_one():
    probs = _probs(_smile())
    assert all(p is not None for p in probs)
    assert abs(sum(probs) - 1.0) < 1e-9


def test_digital_is_monotone_so_the_density_is_a_density():
    smile = _smile()
    digitals = [smile.digital(k) for k in range(6370, 6431, 5)]
    assert all(d is not None for d in digitals)
    assert all(a >= b for a, b in zip(digitals, digitals[1:]))


def test_the_atm_bracket_carries_the_most_mass():
    probs = _probs(_smile())
    assert max(probs) == probs[2]           # 6390-6410 straddles spot 6400
    # And the ladder is sensibly humped, not flat noise.
    assert probs[2] > probs[1] > probs[0]
    assert probs[2] > probs[3] > probs[4]


def test_a_thin_chain_fails_closed():
    assert _smile(min_strikes=200) is None


def test_a_bracket_edge_off_the_fitted_ladder_is_unpriceable():
    """The deribit lesson: a "between" whose cap falls off the ladder is not
    half-priceable - the floor leg alone would silently assert P(above cap) = 0."""
    smile = _smile()
    assert bracket_prob(smile, 6390, 9999.0) is None
    assert bracket_prob(smile, None, None) is None


# --- correction 2: the VRP shrink --------------------------------------------

def test_vrp_shrink_thins_both_tails():
    """Risk-neutral tails are fatter than real-world ones; the shrink must move
    mass from the tails into the centre, in both directions at once."""
    base, shrunk = _probs(_smile(1.0)), _probs(_smile(0.92))
    assert shrunk[0] < base[0]              # lower tail thinner
    assert shrunk[-1] < base[-1]            # upper tail thinner
    assert shrunk[2] > base[2]              # centre fatter
    assert abs(sum(shrunk) - 1.0) < 1e-9    # still a distribution


def test_shrink_scales_the_recorded_sigma():
    assert abs(atm_sigma(_smile(0.92)) - 0.92 * atm_sigma(_smile(1.0))) < 1e-6


# --- correction 1: re-centering ----------------------------------------------

def test_recentering_moves_mass_toward_the_new_spot():
    """Sticky-moneyness: the smile slides with `spot_used`, so a higher spot must
    raise every upper bracket and cut the lower tail."""
    base, up = _probs(_smile()), _probs(_smile(spot_used=6415.0))
    assert up[3] > base[3] and up[4] > base[4]
    assert up[0] < base[0]
    assert abs(sum(up) - 1.0) < 1e-9


def test_default_location_is_the_chains_own_spot():
    """Same-timestamp internal consistency: with no override, the density sits on
    the delayed snapshot's own index level, not on anything fresher."""
    assert _smile().forward == SPOT


# --- tickets -----------------------------------------------------------------

def _kalshi_markets(quotes):
    """The KXINX ladder as the API serves it: cents ints, active, one event day.

    Event tickers carry the live `H1600` settlement-hour suffix (verified 2026-08-16).
    That matters here rather than being cosmetic: this fixture used the suffix-less
    weather shape, so every ticket test passed while the sleeve was blind to every
    real market on the venue.

    `quotes` maps SPEC index -> (yes_bid_c, yes_ask_c); anything absent is quoted
    fairly off the shrunk density so it produces no ticket.
    """
    fair = _probs(_smile(0.92))
    markets = []
    for i, (floor, cap) in enumerate(SPEC):
        if i in quotes:
            bid_c, ask_c = quotes[i]
        else:
            ask_c = round(fair[i] * 100) + 1
            bid_c = ask_c - 2
        markets.append({
            "ticker": f"KXINX-26AUG13H1600-B{i}",
            "event_ticker": "KXINX-26AUG13H1600",
            "status": "active", "title": f"bracket {floor}-{cap}",
            "floor_strike": floor, "cap_strike": cap,
            "yes_bid": bid_c, "yes_ask": ask_c,
        })
    return markets


def _run(quotes=None, cfg=None, now=NOW, markets=None):
    markets = markets if markets is not None else _kalshi_markets(quotes or {})
    return find_spx_edges(
        cfg or Config(),
        cboe_fetch=lambda url, params=None: _payload(),
        kalshi_fetch=lambda url, params=None: {"markets": markets},
        now=now)


def test_agreement_with_the_market_produces_nothing():
    """The common, correct outcome: fairly-priced brackets are not tickets."""
    assert _run() == []


def test_a_mispriced_bracket_tickets_the_right_leg():
    # Centre bracket fair ~0.286 (shrunk) offered at 20c: ~8.6c of edge.
    tickets = _run({2: (18, 20)})
    assert len(tickets) == 1
    t = tickets[0]
    assert t["market_id"] == "KXINX-26AUG13H1600-B2" and t["outcome"] == "Yes"
    assert t["source"] == "spxindex" and t["venue"] == "kalshi"
    assert t["model"] == MODEL_TAG and t["validated"] is False
    assert t["entry_price"] == 0.20
    assert abs(t["fair"] - t["model_prob"]) < 1e-9
    assert t["edge"] > 0.05


def test_an_overpriced_bracket_tickets_the_no_side():
    # Bracket 1 fair ~0.219 (shrunk) bid at 30c: NO at 70c is worth ~78c.
    tickets = _run({1: (30, 32)})
    assert len(tickets) == 1
    t = tickets[0]
    assert t["outcome"] == "No" and t["entry_price"] == 0.70
    assert abs(t["model_prob"] - (1.0 - t["fair"])) < 1e-9


def test_the_bar_is_the_index_fee_plus_the_threshold():
    """A mispricing worth less than fee + spx_min_edge is not a ticket - and the fee
    in that bar is the 0.035 index multiplier, not the general 0.07."""
    fair = _probs(_smile(0.92))[2]                      # ~0.286
    min_edge = 0.027
    bar_35 = fee_per_contract(0.25, multiplier=INDEX_FEE_MULTIPLIER) + min_edge
    bar_70 = fee_per_contract(0.25, multiplier=0.07) + min_edge
    edge = fair - 0.25
    # The fixture edge is chosen to sit BETWEEN the two bars: it must ticket under
    # the index schedule and would not under the general one, so this test fails
    # if anyone ever wires the wrong multiplier into the gate.
    assert bar_35 < edge < bar_70
    assert len(_run({2: (23, 25)}, cfg=Config(spx_min_edge=min_edge))) == 1
    # And a mispricing below even the index bar stays untraded.
    assert _run({2: (24, 26)}, cfg=Config(spx_min_edge=min_edge)) == []


def test_no_book_means_no_opinion_not_a_reference_price():
    """The crossvenue lesson: a bracket quoted 0/0 is nobody's price. However far
    it sits from fair value, it must be skipped, not traded against."""
    assert _run({2: (0, 0)}) == []


def test_blackout_suppresses_tickets_near_the_close():
    """15-minute-delayed data at 3:30pm describes 3:15pm; near settlement that gap
    is most of a bracket. The mispricing that tickets at 1pm must not at 3:30."""
    quotes = {2: (18, 20)}
    assert _run(quotes) != []
    late = datetime(2026, 8, 13, 19, 30, tzinfo=timezone.utc)     # 15:30 EDT
    assert _run(quotes, now=late) == []


def test_blackout_wall_clock_math():
    cfg = Config()
    assert not in_blackout(cfg, datetime(2026, 8, 13, 18, 59, tzinfo=timezone.utc))
    assert in_blackout(cfg, datetime(2026, 8, 13, 19, 0, tzinfo=timezone.utc))
    # January is EST: the same UTC hour is one ET hour later.
    assert in_blackout(cfg, datetime(2026, 1, 13, 20, 0, tzinfo=timezone.utc))
    assert not in_blackout(cfg, datetime(2026, 1, 13, 19, 59, tzinfo=timezone.utc))


def test_close_time_tracks_daylight_saving():
    from datetime import date
    aug = close_ts_utc(date(2026, 8, 13))
    jan = close_ts_utc(date(2026, 1, 13))
    assert datetime.fromtimestamp(aug, timezone.utc).hour == 20      # EDT
    assert datetime.fromtimestamp(jan, timezone.utc).hour == 21      # EST


def test_a_stale_event_day_is_ignored():
    """Only the ladder that settles on THIS close. Yesterday's finalized event
    coming along in the same series listing must not be priced off today's chain."""
    markets = _kalshi_markets({2: (18, 20)})
    for m in markets:
        m["event_ticker"] = "KXINX-26AUG12H1600"
        m["ticker"] = m["ticker"].replace("26AUG13", "26AUG12")
    assert _run(markets=markets) == []


def test_tickets_carry_the_honesty_metadata():
    t = _run({2: (18, 20)})[0]
    assert t["spot_used"] == SPOT
    assert t["chain_ts"] == "2026-08-13 17:00:00"
    assert t["vrp_shrink"] == 0.92
    assert t["implied_sigma"] > 0
    assert t["series"] == "KXINX"
    assert t["end_iso"].startswith("2026-08-13T20:00")


def test_disabled_sleeve_emits_nothing():
    assert _run({2: (18, 20)}, cfg=Config(spx_enabled=False)) == []


# --- wiring into the board and the trial -------------------------------------

def test_publish_survives_a_cboe_outage(monkeypatch):
    def boom(cfg):
        raise RuntimeError("cdn.cboe.com down")
    monkeypatch.setattr("predictionedge.spxdensity.find_spx_edges", boom)
    assert publish.spxindex_tickets(Config()) == []
    assert publish.spxindex_tickets(Config(spx_enabled=False)) == []


def test_trial_rows_use_the_index_fee_schedule_and_keep_the_metadata():
    """The row must be booked as a taker cross at multiplier 0.035, and carry the
    fields a later re-fit needs (spot_used, chain_ts, vrp_shrink, fair,
    implied_sigma) - exactly the way weather's forecast fields ride along."""
    ticket = _run({2: (18, 20)})[0]
    trial = {"open": [], "settled": [], "stats": {}}
    added = papertrial.record(
        trial, [{**ticket, "_shown": True, "_maker": False,
                 "_fee_mult": INDEX_FEE_MULTIPLIER}],
        stake=100.0, fee_multiplier=0.07, now=1_000_000.0)
    assert added == 1
    row = trial["open"][0]
    assert row["source"] == "spxindex" and row["venue"] == "kalshi"
    contracts = max(1, round(row["contracts"]))
    assert row["fee"] == trade_fee(row["price"], contracts,
                                   multiplier=INDEX_FEE_MULTIPLIER, maker=False)
    # And NOT what the trial-wide default multiplier would have charged. The fee
    # SCHEDULE is what this asserts; every instant fill is a taker fill either way.
    assert row["fee"] != trade_fee(row["price"], contracts,
                                   multiplier=0.07, maker=False)
    for field in ("spot_used", "chain_ts", "vrp_shrink", "fair",
                  "implied_sigma", "series", "model_prob", "edge", "model"):
        assert field in row, field


def test_the_index_multiplier_is_half_the_general_one():
    assert INDEX_FEE_MULTIPLIER == 0.035
