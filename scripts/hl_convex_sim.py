"""Payoff math for a high-leverage "occasional hit" ticket on Hyperliquid.

Stdlib only. Two questions this answers, both of which matter more than the entry signal:

1. Knock-out geometry. On Hyperliquid an isolated position is liquidated when
   isolated margin + PnL falls to the *maintenance* margin, and maintenance is fixed by
   the asset's margin tier (half of initial at the tier's max leverage) - NOT by the
   leverage you picked. So for a tier-1 BTC ticket (max 40x -> initial 2.5%,
   maintenance 1.25%) the distance to liquidation is:

        d_liq = 1/L - mm            (fraction of entry price)

   40x -> 1.25%, 20x -> 3.75%, 10x -> 8.75%. Under a driftless log-price with annualized
   vol sigma held for T minutes, the reflection principle gives

        P(knock-out) = 2 * Phi( -d_liq / (sigma * sqrt(T / minutes_per_year)) ).

   That closed form is the whole "is this ticket survivable" test.

2. Ticket EV. A ticket = isolated margin m at leverage L with a take-profit at +tp,
   a stop at -sl (tighter than d_liq so we never pay the backstop), and a time stop T.
   Monte Carlo over a jump-diffusion with a directional drift that is right with
   probability p_dir. Output: hit rate, mean return on margin, and the Kelly fraction.

Usage:
    python scripts/hl_convex_sim.py            # prints the tables used in the doc
"""

from __future__ import annotations

import math
import random
from statistics import NormalDist

ND = NormalDist()
MIN_PER_YEAR = 365.25 * 24 * 60

TAKER = 0.00045        # base perp taker fee, per side, on notional
SLIP = 0.0002          # assumed slippage per side (BTC/ETH top-of-book, small size)
MM_TIER1_BTC = 0.0125  # maintenance margin, BTC tier 1 (40x max -> 2.5%/2)


def liq_distance(leverage: float, mm: float = MM_TIER1_BTC) -> float:
    return 1.0 / leverage - mm


def p_knockout(leverage: float, sigma_ann: float, minutes: float, mm: float = MM_TIER1_BTC) -> float:
    d = liq_distance(leverage, mm)
    s = sigma_ann * math.sqrt(minutes / MIN_PER_YEAR)
    return 2.0 * ND.cdf(-d / s)


