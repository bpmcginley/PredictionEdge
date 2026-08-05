"""Closed-form arbitrage / hedge sizing.

When an order was placed at price ``x`` while the event's probability was ``A``, and
that probability has since moved by a factor ``q`` (now ``q*A``), this computes the
optimal new orders ``y`` (same event) and ``z`` (inverse event) that maximise the
average constraint slack as a fraction of total capital. The optimum always has
``y == 0`` - the lock-in comes from sizing the inverse-side hedge ``z``.

This is a direct, closed-form implementation (no numerical optimiser), per spec.
Used by the order-review step to hedge resting orders when the odds have moved.
"""

from __future__ import annotations


def optimal_arbitrage_orders(x: float, A: float, q: float, tol: float = 1e-12) -> dict:
    """Optimal hedge orders for an order at price ``x``, original prob ``A``, shift ``q``.

    Returns ``{"feasible", "y", "z", "r_max", "case"}``. Raises ``ValueError`` on
    invalid inputs (A not in (0,1), q<=0, x<0, or q*A not in (0,1)).
    """
    if A <= 0.0 or A >= 1.0:
        raise ValueError(f"A must be in (0, 1), got {A}")
    if q <= 0.0:
        raise ValueError(f"q must be > 0, got {q}")
    if x < 0.0:
        raise ValueError(f"x must be >= 0, got {x}")
    if q * A <= 0.0 or q * A >= 1.0:
        raise ValueError(f"q*A must be in (0, 1), got {q * A}")

    if x == 0.0:
        return {"feasible": True, "y": 0.0, "z": 0.0, "r_max": 0.0, "case": "x_zero"}

    # With capital already committed (x > 0), a downward move (q < 1) cannot be
    # hedged into guaranteed slack - the position is underwater versus the new odds.
    if q < 1.0 - tol:
        return {"feasible": False, "y": None, "z": None, "r_max": None,
                "case": "q_below_1_infeasible"}

    # Closed-form optimum for q >= 1. The threshold t decides which bound binds.
    t = A / (1.0 - A)

    if q < t - tol:
        y = 0.0
        z = ((1.0 - q * A) / (q * A)) * x
        r_max = (q - 1.0) / 2.0
        case = "lower_bound_optimum"
    elif abs(q - t) <= tol:
        y = 0.0
        z = ((1.0 - q * A) / (q * A)) * x
        r_max = (q - 1.0) / 2.0
        case = "tie_case"
    else:  # q > t + tol
        y = 0.0
        z = ((1.0 - A) / A) * x
        r_max = A * (q - 1.0) / (2.0 * (1.0 - q * A))
        case = "upper_bound_optimum"

    return {"feasible": True, "y": y, "z": z, "r_max": r_max, "case": case}


def evaluate_arbitrage_slack(
    x: float, A: float, q: float, y: float, z: float, tol: float = 1e-12
) -> dict:
    """Check the slack of a given (y, z) against the two arbitrage constraints."""
    lhs1 = x / A + y / (q * A)
    rhs1 = x + y + z
    lhs2 = z / (1.0 - q * A)
    rhs2 = x + y + z
    d1 = lhs1 - rhs1
    d2 = lhs2 - rhs2
    avg_slack = (d1 + d2) / 2.0
    total = x + y + z
    ratio = avg_slack / total if total > 0 else 0.0
    feasible = d1 >= -tol and d2 >= -tol and y >= -tol and z >= -tol
    return {
        "lhs1": lhs1, "rhs1": rhs1, "lhs2": lhs2, "rhs2": rhs2,
        "d1": d1, "d2": d2, "avg_slack": avg_slack, "ratio": ratio, "feasible": feasible,
    }


if __name__ == "__main__":
    # Spec examples.
    r = optimal_arbitrage_orders(5, 0.5, 1.5)
    assert r["feasible"] and abs(r["y"]) < 1e-12 and abs(r["z"] - 5.0) < 1e-9, r
    r = optimal_arbitrage_orders(5, 0.5, 0.5)
    assert r["feasible"] is False, r
    print("arbitrage self-tests passed")
