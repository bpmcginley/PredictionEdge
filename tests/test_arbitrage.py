from predictionedge.arbitrage import evaluate_arbitrage_slack, optimal_arbitrage_orders


def test_spec_example_upper_bound():
    r = optimal_arbitrage_orders(5, 0.5, 1.5)
    assert r["feasible"] is True
    assert abs(r["y"]) < 1e-12
    assert abs(r["z"] - 5.0) < 1e-9
    assert abs(r["r_max"] - 0.5) < 1e-9
    assert r["case"] == "upper_bound_optimum"


def test_spec_example_infeasible_below_one():
    r = optimal_arbitrage_orders(5, 0.5, 0.5)
    assert r["feasible"] is False
    assert r["y"] is None and r["z"] is None
    assert r["case"] == "q_below_1_infeasible"


def test_x_zero():
    r = optimal_arbitrage_orders(0, 0.4, 2.0)
    assert r == {"feasible": True, "y": 0.0, "z": 0.0, "r_max": 0.0, "case": "x_zero"}


def test_returned_plan_is_feasible_by_evaluator():
    r = optimal_arbitrage_orders(5, 0.5, 1.5)
    ev = evaluate_arbitrage_slack(5, 0.5, 1.5, r["y"], r["z"])
    assert ev["feasible"] is True
    assert ev["d1"] >= -1e-9 and ev["d2"] >= -1e-9


def test_lower_bound_case_for_high_A():
    # A=0.8 -> t=4. q=1.2 is in [1, t) and keeps q*A=0.96 < 1, so lower-bound binds.
    r = optimal_arbitrage_orders(10, 0.8, 1.2)
    assert r["case"] == "lower_bound_optimum"
    assert abs(r["r_max"] - 0.1) < 1e-9  # (q-1)/2


def test_invalid_inputs_raise():
    for bad in [
        lambda: optimal_arbitrage_orders(5, 0.0, 1.5),   # A<=0
        lambda: optimal_arbitrage_orders(5, 1.0, 1.5),   # A>=1
        lambda: optimal_arbitrage_orders(5, 0.5, 0.0),   # q<=0
        lambda: optimal_arbitrage_orders(-1, 0.5, 1.5),  # x<0
        lambda: optimal_arbitrage_orders(5, 0.5, 3.0),   # q*A>=1
    ]:
        try:
            bad()
            assert False, "expected ValueError"
        except ValueError:
            pass
