"""The filters here encode what the 2026-06-26 live session cost us - keep them honest."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from predictionedge.config import Config
from predictionedge.sizing import Account
from predictionedge.polymarket import MockPolymarketDataClient, Trade
from predictionedge.signals import build_board
from predictionedge.whales import SmartWalletScorer

NOW = 1_700_000_000.0
NOW_DT = datetime.fromtimestamp(NOW, tz=timezone.utc)


def _iso(**delta):
    return (NOW_DT + timedelta(**delta)).isoformat().replace("+00:00", "Z")


def _trade(wallet, cid, outcome, size, price, *, minutes_ago=2.0, title="Some market",
           side="BUY"):
    return Trade(wallet=wallet, name="", side=side, size=size, price=price,
                 ts=int(NOW - minutes_ago * 60), title=title, outcome=outcome,
                 condition_id=cid, event_slug=f"evt-{cid}", slug=f"mkt-{cid}")


def _meta(**over):
    base = {"question": "Some market", "slug": "mkt", "end_date": _iso(days=5),
            "game_start": "", "closed": False, "active": True, "volume": 500_000.0,
            "liquidity": 80_000.0, "best_bid": 0.39, "best_ask": 0.41,
            "yes_price": 0.40, "outcomes": ["Yes", "No"], "prices": [0.40, 0.60]}
    base.update(over)
    return base


def _cfg(**over):
    cfg = Config(copytrade_min_usd=10_000, copytrade_min_wallets=1,
                 board_min_conviction=0.0)
    return replace(cfg, **over) if over else cfg


class _Profiler:
    """Canned wallet facts. A wallet absent from a map is 'unknown', not 'zero'."""

    def __init__(self, bankroll_map=None, boom=False, first_seen_map=None):
        self._map = bankroll_map or {}
        self._first = first_seen_map or {}
        self._boom = boom
        self.asked = []
        self.aged = []

    def bankrolls(self, addresses):
        self.asked.append(list(addresses))
        if self._boom:
            raise RuntimeError("data api down")
        return {a: v for a, v in self._map.items() if a in set(addresses)}

    def first_seen(self, addresses):
        self.aged.append(list(addresses))
        if self._boom:
            raise RuntimeError("data api down")
        return {a: v for a, v in self._first.items() if a in set(addresses)}


def _board(trades, metas, cfg=None, account=None, profiler=None):
    client = MockPolymarketDataClient(trades=trades)
    return build_board(cfg or _cfg(), client, SmartWalletScorer(),
                       account or Account(size=100_000),
                       now_ts=NOW, meta_fetch=lambda ids: metas, profiler=profiler)


def test_happy_path_produces_a_sized_ticket():
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40),
              _trade("0xSHARP2", "A", "Yes", 40_000, 0.40)]
    r = _board(trades, {"A": _meta()})
    assert len(r.tickets) == 1
    t = r.tickets[0]
    assert t.entry_price == 0.41 and t.contracts > 0 and t.cost > 0
    assert t.n_wallets == 2
    assert t.why and any("wallet" in w for w in t.why)


def test_in_play_market_is_rejected():
    """The June batch: whales copied mid-match, we filled at the post-goal price."""
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40)]
    r = _board(trades, {"A": _meta(game_start=_iso(minutes=-45), end_date=_iso(hours=2))})
    assert r.tickets == []
    assert any("in-play" in k for k in r.rejected)


def test_market_resolving_too_soon_is_rejected():
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40)]
    r = _board(trades, {"A": _meta(end_date=_iso(hours=1))})
    assert r.tickets == []
    assert any("under" in k for k in r.rejected)


def test_same_day_pregame_market_is_allowed():
    """Informed money on sports arrives late; 4h out is a normal, wanted ticket."""
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40)]
    r = _board(trades, {"A": _meta(end_date=_iso(hours=4),
                                   game_start=_iso(hours=1))})
    assert len(r.tickets) == 1


def test_sports_market_with_no_kickoff_is_rejected():
    """We cannot tell whether the ball is already in the air, so we pass.

    Counted under its own reason: the old code reached this state by falling back to
    `startDate` (market CREATION, always past) and reported it as "in-play", which is
    both false and the wrong hypothesis to retire.
    """
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40, title="Alcaraz vs Sinner")]
    r = _board(trades, {"A": _meta(question="Alcaraz vs Sinner", game_start="")})
    assert r.tickets == []
    assert any("no event start time" in k for k in r.rejected)
    assert not any("in-play" in k for k in r.rejected)


def test_non_sports_market_with_no_kickoff_is_fine():
    """Politics, crypto, econ: nothing kicks off, so a missing time means nothing.

    This is the half the old `or startDate` fallback deleted from the board entirely -
    every such market read as already started.
    """
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40,
                     title="Fed cuts rates in September")]
    r = _board(trades, {"A": _meta(question="Fed cuts rates in September",
                                   game_start="")})
    assert len(r.tickets) == 1


def test_no_dates_at_all_is_rejected():
    """Not knowing when something resolves is a reason to pass, not to proceed."""
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40)]
    r = _board(trades, {"A": _meta(end_date="", game_start="")})
    assert r.tickets == []
    assert any("no resolution or start time" in k for k in r.rejected)


def test_staleness_is_measured_against_kickoff_not_the_padded_deadline():
    """The measured failure: a 7-day `endDate` on a game being played tonight.

    At 165.9h the allowance became 1,991 minutes, so a 33h-old whale fill passed. The
    same ticket judged against kickoff gets minutes.
    """
    old = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40, minutes_ago=1_980,
                  title="Mets vs Pirates")]
    padded = {"A": _meta(question="Mets vs Pirates",
                         end_date=_iso(days=7), game_start=_iso(hours=4))}
    r = _board(old, padded)
    assert r.tickets == []
    assert any("too old" in k for k in r.rejected)


def test_published_horizon_is_the_event_not_the_deadline():
    """174.6h on a same-night ball game is the number that has to stop appearing."""
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40, title="Mets vs Pirates")]
    r = _board(trades, {"A": _meta(question="Mets vs Pirates",
                                   end_date=_iso(days=7), game_start=_iso(hours=4))})
    t = r.tickets[0]
    assert abs(t.hours_to_resolve - 4.0) < 0.1
    assert t.event_iso and t.end_iso and t.event_iso != t.end_iso


def test_min_hours_still_measures_time_to_resolution():
    """`board_min_hours` is not the in-play gate - conflating them is the old bug.

    A game kicking off in 1h that settles in 4h is a normal pre-game ticket.
    """
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40, title="Mets vs Pirates")]
    r = _board(trades, {"A": _meta(question="Mets vs Pirates",
                                   end_date=_iso(hours=4), game_start=_iso(hours=1))})
    assert len(r.tickets) == 1


def test_same_day_but_already_started_is_still_rejected():
    """Loosening the horizon must not reopen the in-play trap."""
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40)]
    r = _board(trades, {"A": _meta(end_date=_iso(hours=4),
                                   game_start=_iso(minutes=-20))})
    assert r.tickets == []
    assert any("in-play" in k for k in r.rejected)


def test_price_that_ran_away_is_rejected():
    """Whale filled at 0.40; it is 0.55 now - that is buying their exhaust."""
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40)]
    r = _board(trades, {"A": _meta(best_ask=0.55, best_bid=0.54)})
    assert r.tickets == []
    assert any("ran away" in k for k in r.rejected)


def test_stale_signal_is_rejected():
    # 30h horizon -> allowance is the 120min floor; a 10h-old fill is far past it.
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40, minutes_ago=600)]
    r = _board(trades, {"A": _meta(end_date=_iso(hours=30))})
    assert r.tickets == []
    assert any("too old" in k for k in r.rejected)


def test_staleness_allowance_scales_with_horizon():
    """The same 10h-old fill is fine on a market that still has ~5 days to run."""
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40, minutes_ago=600)]
    r = _board(trades, {"A": _meta(end_date=_iso(days=5))})
    assert len(r.tickets) == 1


def test_far_dated_market_is_rejected():
    """No edge on something that resolves next year - the user's own rule."""
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40)]
    r = _board(trades, {"A": _meta(end_date=_iso(days=300))})
    assert r.tickets == []
    assert any("days out" in k for k in r.rejected)


