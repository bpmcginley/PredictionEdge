"""De-vigging: turn bookmaker odds into fair probabilities.

A sportsbook's quoted prices imply probabilities that sum to more than 1 - the
excess is the "vig" (the book's margin). De-vigging removes it to recover the
book's true probability estimate, which we treat as fair value to fade Kalshi
against.

Three methods are provided. Multiplicative (proportional normalisation) is the
default and the most common; additive spreads the overround equally; power solves
for an exponent that makes the probabilities sum to 1 and tends to behave best on
two-outcome favourite/longshot markets.
"""

from __future__ import annotations


def implied_from_decimal(decimal_odds: float) -> float:
    """Implied (vigged) probability from decimal odds."""
    if decimal_odds <= 1.0:
        raise ValueError(f"decimal odds must be > 1.0, got {decimal_odds}")
    return 1.0 / decimal_odds


def american_to_decimal(american: int) -> float:
    """Convert American (moneyline) odds to decimal odds."""
    if american == 0:
        raise ValueError("American odds cannot be 0")
    return 1.0 + (american / 100.0 if american > 0 else 100.0 / abs(american))


def overround(implied: list[float]) -> float:
    """How much the implied probabilities exceed 1 (the book's margin)."""
    return sum(implied) - 1.0


def devig_multiplicative(implied: list[float]) -> list[float]:
    """Proportional normalisation: divide each by the total. The standard default."""
    total = sum(implied)
    if total <= 0:
        raise ValueError("implied probabilities must sum to a positive number")
    return [p / total for p in implied]


def devig_additive(implied: list[float]) -> list[float]:
    """Subtract an equal share of the overround from each outcome."""
    share = overround(implied) / len(implied)
    fair = [p - share for p in implied]
    if any(p <= 0 for p in fair):
        # Additive can go negative on lopsided markets; fall back to multiplicative.
        return devig_multiplicative(implied)
    return fair


def devig_power(implied: list[float], *, tol: float = 1e-10, max_iter: int = 200) -> list[float]:
    """Find exponent k with sum(p_i ** k) == 1, via bisection on k."""
    lo, hi = 0.0, 10.0
    for _ in range(max_iter):
        k = 0.5 * (lo + hi)
        s = sum(p**k for p in implied)
        if abs(s - 1.0) < tol:
            break
        # sum is monotonically decreasing in k for p_i < 1
        if s > 1.0:
            lo = k
        else:
            hi = k
    k = 0.5 * (lo + hi)
    return [p**k for p in implied]


_METHODS = {
    "multiplicative": devig_multiplicative,
    "additive": devig_additive,
    "power": devig_power,
}


def devig(implied: list[float], method: str = "multiplicative") -> list[float]:
    """De-vig by name. Methods: multiplicative (default), additive, power."""
    try:
        return _METHODS[method](implied)
    except KeyError:
        raise ValueError(f"unknown de-vig method {method!r}; choose from {sorted(_METHODS)}")


def fair_prob_two_way(
    win_decimal: float, lose_decimal: float, method: str = "multiplicative"
) -> float:
    """Fair probability of the first outcome in a two-way market from decimal odds."""
    implied = [implied_from_decimal(win_decimal), implied_from_decimal(lose_decimal)]
    return devig(implied, method)[0]
