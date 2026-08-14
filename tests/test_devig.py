import math
import random

import pytest

from predictionedge.devig import (
    american_to_decimal,
    devig,
    devig_multiplicative,
    devig_power,
    devig_shin,
    fair_all,
    fair_prob_two_way,
    implied_from_decimal,
    overround,
    require_complete_market,
)

ALL_METHODS = ("multiplicative", "additive", "power", "shin", "blend")


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
    for method in ALL_METHODS:
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
    for method in ALL_METHODS:
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


# --- power + Shin + blend ---------------------------------------------------
# The upgrade's whole claim (Štrumbelj 2014): multiplicative is the least
# accurate de-vig, and power/Shin's gains concentrate on lopsided books, where
# they hand the favourite MORE and the longshot LESS than multiplicative does.

def _vigged_books(seed: int = 7, count: int = 200) -> list[list[float]]:
    """Random valid books: 2- and 3-outcome, assorted vig loads and shapes."""
    rng = random.Random(seed)
    books = []
    for _ in range(count):
        n = rng.choice((2, 2, 3))          # sports h2h dominates; soccer 3-way too
        raw = [rng.uniform(0.05, 1.0) for _ in range(n)]
        true = [r / sum(raw) for r in raw]
        margin = rng.uniform(1.0, 1.12)    # fair through heavy 12% overround
        # Load the vig unevenly (longshot-heavier, like real books), then make
        # sure the book is still a book: total must be >= 1.
        implied = [min(0.999, t * margin * rng.uniform(0.98, 1.06)) for t in true]
        if sum(implied) >= 1.0:
            books.append(implied)
    assert len(books) > 150                # the generator actually generated
    return books


def test_property_all_methods_valid_probabilities():
    """For ANY valid book: every method sums to 1 (+-1e-9), each p in (0,1)."""
    for implied in _vigged_books():
        for method in ("multiplicative", "power", "shin", "blend"):
            fair = devig(implied, method)
            assert abs(sum(fair) - 1.0) < 1e-9, (method, implied)
            assert all(0.0 < p < 1.0 for p in fair), (method, implied)


def test_property_power_and_shin_favour_the_favourite():
    """On lopsided 2-outcome books the favourite gains vs multiplicative.

    This direction IS the feature: books load vig onto longshots, multiplicative
    removes it proportionally, so it shaves the favourite too hard. Power and
    Shin both push probability back toward the favourite.
    """
    rng = random.Random(11)
    for _ in range(100):
        q_fav = rng.uniform(0.60, 0.95)
        q_dog = (1.0 - q_fav) * rng.uniform(1.02, 1.15) + rng.uniform(0.005, 0.02)
        implied = [q_fav, q_dog]
        if sum(implied) < 1.0:
            continue
        mult = devig_multiplicative(implied)
        power = devig_power(implied)
        shin = devig_shin(implied)
        assert power[0] > mult[0], implied     # favourite gets MORE
        assert shin[0] > mult[0], implied
        assert power[1] < mult[1], implied     # longshot gets LESS
        assert shin[1] < mult[1], implied


def test_property_fair_coin_book_all_methods_agree():
    """Symmetric book: no method has any favourite to favour."""
    for q in (0.50, 0.52, 0.55):               # no-vig coin through 10% overround
        for method in ALL_METHODS:
            fair = devig([q, q], method)
            assert abs(fair[0] - 0.5) < 1e-9, (method, q)
            assert abs(fair[1] - 0.5) < 1e-9, (method, q)


def test_vig_free_book_is_returned_unchanged():
    """Implied summing to exactly 1: power recovers k=1, Shin recovers z=0."""
    implied = [0.6, 0.4]
    for method in ("multiplicative", "power", "shin", "blend"):
        fair = devig(implied, method)
        assert abs(fair[0] - 0.6) < 1e-6, method
        assert abs(fair[1] - 0.4) < 1e-6, method


def test_three_outcome_soccer_book():
    """The real Pinnacle three-way: home 1.16 / draw 6.60 / away 15.42."""
    implied = [implied_from_decimal(x) for x in (_HOME, _DRAW, _AWAY)]
    mult = devig_multiplicative(implied)
    for method in ("power", "shin", "blend"):
        fair = devig(implied, method)
        assert abs(sum(fair) - 1.0) < 1e-9
        assert all(0.0 < p < 1.0 for p in fair)
        assert fair[0] > mult[0]               # favourite up vs multiplicative
        assert fair[2] < mult[2]               # extreme longshot down


def test_extreme_favourite():
    """q=0.95 favourite: all methods stay valid and the direction holds."""
    implied = [0.95, 0.10]
    mult, power, shin = (devig(implied, m) for m in ("multiplicative", "power", "shin"))
    for fair in (mult, power, shin):
        assert abs(sum(fair) - 1.0) < 1e-9
        assert all(0.0 < p < 1.0 for p in fair)
    assert power[0] > mult[0] and shin[0] > mult[0]


def test_shin_two_way_closed_form_matches_bisection_conditions():
    """The closed-form z reproduces sum(p)=1 exactly and z is a sane margin."""
    from predictionedge.devig import _shin_probs, _shin_z_two_way

    implied = [implied_from_decimal(1.50), implied_from_decimal(2.80)]
    z = _shin_z_two_way(implied)
    assert 0.0 < z < 0.2                       # a few percent, not a pathology
    assert abs(sum(_shin_probs(implied, z)) - 1.0) < 1e-12


def test_shin_falls_back_to_multiplicative_when_unsolvable(caplog):
    """An absurd overround (z would exceed the bracket) must not guess.

    [0.9, 0.9, 0.9] has a 170% overround - no insider share in [0, 0.2] can
    explain it, so Shin logs and returns the multiplicative answer instead of a
    confident fabrication.
    """
    implied = [0.9, 0.9, 0.9]
    with caplog.at_level("WARNING", logger="predictionedge.devig"):
        fair = devig(implied, "shin")
    assert fair == devig_multiplicative(implied)
    assert any("falling back to multiplicative" in r.message for r in caplog.records)


def test_blend_sits_between_power_and_shin():
    implied = [implied_from_decimal(1.30), implied_from_decimal(3.80)]
    power, shin, blend = (devig(implied, m) for m in ("power", "shin", "blend"))
    lo, hi = sorted((power[0], shin[0]))
    assert lo <= blend[0] <= hi
    assert abs(sum(blend) - 1.0) < 1e-12


def test_blend_is_the_log_odds_mean_on_two_way():
    implied = [0.60, 0.45]
    power, shin, blend = (devig(implied, m) for m in ("power", "shin", "blend"))
    logit = lambda p: math.log(p / (1.0 - p))  # noqa: E731
    expected = 1.0 / (1.0 + math.exp(-0.5 * (logit(power[0]) + logit(shin[0]))))
    # 1e-9, not 1e-12: blend renormalises, and power's bisection tolerance means
    # its two probs don't sum to 1 at machine precision.
    assert abs(blend[0] - expected) < 1e-9


def test_fair_all_carries_every_ab_method():
    """The instrumentation dict: one value per A/B-tracked method, consistent
    with calling each method directly."""
    implied = [implied_from_decimal(1.80), implied_from_decimal(2.10)]
    out = fair_all(implied)
    assert set(out) == {"fair_mult", "fair_power", "fair_shin"}
    assert out["fair_mult"] == devig(implied, "multiplicative")[0]
    assert out["fair_power"] == devig(implied, "power")[0]
    assert out["fair_shin"] == devig(implied, "shin")[0]


def test_unknown_method_lists_all_five():
    with pytest.raises(ValueError, match="shin"):
        devig([0.55, 0.50], "nope")