def test_closed_market_is_rejected():
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40)]
    r = _board(trades, {"A": _meta(closed=True)})
    assert r.tickets == []


def test_one_ticket_per_event_keeps_the_stronger_side():
    """Two markets on one game is a single opinion, not two bets."""
    strong = [_trade("0xSHARP1", "A", "Yes", 90_000, 0.40, title="Türkiye vs USA"),
              _trade("0xSHARP2", "A", "Yes", 90_000, 0.40, title="Türkiye vs USA")]
    # Same event page as A, so it must collapse into the stronger leg.
    weak = Trade(wallet="0xSHARP1", name="", side="BUY", size=20_000, price=0.40,
                 ts=int(NOW - 120), title="Türkiye vs USA", outcome="Yes",
                 condition_id="B", event_slug="evt-A", slug="mkt-B")
    kick = _iso(hours=6)
    metas = {"A": _meta(question="Türkiye vs USA", game_start=kick),
             "B": _meta(question="Türkiye vs USA", game_start=kick)}
    r = _board([*strong, weak], metas)
    assert len(r.tickets) == 1
    assert r.tickets[0].n_wallets == 2          # kept the two-wallet side
    assert any("duplicate event" in k for k in r.rejected)


def test_side_label_spells_out_the_team():
    trades = [_trade("0xSHARP1", "A", "Türkiye", 60_000, 0.40, title="Türkiye vs USA")]
    r = _board(trades, {"A": _meta(question="Türkiye vs USA", game_start=_iso(hours=6),
                                   outcomes=["Türkiye", "USA"], prices=[0.41, 0.59])})
    assert r.tickets[0].side_label.startswith("Türkiye")
    assert r.tickets[0].entry_price == 0.41      # the team's price, not the YES book


