from predictionedge.devig import (
    american_to_decimal,
    devig,
    fair_prob_two_way,
    implied_from_decimal,
    overround,
)


def test_implied_from_decimal():
    assert abs(implied_from_decimal(2.0) - 0.5) < 1e-12
    assert abs(implied_from_decimal(4.0) - 0.25) < 1e-12


def test_american_to_decimal():
    assert abs(american_to_decimal(100) - 2.0) < 1e-12
    assert abs(american_to_decimal(-200) - 1.5) < 1e-12
    assert abs(american_to_decimal(150) - 2.5) < 1e-12


def test_devig_methods_sum_to_one():
    implied = [implied_from_decimal(1.80), implied_from_decimal(2.10)]
    assert overround(implied) > 0  # there is vig to remove
    for method in ("multiplicative", "additive", "power"):
        fair = devig(implied, method)
        assert abs(sum(fair) - 1.0) < 1e-6


def test_fair_prob_two_way_favourite():
    # Shorter price -> higher fair probability, and below the vigged implied.
    p = fair_prob_two_way(1.50, 2.80)
    assert 0.5 < p < implied_from_decimal(1.50)


def test_additive_falls_back_when_negative():
    # Extreme favourite where additive would push the longshot negative.
    fair = devig([implied_from_decimal(1.02), implied_from_decimal(30.0)], "additive")
    assert all(p > 0 for p in fair)
    assert abs(sum(fair) - 1.0) < 1e-6