def simulate_ticket(
    *,
    leverage: float,
    tp: float,            # take-profit distance as fraction of price
    sl: float,            # stop distance as fraction of price (must be < liq distance)
    minutes: int,
    sigma_ann: float,
    drift_per_min: float, # magnitude of the directional impulse we are betting on
    p_dir: float,         # probability the impulse goes our way
    n: int = 20000,
    seed: int = 7,
    mm: float = MM_TIER1_BTC,
    trail: float | None = None,   # trailing stop distance from the running best price
    jump_per_min: float = 0.0,    # probability per minute of a +/- jump
    jump_size: float = 0.01,      # jump magnitude (fraction of price)
) -> dict:
    rng = random.Random(seed)
    d_liq = liq_distance(leverage, mm)
    assert sl < d_liq, "stop must sit inside the liquidation distance"
    step_sigma = sigma_ann * math.sqrt(1.0 / MIN_PER_YEAR)
    cost = 2 * (TAKER + SLIP) * leverage   # round-trip cost as a fraction of margin
    rets, hits, stops, kos = [], 0, 0, 0
    for _ in range(n):
        sign = 1.0 if rng.random() < p_dir else -1.0
        x = 0.0
        best = 0.0
        outcome = None
        for _ in range(minutes):
            x += sign * drift_per_min + step_sigma * rng.gauss(0, 1)
            if jump_per_min and rng.random() < jump_per_min:
                x += jump_size * (1.0 if rng.random() < 0.5 else -1.0)
            best = max(best, x)
            if trail is not None and best > 0 and x <= best - trail:
                outcome = x - SLIP
                if outcome > 0:
                    hits += 1
                else:
                    stops += 1
                break
            if x >= tp:
                outcome = tp
                hits += 1
                break
            if x <= -d_liq:
                outcome = -1.0 / leverage    # whole margin gone (plus mm not returned on backstop)
                kos += 1
                break
            if x <= -sl:
                outcome = -sl - SLIP          # stop is a market order on mark trigger
                stops += 1
                break
        if outcome is None:
            outcome = x                       # time stop: close at market
        rets.append(outcome * leverage - cost)
    mean = sum(rets) / n
    wins = [r for r in rets if r > 0]
    losses = [-r for r in rets if r <= 0]
    p = len(wins) / n
    b = (sum(wins) / len(wins)) / (sum(losses) / len(losses)) if wins and losses else float("nan")
    kelly = p - (1 - p) / b if b == b and b > 0 else float("nan")
    return {
        "hit": hits / n, "stop": stops / n, "ko": kos / n, "p_win": p,
        "avg_win_x": (sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss_x": (sum(losses) / len(losses)) if losses else 0.0,
        "ev_on_margin": mean, "kelly": kelly, "cost_on_margin": cost,
    }


def main() -> None:
    print("Liquidation distance and round-trip cost as % of the ticket (BTC tier 1):")
    for L in (5, 10, 20, 40):
        print(f"  {L:>3}x  d_liq={100*liq_distance(L):5.2f}%   fees+slip={100*2*(TAKER+SLIP)*L:5.2f}% of margin")

    print("\nP(knock-out) holding a driftless position, by leverage / annualized vol / minutes:")
    print("  L    vol    5m     15m    60m    240m")
    for L in (10, 20, 40):
        for vol in (0.30, 0.60, 1.20):
            row = "  ".join(f"{100*p_knockout(L, vol, m):5.1f}%" for m in (5, 15, 60, 240))
            print(f"  {L:>2}x  {int(vol*100):>3}%  {row}")

    print("\nTicket EV (BTC, 60-min event window, local vol 90% ann.). Impulse = drift/min if right.")
    print("  L   tp     sl     p_dir  drift  hit    stop   ko     p_win  avgW   avgL   EV      Kelly")
    scenarios = [
        # leverage, tp, sl, p_dir, drift/min
        (40, 0.020, 0.008, 0.50, 0.00000),   # no edge, pure noise: what fees do to you
        (40, 0.020, 0.008, 0.60, 0.00040),   # modest edge, tight stop
        (20, 0.030, 0.015, 0.60, 0.00040),   # same edge, half leverage, wider stop
        (20, 0.030, 0.015, 0.65, 0.00060),   # stronger impulse
        (10, 0.050, 0.030, 0.65, 0.00060),   # low leverage, wide stop
        (40, 0.010, 0.006, 0.60, 0.00040),   # scalp-sized target at max leverage
    ]
    for L, tp, sl, pd, dr in scenarios:
        r = simulate_ticket(leverage=L, tp=tp, sl=sl, minutes=60, sigma_ann=0.90,
                            drift_per_min=dr, p_dir=pd)
        print(f"  {L:>2}  {tp:.3f}  {sl:.3f}  {pd:.2f}   {dr:.4f} "
              f"{r['hit']:5.2f}  {r['stop']:5.2f}  {r['ko']:5.2f}  {r['p_win']:5.2f}  "
              f"{r['avg_win_x']:5.2f}  {r['avg_loss_x']:5.2f}  {r['ev_on_margin']:+6.3f}  {r['kelly']:+6.3f}")

    print("\n'Occasional hit' variant: no take-profit, tight stop, trailing stop once in profit,")
    print("240-min window, local vol 60%, plus a 1% jump with prob 0.5%/min (either direction).")
    print("  L   sl     trail  p_dir  drift   hit    stop   ko     p_win  avgW   avgL   EV      Kelly")
    for L, sl, tr, pd, dr in [
        (40, 0.008, 0.006, 0.50, 0.0000),
        (40, 0.008, 0.006, 0.55, 0.0002),
        (20, 0.015, 0.010, 0.55, 0.0002),
        (20, 0.015, 0.010, 0.60, 0.0003),
        (20, 0.020, 0.015, 0.60, 0.0003),
        (10, 0.030, 0.020, 0.60, 0.0003),
    ]:
        r = simulate_ticket(leverage=L, tp=0.50, sl=sl, minutes=240, sigma_ann=0.60,
                            drift_per_min=dr, p_dir=pd, trail=tr, jump_per_min=0.005, jump_size=0.01)
        print(f"  {L:>2}  {sl:.3f}  {tr:.3f}  {pd:.2f}   {dr:.4f}  "
              f"{r['hit']:5.2f}  {r['stop']:5.2f}  {r['ko']:5.2f}  {r['p_win']:5.2f}  "
              f"{r['avg_win_x']:5.2f}  {r['avg_loss_x']:5.2f}  {r['ev_on_margin']:+6.3f}  {r['kelly']:+6.3f}")

    print("\nBreak-even hit rate for a ticket paying b:1 net (Kelly f* = p - (1-p)/b):")
    for b in (1, 2, 3, 5, 10):
        print(f"  b={b:>2}  break-even p={100/(1+b):5.1f}%   quarter-Kelly at p=+10pts: "
              f"{100*max(0.0, ((1/(1+b))+0.10) - (1-((1/(1+b))+0.10))/b)/4:4.1f}% of bankroll")


if __name__ == "__main__":
    main()