def test_named_outcome_is_priced_from_its_own_leg():
    """A team's price is not the YES price - quoting the wrong leg invents fake drift."""
    trades = [_trade("0xSHARP1", "A", "Man City", 60_000, 0.70, title="EPL winner")]
    meta = _meta(question="EPL winner", best_bid=0.28, best_ask=0.29,
                 game_start=_iso(hours=6),
                 outcomes=["Man City", "Arsenal"], prices=[0.71, 0.29])
    r = _board(trades, {"A": meta})
    assert r.tickets[0].entry_price == 0.71
    assert abs(r.tickets[0].drift_c) < 2         # ~flat, not the bogus -41c


def test_unpriceable_outcome_is_rejected_not_guessed():
    trades = [_trade("0xSHARP1", "A", "Some Team", 60_000, 0.40)]
    meta = _meta(outcomes=["Other", "Team"], prices=[0.4, 0.6], best_ask=0.0,
                 best_bid=0.0, yes_price=0.0)
    r = _board(trades, {"A": meta})
    assert r.tickets == []
    assert any("could not price" in k for k in r.rejected)


def test_yes_no_label_includes_the_question():
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40)]
    r = _board(trades, {"A": _meta(question="Will X happen?")})
    assert r.tickets[0].side_label == "YES on: Will X happen?"


def test_the_buy_bar_keeps_weak_ideas_off_the_board():
    trades = [_trade("0xSHARP1", "A", "Yes", 10_000, 0.40, minutes_ago=110)]
    r = _board(trades, {"A": _meta()}, cfg=_cfg(board_min_conviction=0.95))
    assert r.tickets == []
    # Not bought - but still recorded, because a threshold that only ever observes
    # outcomes above itself can never be shown to be in the wrong place.
    assert len(r.probe) == 1
    assert any("buy bar" in k for k in r.rejected)


def test_below_the_probe_floor_is_dropped_outright():
    trades = [_trade("0xSHARP1", "A", "Yes", 10_000, 0.40, minutes_ago=110)]
    r = _board(trades, {"A": _meta()},
               cfg=_cfg(board_min_conviction=0.95, board_probe_min_conviction=0.95))
    assert r.tickets == [] and r.probe == []
    assert r.rejected["conviction below threshold"] == 1


def test_report_counts_everything_considered():
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40),
              _trade("0xSHARP2", "B", "Yes", 60_000, 0.40)]
    r = _board(trades, {"A": _meta(), "B": _meta(closed=True)})
    assert r.considered == 2
    assert len(r.tickets) == 1


def test_no_signals_yields_a_note_not_a_crash():
    r = _board([], {})
    assert r.tickets == [] and r.notes


def test_missing_metadata_fails_closed():
    """No metadata means no date checks are possible, so it must reject, not pass."""
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40)]
    r = _board(trades, {})          # metadata lookup returned nothing for this market
    assert r.tickets == []
    assert any("no market metadata" in k for k in r.rejected)


