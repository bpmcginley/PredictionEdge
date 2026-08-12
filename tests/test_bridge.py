"""The venue bridge: whale tickets re-recorded at PM-US and Kalshi asks.

The property under test throughout: a wrong match is worse than no match. Every
refusal path must produce a logged reason and zero tickets, and every match must be
the exact contract - team codes, date, and outcome all agreeing - at the venue's own
ask with taker fees.
"""

from predictionedge.bridge import (
    DEFAULT_KALSHI_SERIES,
    _date_token,
    build_bridge_tickets,
    kalshi_candidates,
    parse_series_env,
    permanent_skips,
    update_log,
)
from predictionedge.fees import trade_fee
from predictionedge.papertrial import record, settle
from predictionedge.polymarket_us import MockPolymarketUSClient, PMUSMarket


def _ticket(**over):
    t = {
        "market_id": "0x" + "ab" * 32,
        "title": "Will Germany win?",
        "outcome": "Yes",
        "entry_price": 0.52,
        "slug": "fifwc-ecu-ger-2026-06-25-ger",
        "conviction": 0.61,
        "n_wallets": 1,
        "whale_usd": 25000,
        "drift_c": 1.0,
        "hours_to_resolve": 5.0,
        "liquidity": 80000,
        "end_iso": "2026-06-26T00:00:00Z",
        "event_iso": "2026-06-25T19:00:00Z",
    }
    t.update(over)
    return t


def _statuses(attempts, venue):
    return {a["origin_key"]: a["status"] for a in attempts if a["venue"] == venue}


# --- date / ticker construction -------------------------------------------------

def test_date_token():
    assert _date_token("2026-08-12") == "26AUG12"
    assert _date_token("2026-01-03") == "26JAN03"
    assert _date_token("garbage") is None


def test_kalshi_candidates_exact_construction():
    cands, reason = kalshi_candidates("mlb-bal-tex-2026-08-12-tex", "Yes",
                                      DEFAULT_KALSHI_SERIES)
    assert reason == ""
    # Both home/away orders, nothing else: construction, never search.
    assert cands == ["KXMLBGAME-26AUG12BALTEX-TEX", "KXMLBGAME-26AUG12TEXBAL-TEX"]


def test_kalshi_candidates_outcome_fallback_to_ticket_outcome():
    # No trailing outcome token in the slug; the ticket's own outcome names the team.
    cands, reason = kalshi_candidates("mlb-bal-tex-2026-08-12", "BAL",
                                      DEFAULT_KALSHI_SERIES)
    assert reason == ""
    assert all(c.endswith("-BAL") for c in cands)


def test_kalshi_candidates_refusals():
    # Unmapped league: soccer has no configured Kalshi series.
    assert kalshi_candidates("fifwc-ecu-ger-2026-06-25-ger", "Yes",
                             DEFAULT_KALSHI_SERIES) == ([], "no-series")
    # Outcome that names no team (a total) must refuse, never guess a moneyline.
    assert kalshi_candidates("mlb-bal-tex-2026-08-12", "Over 8.5",
                             DEFAULT_KALSHI_SERIES) == ([], "outcome-not-a-team")
    # No date means no signature at all.
    assert kalshi_candidates("who-wins-the-election", "Yes",
                             DEFAULT_KALSHI_SERIES) == ([], "no-signature")


def test_parse_series_env_extends_defaults():
    m = parse_series_env("epl:KXEPLGAME, bad, mls : kxmlsgame")
    assert m["mlb"] == "KXMLBGAME"          # defaults survive
    assert m["epl"] == "KXEPLGAME"
    assert m["mls"] == "KXMLSGAME"          # normalised to upper
    assert "bad" not in m


# --- PM-US adapter ---------------------------------------------------------------

