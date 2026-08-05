from predictionedge.omen import (
    CONTRACTS_PER_EVENT_PER_1K,
    CONTRACTS_PER_MARKET_PER_1K,
    OmenAccount,
    event_url,
    size_position,
)


def test_contract_limits_scale_with_account():
    a = OmenAccount(size=100_000)
    assert a.max_contracts_per_market == 100 * CONTRACTS_PER_MARKET_PER_1K
    assert a.max_contracts_per_event == 100 * CONTRACTS_PER_EVENT_PER_1K


def test_daily_loss_limit_is_five_percent():
    assert OmenAccount(size=50_000).daily_loss_limit == 2_500


def test_size_position_respects_risk_budget():
    a = OmenAccount(size=100_000, per_trade_fraction=0.01)   # $1,000 budget
    s = size_position(0.50, a, conviction=1.0)
    assert s is not None
    assert s.contracts == 1900          # budget wants 2000, Omen caps at 19/$1k
    assert s.capped_by == "omen-market-limit"


def test_conviction_scales_size_down():
    a = OmenAccount(size=100_000, per_trade_fraction=0.001)  # $100 budget
    strong = size_position(0.50, a, conviction=1.0)
    weak = size_position(0.50, a, conviction=0.4)
    assert strong.contracts == 200 and weak.contracts == 80
    assert weak.capped_by == "risk-budget"


def test_event_ceiling_accounts_for_existing_exposure():
    a = OmenAccount(size=1_000, per_trade_fraction=1.0)
    # per-event cap is 38; 35 already on -> only 3 left even though per-market allows 19
    s = size_position(0.10, a, conviction=1.0, already_on_event=35)
    assert s.contracts == 3


def test_rejects_sub_penny_and_resolved_prices():
    a = OmenAccount()
    assert size_position(0.0, a) is None
    assert size_position(0.005, a) is None      # below Omen's $0.01 minimum
    assert size_position(1.0, a) is None


def test_rejects_when_budget_buys_nothing():
    a = OmenAccount(size=100, per_trade_fraction=0.0001)   # $0.01 budget
    assert size_position(0.90, a) is None


def test_event_url():
    assert event_url("nfl-abc") == "https://polymarket.com/event/nfl-abc"
    assert event_url("") == ""