def test_parlay_tickets_are_named_not_filed_as_missing_metadata():
    """Combo tickets ride the same feed with a synthetic id and no market behind them.

    Gamma has no such market because there is no such market, so these read as a
    lookup problem. Copying one would be wrong regardless: the legs are correlated,
    and no drift or resolution filter here means anything applied to a basket.
    """
    combo = "0x" + "ab" * 31          # 62 hex, the shape the feed actually emits
    trades = [_trade("0xSHARP1", combo, "Yes", 60_000, 0.40)]
    r = _board(trades, {combo: _meta()})
    assert r.tickets == []
    assert any("parlay" in k for k in r.rejected)
    assert not any("metadata" in k for k in r.rejected)


def test_unread_metadata_is_distinguished_from_absent_metadata():
    """A market we could not READ about is not a market that does not exist.

    Both lose the ticket, but only one is a bug to go and fix. Filing them under one
    rejection reason made a network leak look like the filter working as designed.
    """
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40)]

    def fetch(ids, failures=None):
        if failures is not None:
            failures.update(ids)
        return {}

    client = MockPolymarketDataClient(trades=trades)
    r = build_board(_cfg(), client, SmartWalletScorer(), Account(size=100_000),
                    now_ts=NOW, meta_fetch=fetch)
    assert r.tickets == []
    assert any("lookup failed" in k for k in r.rejected)
    assert not any(k == "no market metadata" for k in r.rejected)


def test_thin_liquidity_raises_a_warning():
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40)]
    r = _board(trades, {"A": _meta(liquidity=2_000.0)})
    assert any("thin liquidity" in w for w in r.tickets[0].warnings)


# --- E4.1 bankroll fraction, not dollar size ---------------------------------
# $50k means two different things depending on who bet it. These pin that the board
# now knows the difference, and - just as important - that it never pretends to know.

def test_same_dollars_from_a_bigger_wallet_scores_lower():
    """The whole claim in one test: $60k is an opinion at $120k, noise at $12M."""
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40)]
    committed = _board(trades, {"A": _meta()}, profiler=_Profiler({"0xSHARP1": 120_000}))
    casual = _board(trades, {"A": _meta()}, profiler=_Profiler({"0xSHARP1": 12_000_000}))
    assert casual.tickets[0].conviction < committed.tickets[0].conviction


def test_bankroll_fraction_changes_position_size():
    """Conviction feeds the sizer, so this has to move real money, not just the rank."""
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40)]
    committed = _board(trades, {"A": _meta()}, profiler=_Profiler({"0xSHARP1": 120_000}))
    casual = _board(trades, {"A": _meta()}, profiler=_Profiler({"0xSHARP1": 12_000_000}))
    assert casual.tickets[0].contracts < committed.tickets[0].contracts


def test_unknown_bankroll_leaves_conviction_exactly_as_before():
    """Fail closed: no data must land on the old behaviour, not a guessed fraction."""
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40)]
    unknown = _board(trades, {"A": _meta()}, profiler=_Profiler({}))
    off = _board(trades, {"A": _meta()}, cfg=_cfg(whale_bankroll_weighting=False))
    assert unknown.tickets[0].conviction == off.tickets[0].conviction


def test_bankroll_weighting_can_only_cut_never_boost():
    """Idle USDC is invisible to the API, so fractions read high. That must not pay."""
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40)]
    all_in = _board(trades, {"A": _meta()}, profiler=_Profiler({"0xSHARP1": 60_000}))
    unknown = _board(trades, {"A": _meta()}, profiler=_Profiler({}))
    assert all_in.tickets[0].conviction == unknown.tickets[0].conviction


def test_flag_off_disables_bankroll_weighting_entirely():
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40)]
    prof = _Profiler({"0xSHARP1": 12_000_000})
    r = _board(trades, {"A": _meta()}, cfg=_cfg(whale_bankroll_weighting=False),
               profiler=prof)
    plain = _board(trades, {"A": _meta()}, cfg=_cfg(whale_bankroll_weighting=False))
    assert r.tickets[0].conviction == plain.tickets[0].conviction
    assert prof.asked == []          # and it does not even spend the API calls