def test_pmus_match_records_at_the_ask():
    pmus = MockPolymarketUSClient({
        "atc-fwc-ecu-ger-2026-06-25-ger": PMUSMarket(
            "atc-fwc-ecu-ger-2026-06-25-ger", 0.50, 0.55, 0.52),
    })
    tickets, attempts = build_bridge_tickets([_ticket()], pmus_client=pmus)
    assert _statuses(attempts, "polymarket-us") == {
        "0x" + "ab" * 32 + ":yes": "matched"}
    [t] = tickets
    assert t["venue"] == "polymarket-us"
    assert t["source"] == "whale-pmus"
    assert t["entry_price"] == 0.55          # the ask, not the intl entry
    assert t["origin_market_id"] == "0x" + "ab" * 32
    assert t["outcome"] == "Yes"             # settles by the ORIGIN market's names
    assert t["_maker"] is False


def test_pmus_no_side_pays_one_minus_bid():
    pmus = MockPolymarketUSClient({
        "atc-fwc-ecu-ger-2026-06-25-ger": PMUSMarket(
            "atc-fwc-ecu-ger-2026-06-25-ger", 0.50, 0.55, 0.52),
    })
    tickets, _ = build_bridge_tickets([_ticket(outcome="No")], pmus_client=pmus)
    assert tickets[0]["entry_price"] == 0.50   # 1 - yes_bid


def test_pmus_refusals_are_logged_not_guessed():
    pmus = MockPolymarketUSClient({})        # empty venue: nothing can match
    okey = "0x" + "ab" * 32
    _, attempts = build_bridge_tickets(
        [_ticket(),
         _ticket(outcome="Germany"),                 # not a yes/no leg
         _ticket(slug="who-wins-the-election")],     # no game signature
        pmus_client=pmus)
    st = [a["status"] for a in attempts if a["venue"] == "polymarket-us"]
    assert st == ["no-counterpart", "outcome-not-yes-no", "no-signature"]
    assert okey + ":yes" in {a["origin_key"] for a in attempts}


def test_pmus_collision_refused_as_ambiguous():
    # Moneyline / run line / total on one game share a signature on PM-US; the
    # matcher must drop all of them rather than route onto whichever came first.
    sl = "mlb-bal-tex-2026-08-12"
    pmus = MockPolymarketUSClient({
        sl: PMUSMarket(sl, 0.5, 0.55, 0.5),
        "atc-" + sl: PMUSMarket("atc-" + sl, 0.4, 0.45, 0.4),
    })
    tickets, attempts = build_bridge_tickets(
        [_ticket(slug="mlb-bal-tex-2026-08-12", outcome="Yes")], pmus_client=pmus)
    assert tickets == []
    assert [a["status"] for a in attempts] == ["ambiguous"]


# --- Kalshi adapter --------------------------------------------------------------

def _kx_fetch_factory(markets, calls=None):
    def fetch(url, params):
        if calls is not None:
            calls.append(params)
        return {"markets": [m for m in markets
                            if m["ticker"].rsplit("-", 1)[0] == params["event_ticker"]]}
    return fetch


def test_kalshi_match_records_at_the_ask():
    fetch = _kx_fetch_factory([
        {"ticker": "KXMLBGAME-26AUG12BALTEX-TEX",
         "title": "Texas to win", "yes_ask": 57},
    ])
    tickets, attempts = build_bridge_tickets(
        [_ticket(slug="mlb-bal-tex-2026-08-12-tex", title="Rangers ML")],
        kalshi_fetch=fetch)
    assert [a["status"] for a in attempts if a["venue"] == "kalshi"] == ["matched"]
    [t] = tickets
    assert t["market_id"] == "KXMLBGAME-26AUG12BALTEX-TEX"
    assert t["venue"] == "kalshi"
    assert t["source"] == "whale-kalshi"
    assert t["outcome"] == "Yes"             # the ticker names the team; we hold YES
    assert t["entry_price"] == 0.57          # cents converted to dollars
    assert t["_maker"] is False


def test_kalshi_no_market_vs_lookup_failed():
    ok_but_empty = _kx_fetch_factory([])
    _, attempts = build_bridge_tickets(
        [_ticket(slug="mlb-bal-tex-2026-08-12-tex")], kalshi_fetch=ok_but_empty)
    assert [a["status"] for a in attempts] == ["no-market"]

    def broken(url, params):
        raise OSError("tls reset")
    _, attempts = build_bridge_tickets(
        [_ticket(slug="mlb-bal-tex-2026-08-12-tex")], kalshi_fetch=broken)
    assert [a["status"] for a in attempts] == ["lookup-failed"]


