import pytest

from predictionedge.devig import (
    american_to_decimal,
    devig,
    fair_prob_two_way,
    implied_from_decimal,
    overround,
    require_complete_market,
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


# --- incomplete markets -----------------------------------------------------
# The real Pinnacle three-way that motivated the guard: home 1.16, draw 6.60,
# away 15.42. Drop the draw - which is exactly what odds.py's home/away read did -
# and the survivors sum to 0.9269 instead of over 1.
_HOME, _DRAW, _AWAY = 1.16, 6.60, 15.42


def test_missing_leg_is_rejected_not_renormalised():
    two_of_three = [implied_from_decimal(_HOME), implied_from_decimal(_AWAY)]
    assert sum(two_of_three) < 1.0            # the tell: a book can never quote this
    with pytest.raises(ValueError, match="missing legs"):
        require_complete_market(two_of_three)


def test_every_method_refuses_an_incomplete_market():
    """All three, not just multiplicative: additive and power each fail differently.

    Additive's negative-value fallback never fires (a negative overround scales legs
    UP), and power's bisection still converges. Both would return a confident, wrong
    number - which is the dangerous kind.
    """
    two_of_three = [implied_from_decimal(_HOME), implied_from_decimal(_AWAY)]
    for method in ("multiplicative", "additive", "power"):
        with pytest.raises(ValueError):
            devig(two_of_three, method)


def test_the_bias_the_guard_prevents_was_toward_the_favourite():
    """Quantifies the harm, so a future 'relax this' has the number in front of it."""
    three = [implied_from_decimal(x) for x in (_HOME, _DRAW, _AWAY)]
    correct = devig(three)[0]
    two = [implied_from_decimal(_HOME), implied_from_decimal(_AWAY)]
    naive = two[0] / sum(two)                 # what the old code computed
    assert naive - correct > 0.10             # +11 points, always toward the favourite


def test_a_genuine_two_way_book_still_passes():
    """The guard must not fire on the normal case it sits in front of."""
    require_complete_market([implied_from_decimal(1.80), implied_from_decimal(2.10)])