def test_bankroll_outage_degrades_to_dollar_sizing_with_a_note():
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40)]
    r = _board(trades, {"A": _meta()}, profiler=_Profiler({}, boom=True))
    assert len(r.tickets) == 1       # a wallet-data outage must not empty the board
    assert any("bankroll" in n for n in r.notes)


def test_wallets_are_priced_individually_not_pooled():
    """Pooling lets a $10M wallet's small change drown out a $100k wallet going all in."""
    from predictionedge.signals import _bankroll_fraction

    wallet_usd = (("0xBIG", 50_000.0), ("0xSMALL", 50_000.0))
    banks = {"0xBIG": 10_000_000.0, "0xSMALL": 100_000.0}
    frac = _bankroll_fraction(wallet_usd, banks)
    pooled = 100_000.0 / 10_100_000.0
    assert frac > pooled * 10        # the committed wallet still registers
    assert 0.24 < frac < 0.26        # notional-weighted mean of 0.5% and 50%


# --- E4.3 fresh-wallet concentration ------------------------------------------
# A brand-new wallet funding up and immediately taking a large one-sided position in a
# thin market. The leaderboard cannot rank a wallet with no history, so this pattern is
# invisible to every other signal in the repo - it is found by shape, not reputation.

_DAY = 86_400.0


def _fresh_cfg(**over):
    return _cfg(whale_fresh_min_usd=5_000.0, whale_fresh_max_age_days=30.0,
                whale_fresh_max_liquidity=250_000.0, **over)


def _thin(**over):
    base = {"liquidity": 40_000.0}
    base.update(over)
    return _meta(**base)


def test_fresh_wallet_produces_a_ticket_no_other_signal_could_find():
    trades = [_trade("0xNEWBIE", "A", "Yes", 30_000, 0.40)]
    prof = _Profiler(first_seen_map={"0xNEWBIE": NOW - 3 * _DAY})
    r = _board(trades, {"A": _thin()}, cfg=_fresh_cfg(), profiler=prof)
    assert len(r.tickets) == 1
    assert r.tickets[0].n_wallets == 1
    # It must never be dressed up as a proven wallet.
    assert not any("profitable" in w for w in r.tickets[0].why)
    assert any("no track record" in w for w in r.tickets[0].why)
    assert any("unproven wallet" in w for w in r.tickets[0].warnings)


def test_old_wallet_without_a_track_record_is_still_nothing():
    """Most unranked wallets are just unranked. Only NEW ones carry the pattern."""
    trades = [_trade("0xNEWBIE", "A", "Yes", 30_000, 0.40)]
    prof = _Profiler(first_seen_map={"0xNEWBIE": NOW - 400 * _DAY})
    r = _board(trades, {"A": _thin()}, cfg=_fresh_cfg(), profiler=prof)
    assert r.tickets == []
    assert any("not new" in k for k in r.rejected)


def test_unknown_wallet_age_rejects_rather_than_assumes_new():
    """Fail closed: an unreadable history must not make every address look brand new."""
    trades = [_trade("0xNEWBIE", "A", "Yes", 30_000, 0.40)]
    r = _board(trades, {"A": _thin()}, cfg=_fresh_cfg(), profiler=_Profiler())
    assert r.tickets == []
    assert any("age unavailable" in k for k in r.rejected)


def test_fresh_wallet_needs_a_profiler_at_all():
    trades = [_trade("0xNEWBIE", "A", "Yes", 30_000, 0.40)]
    r = _board(trades, {"A": _thin()}, cfg=_fresh_cfg(), profiler=None)
    assert r.tickets == []
    assert any("age unavailable" in k for k in r.rejected)


def test_fresh_pattern_needs_a_thin_market():
    """Informed money moving an illiquid market is the claim; depth kills it."""
    trades = [_trade("0xNEWBIE", "A", "Yes", 30_000, 0.40)]
    prof = _Profiler(first_seen_map={"0xNEWBIE": NOW - 3 * _DAY})
    r = _board(trades, {"A": _thin(liquidity=5_000_000.0)}, cfg=_fresh_cfg(), profiler=prof)
    assert r.tickets == []
    assert any("too liquid" in k for k in r.rejected)


def test_unknown_market_depth_rejects_the_fresh_pattern():
    trades = [_trade("0xNEWBIE", "A", "Yes", 30_000, 0.40)]
    prof = _Profiler(first_seen_map={"0xNEWBIE": NOW - 3 * _DAY})
    r = _board(trades, {"A": _thin(liquidity=0.0)}, cfg=_fresh_cfg(), profiler=prof)
    assert r.tickets == []
    assert any("depth unknown" in k for k in r.rejected)


