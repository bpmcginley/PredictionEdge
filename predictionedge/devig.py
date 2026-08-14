"""De-vigging: turn bookmaker odds into fair probabilities.

A sportsbook's quoted prices imply probabilities that sum to more than 1 - the
excess is the "vig" (the book's margin). De-vigging removes it to recover the
book's true probability estimate, which we treat as fair value to fade Kalshi
against.

Five methods are provided. Multiplicative (proportional normalisation) is the
most common but - per Štrumbelj (2014) and the practitioner replications since -
also the LEAST accurate, because it spreads the vig proportionally when books
actually load it onto longshots. Additive spreads the overround equally. Power
solves for an exponent that makes the probabilities sum to 1; Shin inverts an
insider-trading model of how the book sets its margin. Power and Shin best
recover true probabilities, with the gains concentrated on lopsided
favourite/longshot markets - both give the favourite MORE and the longshot LESS
than multiplicative does. Blend averages power and Shin in log-odds space, the
scale on which probabilities combine without favourite/longshot distortion.
"""

from __future__ import annotations

import logging
import math

log = logging.getLogger(__name__)


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


def require_complete_market(implied: list[float]) -> None:
    """Reject a leg set that cannot be a whole market.

    A single book's own implied probabilities must sum to MORE than 1 - that excess is
    the vig, and it is how the book makes money. A sum below 1 is therefore never a
    "tight book": it means legs are missing from the list we were handed.

    This is the guard the de-vig path was missing. `odds.py` reads only `home_team` and
    `away_team` from each h2h market, so on a three-way sport the Draw was silently
    dropped and the remaining two summed to less than 1; `devig_multiplicative` checked
    only `total <= 0`, so it happily divided by that total and scaled BOTH survivors up.
    Measured on a real Pinnacle line, 1/1.16 + 1/15.42 = 0.9269 renormalised P(home) to
    0.930 against a correct three-way 0.8174 - **+11 points, always toward the
    favourite**, which is four times `min_edge`. It also sat UNDER `sane_edge_ceiling`,
    so the circuit breaker would not have caught it; only this check does.

    Raises ValueError so `consensus_fair_prob` drops the offending book and, if no book
    survives, returns None - no fair value, hence no edge. Failing closed, not loudly.
    """
    total = sum(implied)
    if total <= 0:
        raise ValueError("implied probabilities must sum to a positive number")
    if total < 1.0:
        raise ValueError(
            f"implied probabilities sum to {total:.4f} < 1: this market is missing legs "
            "(e.g. the Draw on a three-way), so de-vigging it would inflate the "
            "survivors rather than remove vig"
        )


def devig_multiplicative(implied: list[float]) -> list[float]:
    """Proportional normalisation: divide each by the total. The standard default."""
    require_complete_market(implied)
    total = sum(implied)
    return [p / total for p in implied]


def devig_additive(implied: list[float]) -> list[float]:
    """Subtract an equal share of the overround from each outcome."""
    # Needs its own guard, not just the multiplicative fallback's: on an incomplete set
    # the overround is NEGATIVE, so every leg gets scaled up, stays positive, and the
    # fallback never fires. The inflated result would look perfectly well-formed.
    require_complete_market(implied)
    share = overround(implied) / len(implied)
    fair = [p - share for p in implied]
    if any(p <= 0 for p in fair):
        # Additive can go negative on lopsided markets; fall back to multiplicative.
        return devig_multiplicative(implied)
    return fair


def devig_power(implied: list[float], *, tol: float = 1e-10, max_iter: int = 200) -> list[float]:
    """Find exponent k with sum(p_i ** k) == 1, via bisection on k."""
    # Also guarded: with legs missing the bisection still converges (sum(p^0) = n > 1),
    # so it would return a confident, wrong answer instead of failing.
    require_complete_market(implied)
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


def _shin_probs(implied: list[float], z: float) -> list[float]:
    """Shin fair probabilities for a given insider share z (Shin 1992/1993).

    p_i = (sqrt(z^2 + 4(1-z) q_i^2 / S) - z) / (2(1-z)),  S = sum(q_j).
    At z = 0 this is q_i / sqrt(S), so a book with no vig (S = 1) is returned
    unchanged - z = 0 is recovered exactly, not approximately.
    """
    s = sum(implied)
    w = 1.0 - z
    return [(math.sqrt(z * z + 4.0 * w * q * q / s) - z) / (2.0 * w) for q in implied]


