"""The filters here encode what the 2026-06-26 live session cost us - keep them honest."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from predictionedge.config import Config
from predictionedge.omen import OmenAccount
from predictionedge.polymarket import MockPolymarketDataClient, Trade
from predictionedge.signals import build_board
from predictionedge.whales import SmartWalletScorer

NOW = 1_700_000_000.0
NOW_DT = datetime.fromtimestamp(NOW, tz=timezone.utc)


def _iso(**delta):
    return (NOW_DT + timedelta(**delta)).isoformat().replace("+00:00", "Z")


def _trade(wallet, cid, outcome, size, price, *, minutes_ago=2.0, title="Some market"):
    return Trade(wallet=wallet, name="", side="BUY", size=size, price=price,
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


def _board(trades, metas, cfg=None, account=None):
    client = MockPolymarketDataClient(trades=trades)
    return build_board(cfg or _cfg(), client, SmartWalletScorer(),
                       account or OmenAccount(size=100_000),
                       now_ts=NOW, meta_fetch=lambda ids: metas)


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
    metas = {"A": _meta(question="Türkiye vs USA"), "B": _meta(question="Türkiye vs USA")}
    r = _board([*strong, weak], metas)
    assert len(r.tickets) == 1
    assert r.tickets[0].n_wallets == 2          # kept the two-wallet side
    assert any("duplicate event" in k for k in r.rejected)


def test_side_label_spells_out_the_team():
    trades = [_trade("0xSHARP1", "A", "Türkiye", 60_000, 0.40, title="Türkiye vs USA")]
    r = _board(trades, {"A": _meta(question="Türkiye vs USA",
                                   outcomes=["Türkiye", "USA"], prices=[0.41, 0.59])})
    assert r.tickets[0].side_label.startswith("Türkiye")
    assert r.tickets[0].entry_price == 0.41      # the team's price, not the YES book


def test_named_outcome_is_priced_from_its_own_leg():
    """A team's price is not the YES price - quoting the wrong leg invents fake drift."""
    trades = [_trade("0xSHARP1", "A", "Man City", 60_000, 0.70, title="EPL winner")]
    meta = _meta(question="EPL winner", best_bid=0.28, best_ask=0.29,
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


def test_conviction_threshold_filters_weak_ideas():
    trades = [_trade("0xSHARP1", "A", "Yes", 10_000, 0.40, minutes_ago=110)]
    r = _board(trades, {"A": _meta()}, cfg=_cfg(board_min_conviction=0.95))
    assert r.tickets == []
    assert any("conviction" in k for k in r.rejected)


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


def test_thin_liquidity_raises_a_warning():
    trades = [_trade("0xSHARP1", "A", "Yes", 60_000, 0.40)]
    r = _board(trades, {"A": _meta(liquidity=2_000.0)})
    assert any("thin liquidity" in w for w in r.tickets[0].warnings)