def test_age_is_not_looked_up_for_markets_that_already_failed():
    """The age call is the expensive one, so it goes last, after the free gates."""
    trades = [_trade("0xNEWBIE", "A", "Yes", 30_000, 0.40)]
    prof = _Profiler(first_seen_map={"0xNEWBIE": NOW - 3 * _DAY})
    _board(trades, {"A": _thin(liquidity=5_000_000.0)}, cfg=_fresh_cfg(), profiler=prof)
    assert prof.aged == []


def test_fresh_wallet_still_obeys_the_june_filters():
    """New signal source, same hard-won filters - in-play is in-play for anyone."""
    trades = [_trade("0xNEWBIE", "A", "Yes", 30_000, 0.40)]
    prof = _Profiler(first_seen_map={"0xNEWBIE": NOW - 3 * _DAY})
    r = _board(trades, {"A": _thin(game_start=_iso(minutes=-45), end_date=_iso(hours=2))},
               cfg=_fresh_cfg(), profiler=prof)
    assert r.tickets == []
    assert any("in-play" in k for k in r.rejected)


def test_fresh_flag_off_finds_nothing_new():
    trades = [_trade("0xNEWBIE", "A", "Yes", 30_000, 0.40)]
    prof = _Profiler(first_seen_map={"0xNEWBIE": NOW - 3 * _DAY})
    r = _board(trades, {"A": _thin()}, cfg=_fresh_cfg(whale_fresh_enabled=False),
               profiler=prof)
    assert r.tickets == []


def test_proven_wallets_are_not_rerouted_through_the_fresh_path():
    """A leaderboard wallet is a copy signal, and must stay labelled as one."""
    trades = [_trade("0xSHARP1", "A", "Yes", 30_000, 0.40)]
    prof = _Profiler(first_seen_map={"0xSHARP1": NOW - 3 * _DAY})
    r = _board(trades, {"A": _thin()}, cfg=_fresh_cfg(), profiler=prof)
    assert any("profitable wallet" in w for w in r.tickets[0].why)


# --- E4.2 wallet exits on the board ------------------------------------------

def test_smart_exit_warns_on_the_ticket_it_contradicts():
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40, minutes_ago=90),
              _trade("0xSHARP2", "A", "Yes", 25_000, 0.44, minutes_ago=10, side="SELL")]
    r = _board(trades, {"A": _meta()})
    assert len(r.tickets) == 1               # still advice, with the caveat attached
    assert any("sold $25,000" in w for w in r.tickets[0].warnings)


def test_exit_before_the_buys_is_not_called_an_exit():
    """A wallet that trimmed and then bought more is scaling in, not leaving."""
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40, minutes_ago=10),
              _trade("0xSHARP2", "A", "Yes", 25_000, 0.44, minutes_ago=90, side="SELL")]
    r = _board(trades, {"A": _meta()})
    assert not any("sold" in w for w in r.tickets[0].warnings)


def test_exit_on_a_different_outcome_does_not_warn():
    """Selling another leg is not selling this one - on a 3-way it means nothing."""
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40, minutes_ago=90),
              _trade("0xSHARP2", "A", "No", 25_000, 0.56, minutes_ago=10, side="SELL")]
    r = _board(trades, {"A": _meta()})
    assert not any("sold" in w for w in r.tickets[0].warnings)


def test_exit_on_a_different_market_does_not_warn():
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40, minutes_ago=90),
              _trade("0xSHARP2", "B", "Yes", 25_000, 0.44, minutes_ago=10, side="SELL")]
    r = _board(trades, {"A": _meta(), "B": _meta()})
    assert not any("sold" in w for w in r.tickets[0].warnings)


def test_exit_tracking_flag_off_silences_the_warning():
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40, minutes_ago=90),
              _trade("0xSHARP2", "A", "Yes", 25_000, 0.44, minutes_ago=10, side="SELL")]
    r = _board(trades, {"A": _meta()}, cfg=_cfg(whale_track_exits=False))
    assert not any("sold" in w for w in r.tickets[0].warnings)