def test_skip_set_prevents_refetch():
    calls = []
    fetch = _kx_fetch_factory([], calls)
    okey = "0x" + "ab" * 32 + ":yes"
    build_bridge_tickets([_ticket(slug="mlb-bal-tex-2026-08-12-tex")],
                         kalshi_fetch=fetch, skip={okey + ":kalshi"})
    assert calls == []


# --- record() integration: taker fees, dedup, settlement -------------------------

def test_bridge_rows_book_taker_fees_despite_maker_default():
    trial = {"open": [], "settled": []}
    pmus = MockPolymarketUSClient({
        "atc-fwc-ecu-ger-2026-06-25-ger": PMUSMarket(
            "atc-fwc-ecu-ger-2026-06-25-ger", 0.50, 0.55, 0.52),
    })
    tickets, _ = build_bridge_tickets([_ticket()], pmus_client=pmus)
    assert record(trial, tickets, maker=True) == 1
    row = trial["open"][0]
    contracts = max(1, round(row["contracts"]))
    assert row["fee"] == trade_fee(0.55, contracts, multiplier=0.07, maker=False)
    assert row["fee"] > trade_fee(0.55, contracts, multiplier=0.07, maker=True)
    # Re-record is a no-op: one position per venue leg, first sighting wins.
    assert record(trial, tickets, maker=True) == 0


def test_pmus_row_settles_via_origin_meta():
    trial = {"open": [], "settled": []}
    pmus = MockPolymarketUSClient({
        "atc-fwc-ecu-ger-2026-06-25-ger": PMUSMarket(
            "atc-fwc-ecu-ger-2026-06-25-ger", 0.50, 0.55, 0.52),
    })
    tickets, _ = build_bridge_tickets([_ticket()], pmus_client=pmus)
    record(trial, tickets)
    row = trial["open"][0]
    # main() keys the origin's Gamma meta under the PM-US row's own market_id.
    metas = {row["market_id"]: {"closed": True,
                                "outcomes": ["Yes", "No"], "prices": [1.0, 0.0]}}
    assert settle(trial, metas) == 1
    done = trial["settled"][0]
    assert done["won"] is True
    assert done["source"] == "whale-pmus"


# --- coverage log ----------------------------------------------------------------

def test_update_log_upgrades_transients_but_never_downgrades_matched():
    trial = {}
    okey = "0xabc:yes"
    update_log(trial, [{"origin_key": okey, "venue": "kalshi",
                        "status": "lookup-failed"}], now=1000.0)
    assert trial["bridge"]["summary"]["kalshi"] == {"lookup-failed": 1}
    # A later run succeeds: the transient upgrades.
    update_log(trial, [{"origin_key": okey, "venue": "kalshi",
                        "status": "matched"}], now=2000.0)
    assert trial["bridge"]["summary"]["kalshi"] == {"matched": 1}
    # Matched is final: a later transient failure must not erase the record.
    update_log(trial, [{"origin_key": okey, "venue": "kalshi",
                        "status": "lookup-failed"}], now=3000.0)
    assert trial["bridge"]["log"][okey]["kalshi"] == "matched"
    assert permanent_skips(trial) == {okey + ":kalshi"}


def test_permanent_skips_hold_structural_refusals_and_retry_transients():
    trial = {}
    update_log(trial, [
        {"origin_key": "a:yes", "venue": "kalshi", "status": "no-series"},
        {"origin_key": "b:yes", "venue": "kalshi", "status": "no-market"},
        {"origin_key": "b:yes", "venue": "polymarket-us", "status": "no-counterpart"},
    ], now=1.0)
    # Structural refusal is skipped forever; venue listings can appear, so retry those.
    assert permanent_skips(trial) == {"a:yes:kalshi"}
