from datetime import datetime, timedelta, timezone

from predictionedge.maker import (
    BookTop,
    MakerConfig,
    MakerEngine,
    MockMakerVenue,
    QuotePlan,
    RestingOrder,
    desired_quotes,
    diff_quotes,
    should_pull,
    spread_clears_fees,
)

NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)


def test_spread_must_clear_maker_fee():
    cfg = MakerConfig(half_spread_cents=3, min_edge_after_fees=0.005)
    # fee_per_contract(0.30, maker=True) = 0.25 * 0.07*0.3*0.7 ~= 0.00368
    assert spread_clears_fees(cfg, 0.30) is True
    # Near 50c the fee peaks (~0.00438); demanding a 1c edge on top of a 1c
    # half-spread can't clear it (0.01 - 0.00438 ~= 0.0056 < 0.01).
    tight = MakerConfig(half_spread_cents=1, min_edge_after_fees=0.01)
    assert spread_clears_fees(tight, 0.50) is False


def test_desired_quotes_centers_on_mid_when_flat():
    cfg = MakerConfig(half_spread_cents=3)
    book = BookTop("KXHIGH-TEST", 0.28, 0.34, NOW)  # mid = 0.31
    plan = desired_quotes(cfg, book, inventory=0)
    assert plan is not None
    assert plan.yes_price == 0.28
    assert plan.no_price == 0.66  # 1 - (0.31 + 0.03)


def test_desired_quotes_skews_against_inventory():
    cfg = MakerConfig(half_spread_cents=3, inventory_skew_cents_per_100=1)
    book = BookTop("KXHIGH-TEST", 0.28, 0.34, NOW)
    long_plan = desired_quotes(cfg, book, inventory=200)  # long 200 YES
    flat_plan = desired_quotes(cfg, book, inventory=0)
    # Long inventory should shade the quote down (encourage selling YES / buying NO).
    assert long_plan.yes_price < flat_plan.yes_price


def test_desired_quotes_none_when_edge_too_thin():
    cfg = MakerConfig(half_spread_cents=1, min_edge_after_fees=0.01)
    book = BookTop("KXHIGH-TEST", 0.49, 0.51, NOW)  # mid = 0.50, fee peaks here
    assert desired_quotes(cfg, book, inventory=0) is None


def test_desired_quotes_none_on_missing_book_side():
    cfg = MakerConfig()
    book = BookTop("KXHIGH-TEST", None, 0.34, NOW)
    assert desired_quotes(cfg, book, inventory=0) is None


def test_diff_quotes_places_when_nothing_resting():
    cfg = MakerConfig(quote_size=100)
    plan = QuotePlan("T", 0.28, 0.66, 100)
    actions = diff_quotes(cfg, plan, [])
    kinds = {(a.kind, a.side) for a in actions}
    assert ("place", "yes") in kinds
    assert ("place", "no") in kinds


def test_diff_quotes_holds_within_threshold():
    cfg = MakerConfig(requote_threshold_cents=2)
    plan = QuotePlan("T", 0.28, 0.66, 100)
    resting = [
        RestingOrder("o1", "T", "yes", 0.29, 100),  # 1c off, within 2c threshold
        RestingOrder("o2", "T", "no", 0.66, 100),
    ]
    actions = diff_quotes(cfg, plan, resting)
    assert all(a.kind == "hold" for a in actions)


def test_diff_quotes_requotes_past_threshold():
    cfg = MakerConfig(requote_threshold_cents=2)
    plan = QuotePlan("T", 0.28, 0.66, 100)
    resting = [RestingOrder("o1", "T", "yes", 0.24, 100)]  # 4c off
    actions = diff_quotes(cfg, plan, resting)
    kinds = [(a.kind, a.side) for a in actions]
    assert ("cancel", "yes") in kinds
    assert ("place", "yes") in kinds
    # The NO side wasn't resting at all -> a fresh place.
    assert ("place", "no") in kinds


def test_diff_quotes_pulls_everything_when_plan_is_none():
    cfg = MakerConfig()
    resting = [RestingOrder("o1", "T", "yes", 0.28, 100),
               RestingOrder("o2", "T", "no", 0.66, 100)]
    actions = diff_quotes(cfg, None, resting)
    assert all(a.kind == "cancel" for a in actions)
    assert len(actions) == 2


def test_should_pull_on_stale_book():
    cfg = MakerConfig(max_book_staleness_s=30.0)
    stale_book = BookTop("T", 0.3, 0.32, NOW - timedelta(seconds=60))
    reason = should_pull(cfg, stale_book, NOW, None)
    assert reason is not None and "stale" in reason


def test_should_pull_around_scheduled_event():
    cfg = MakerConfig(pull_minutes_before_event=30, pull_minutes_after_event=15)
    book = BookTop("T", 0.3, 0.32, NOW)
    event_soon = NOW + timedelta(minutes=10)
    assert should_pull(cfg, book, NOW, event_soon) is not None
    event_far = NOW + timedelta(hours=6)
    assert should_pull(cfg, book, NOW, event_far) is None


def test_should_pull_none_when_fresh_and_no_event():
    cfg = MakerConfig()
    book = BookTop("T", 0.3, 0.32, NOW)
    assert should_pull(cfg, book, NOW, None) is None


# --- engine orchestration, via MockMakerVenue (no network) -----------------

def test_engine_selects_nothing_with_empty_allowlist():
    cfg = MakerConfig(series_allowlist=())
    engine = MakerEngine(cfg, MockMakerVenue())
    assert engine.select_markets() == []


def test_engine_places_quotes_on_selected_market():
    cfg = MakerConfig(series_allowlist=("KXHIGH",), quote_size=100, dry_run=False)
    venue = MockMakerVenue(books={"KXHIGH-NYC": BookTop("KXHIGH-NYC", 0.28, 0.34, NOW)})
    engine = MakerEngine(cfg, venue)
    actions = engine.cycle(now=NOW)
    placed = [a for a in actions if a.kind == "place"]
    assert len(placed) == 2  # yes + no
    assert len(venue.placed) == 2


def test_engine_dry_run_never_calls_venue_place():
    cfg = MakerConfig(series_allowlist=("KXHIGH",), quote_size=100, dry_run=True)
    venue = MockMakerVenue(books={"KXHIGH-NYC": BookTop("KXHIGH-NYC", 0.28, 0.34, NOW)})
    engine = MakerEngine(cfg, venue)
    engine.cycle(now=NOW)
    assert venue.placed == []


def test_engine_pulls_resting_orders_for_dropped_market():
    cfg = MakerConfig(series_allowlist=(), dry_run=False)
    venue = MockMakerVenue()
    venue.resting.append(RestingOrder("o1", "KXHIGH-OLD", "yes", 0.28, 100))
    engine = MakerEngine(cfg, venue)
    actions = engine.cycle(now=NOW)
    assert any(a.kind == "cancel" and a.ticker == "KXHIGH-OLD" for a in actions)
    assert "o1" in venue.cancelled