def test_exit_is_advisory_and_never_changes_the_size():
    """Warnings inform the human; they must not silently re-size the position."""
    buys = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40, minutes_ago=90)]
    sell = _trade("0xSHARP2", "A", "Yes", 25_000, 0.44, minutes_ago=10, side="SELL")
    plain = _board(buys, {"A": _meta()})
    warned = _board([*buys, sell], {"A": _meta()})
    assert warned.tickets[0].conviction == plain.tickets[0].conviction
    assert warned.tickets[0].contracts == plain.tickets[0].contracts


def test_wallet_with_no_bankroll_data_is_skipped_not_assumed():
    from predictionedge.signals import _bankroll_fraction

    assert _bankroll_fraction((("0xA", 50_000.0),), {}) is None
    assert _bankroll_fraction((("0xA", 50_000.0),), {"0xA": 0.0}) is None
    # A known wallet still counts when its partner is unknown - and the unknown one
    # neither dilutes nor inflates it.
    frac = _bankroll_fraction((("0xA", 50_000.0), ("0xB", 50_000.0)), {"0xA": 100_000.0})
    assert frac == 0.5


# --- Election concentration inversion -------------------------------------------
# Everywhere else in this file, size from few wallets raises conviction. In an election
# market it lowers it: one trader held 2024 pricing away from the polls for weeks, and
# copying that buys the distortion rather than the information. Same input, opposite
# sign, exactly one category. Sizes stay above copytrade_min_usd so the split under
# test is the only thing that varies.

_ELECTION_TITLE = "Will Smith win the 2028 presidential election?"


def _election_meta(**over):
    base = {"question": _ELECTION_TITLE, "slug": "presidential-election-winner-2028"}
    base.update(over)
    return _meta(**base)


def _election_board(a_usd, b_usd, cfg=None):
    trades = [_trade("0xSHARP1", "A", "Yes", a_usd, 0.40, title=_ELECTION_TITLE),
              _trade("0xSHARP2", "A", "Yes", b_usd, 0.40, title=_ELECTION_TITLE)]
    return _board(trades, {"A": _election_meta()}, cfg=cfg)


def test_one_wallet_dominating_an_election_scores_below_a_split_signal():
    """Same total dollars, same wallet count - only the concentration differs."""
    lopsided = _election_board(90_000, 10_000).tickets[0]
    split = _election_board(50_000, 50_000).tickets[0]
    assert lopsided.conviction < split.conviction


def test_the_same_lopsided_signal_is_untouched_outside_elections():
    """The rule is one category wide - a lopsided tennis signal is scored as before."""
    trades = [_trade("0xSHARP1", "A", "Yes", 90_000, 0.40, title="Alcaraz vs Sinner"),
              _trade("0xSHARP2", "A", "Yes", 10_000, 0.40, title="Alcaraz vs Sinner")]
    metas = {"A": _meta(question="Alcaraz vs Sinner", game_start=_iso(hours=6))}
    on = _board(trades, metas).tickets[0]
    off = _board(trades, metas,
                 cfg=_cfg(whale_election_concentration_penalty=False)).tickets[0]
    assert on.conviction == off.conviction


def test_penalty_can_only_cut_never_boost():
    """A well-split election signal scores exactly as it would with the rule off."""
    on = _election_board(50_000, 50_000).tickets[0].conviction
    off = _election_board(50_000, 50_000,
                          cfg=_cfg(whale_election_concentration_penalty=False)
                          ).tickets[0].conviction
    assert on == off


def test_flag_off_disables_the_election_rule_entirely():
    on = _election_board(90_000, 10_000).tickets[0]
    off = _election_board(90_000, 10_000,
                          cfg=_cfg(whale_election_concentration_penalty=False)).tickets[0]
    assert on.conviction < off.conviction
    assert not any("single wallet" in w for w in off.warnings)


def test_dominated_election_ticket_warns_in_words():
    """A number that quietly moved is worse than useless - it has to say why."""
    t = _election_board(90_000, 10_000).tickets[0]
    assert any("single wallet" in w for w in t.warnings)
    assert any("election market" in w for w in t.why)


def test_unreadable_wallet_breakdown_applies_no_penalty():
    """Unknowable concentration must not read as 'one wallet did all of it'."""
    from predictionedge.signals import _concentration
    assert _concentration(()) is None
    assert _concentration((("0xA", 0.0),)) is None


