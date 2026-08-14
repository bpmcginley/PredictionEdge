import pytest

from predictionedge.fees import fee_per_contract, is_designated, maker_trade_fee, trade_fee


def test_fee_peaks_at_fifty_cents():
    # 0.07 * 0.5 * 0.5 = 0.0175/contract -> 3.5% of a 50c contract
    assert abs(fee_per_contract(0.50) - 0.0175) < 1e-9


def test_fee_shrinks_at_extremes():
    assert fee_per_contract(0.90) < fee_per_contract(0.50)
    assert fee_per_contract(0.10) < fee_per_contract(0.50)


def test_maker_is_quarter_of_taker():
    assert abs(fee_per_contract(0.40, maker=True) - 0.25 * fee_per_contract(0.40)) < 1e-12


def test_total_fee_rounds_up_to_cent():
    # 0.07 * 1 * 0.5 * 0.5 = 0.0175 -> rounds up to 0.02
    assert trade_fee(0.50, 1) == 0.02


def test_zero_contracts_zero_fee():
    assert trade_fee(0.50, 0) == 0.0


def test_a_resting_fill_on_a_standard_market_is_free():
    assert maker_trade_fee(0.50, 100) == 0.0


def test_a_resting_fill_on_a_designated_market_pays_a_quarter_of_taker():
    assert maker_trade_fee(0.50, 100, designated=True) == \
        trade_fee(0.50, 100, maker=True)


def test_maker_fee_still_refuses_an_unpriceable_price():
    # Free is not the same as unchecked: a corrupt price must fail loudly here
    # exactly as it does on the taker path.
    with pytest.raises(ValueError):
        maker_trade_fee(0.0, 100)


def test_no_series_is_designated_until_verified_against_the_schedule():
    assert is_designated("KXHIGHNY-26AUG12-B87.5") is False
    assert is_designated("") is False