def _shin_z_two_way(implied: list[float]) -> float:
    """Closed-form insider share for a 2-outcome book.

    Setting sum(p_i) = 1 in the Shin formula and solving for w = 1-z gives
    w = 2(a1 + a2 - 1) / ((a1 - a2)^2 - 1) with a_i = q_i^2 / S. Exact - no
    iteration, no tolerance.
    """
    s = sum(implied)
    a1, a2 = (q * q / s for q in implied)
    return 1.0 - 2.0 * (a1 + a2 - 1.0) / ((a1 - a2) ** 2 - 1.0)


def devig_shin(implied: list[float], *, tol: float = 1e-12, max_iter: int = 200,
               z_max: float = 0.2) -> list[float]:
    """Shin's method: back out the insider share z the book priced its vig for.

    Two-outcome books use the closed form; n-outcome books bisect on z in
    [0, z_max] (real books imply z of a few percent; 0.2 is far beyond any
    plausible margin). If the solve lands outside that bracket or fails to
    converge, falls back to multiplicative and logs - a wrong-but-confident
    number is worse than the standard one.
    """
    require_complete_market(implied)
    if len(implied) == 2:
        z = _shin_z_two_way(implied)
        if not 0.0 <= z < 1.0:
            log.warning("shin closed form gave z=%.6f outside [0,1); "
                        "falling back to multiplicative", z)
            return devig_multiplicative(implied)
        return _shin_probs(implied, z)

    # sum(p_i(z)) is sqrt(S) >= 1 at z=0 and decreases in z; bisect to 1.
    lo, hi = 0.0, z_max
    if sum(_shin_probs(implied, hi)) - 1.0 > 0.0:
        log.warning("shin bisection not bracketed on [0, %.2f] (overround %.4f); "
                    "falling back to multiplicative", z_max, overround(implied))
        return devig_multiplicative(implied)
    z = 0.0
    for _ in range(max_iter):
        z = 0.5 * (lo + hi)
        f = sum(_shin_probs(implied, z)) - 1.0
        if abs(f) < tol:
            break
        if f > 0.0:
            lo = z
        else:
            hi = z
    else:
        # max_iter bisections on [0, 0.2] is far past float precision; unreachable
        # in practice, but if it ever fires we refuse to guess.
        log.warning("shin bisection did not converge; falling back to multiplicative")
        return devig_multiplicative(implied)
    return _shin_probs(implied, z)


def devig_blend(implied: list[float]) -> list[float]:
    """Average power and Shin in log-odds space, then renormalise.

    Log-odds is the scale on which averaging two probability estimates does not
    systematically drag favourites and longshots toward 0.5. For a two-outcome
    book the result sums to 1 exactly (the logits are antisymmetric); for n > 2
    a final multiplicative renormalisation absorbs the tiny residual.
    """
    require_complete_market(implied)
    power = devig_power(implied)
    shin = devig_shin(implied)
    blended = []
    for pp, ps in zip(power, shin):
        logit = 0.5 * (math.log(pp / (1.0 - pp)) + math.log(ps / (1.0 - ps)))
        blended.append(1.0 / (1.0 + math.exp(-logit)))
    total = sum(blended)
    return [p / total for p in blended]


_METHODS = {
    "multiplicative": devig_multiplicative,
    "additive": devig_additive,
    "power": devig_power,
    "shin": devig_shin,
    "blend": devig_blend,
}


def devig(implied: list[float], method: str = "multiplicative") -> list[float]:
    """De-vig by name. Methods: multiplicative, additive, power, shin, blend."""
    try:
        return _METHODS[method](implied)
    except KeyError:
        raise ValueError(f"unknown de-vig method {method!r}; choose from {sorted(_METHODS)}")


def fair_all(implied: list[float]) -> dict[str, float]:
    """First-outcome fair probability under each A/B-tracked method.

    This is the paper trial's instrumentation: every sports record carries all
    three so the trial can score which method would have done better WITHOUT
    running a second trial. Keys are the trial-row field names.
    """
    return {
        "fair_mult": devig_multiplicative(implied)[0],
        "fair_power": devig_power(implied)[0],
        "fair_shin": devig_shin(implied)[0],
    }


def fair_prob_two_way(
    win_decimal: float, lose_decimal: float, method: str = "multiplicative"
) -> float:
    """Fair probability of the first outcome in a two-way market from decimal odds."""
    implied = [implied_from_decimal(win_decimal), implied_from_decimal(lose_decimal)]
    return devig(implied, method)[0]