# Only 0xSHARP1/0xSHARP2 are on the mock leaderboard; anyone else reads as an unknown
# wallet and lands in the fresh-wallet branch, which is a different hypothesis entirely.
def _many(n_markets, size=60_000):
    trades, metas = [], {}
    for i in range(n_markets):
        cid = f"M{i}"
        trades.append(_trade("0xSHARP1", cid, "Yes", size, 0.40))
        trades.append(_trade("0xSHARP2", cid, "Yes", size, 0.40))
        metas[cid] = _meta()
    return trades, metas

def test_a_quota_no_longer_decides_what_gets_bought():
    # The cap is off by default. Five equally strong tickets are five tickets, not the
    # top three of five - "how many fit on the page" was never a fact about the trade.
    trades, metas = _many(5)
    r = _board(trades, metas, cfg=_cfg(board_min_conviction=0.0))
    assert len(r.tickets) == 5
    assert r.probe == []
    assert r.considered - sum(r.rejected.values()) == len(r.tickets)


def test_the_optional_cap_is_counted_not_swallowed():
    # The valve still exists for capping exposure on a loud day - but every ticket it
    # cuts gets a ledger entry. Silent truncation is what made `considered` minus the
    # rejections fail to equal the published count on the 2026-08-07 board.
    trades, metas = _many(5)
    r = _board(trades, metas, cfg=_cfg(board_min_conviction=0.0, board_max_tickets=3))

    assert len(r.tickets) == 3
    assert len(r.probe) == 2
    assert r.rejected["over the 3-ticket cap"] == 2
    assert r.considered - sum(r.rejected.values()) == len(r.tickets)


def test_the_bar_splits_bought_from_merely_recorded():
    trades, metas = [], {}
    for i, size in enumerate((120_000, 60_000, 30_000)):
        cid = f"M{i}"
        trades.append(_trade("0xSHARP1", cid, "Yes", size, 0.40))
        trades.append(_trade("0xSHARP2", cid, "Yes", size, 0.40))
        metas[cid] = _meta()
    ranked = sorted((t.conviction for t in
                     _board(trades, metas, cfg=_cfg(board_min_conviction=0.0)).tickets),
                    reverse=True)
    bar = (ranked[0] + ranked[1]) / 2       # cut between the strongest two

    r = _board(trades, metas, cfg=_cfg(board_min_conviction=bar))
    assert len(r.tickets) == 1
    assert len(r.probe) == 2
    assert min(t.conviction for t in r.tickets) > max(t.conviction for t in r.probe)


# --- esports, cut on measured evidence (see Config.esports_enabled) -----------

def test_an_esports_market_is_rejected_by_name():
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40)]
    r = _board(trades, {"A": _meta(
        question="Counter-Strike: BIG vs G2 (BO3) - Esports World Cup Group A")})
    assert r.tickets == []
    assert any("esports" in k for k in r.rejected)


def test_an_esports_derivative_is_caught_by_the_event_slug():
    """"Game Handicap: DK (-1.5) vs KT Rolster (+1.5)" names no game anywhere in the
    title. Only the event slug says it is a League of Legends match, and these
    derivatives are half the esports rows in the live record."""
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40)]
    trades[0] = replace(trades[0], event_slug="lol-kt-dk-2026-08-12")
    r = _board(trades, {"A": _meta(question="Game Handicap: DK (-1.5) vs KT Rolster (+1.5)")})
    assert r.tickets == []
    assert any("esports" in k for k in r.rejected)


def test_a_leagues_cup_soccer_fixture_is_not_esports():
    """`lec-tig-vwh` is Leagues Cup, not the LoL EMEA Championship. Matching on the
    bare league acronym swept four soccer markets into the esports pile in testing,
    which is why the classifier does not look at LCK/LPL/LEC/LCS at all."""
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40)]
    trades[0] = replace(trades[0], event_slug="lec-tig-vwh-2026-08-11")
    r = _board(trades, {"A": _meta(question="Will Vancouver Whitecaps FC win on 2026-08-11?")})
    assert len(r.tickets) == 1


def test_the_esports_cut_is_a_flag_not_a_hardcoded_skip():
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40)]
    metas = {"A": _meta(question="Counter-Strike: BIG vs G2 (BO3)")}
    assert _board(trades, metas, cfg=_cfg(esports_enabled=True)).tickets != []
