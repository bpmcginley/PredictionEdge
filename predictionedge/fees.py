"""Kalshi trading-fee math.

Kalshi's general taker fee for a fill is:

    fee = roundup_to_cent( multiplier * contracts * price * (1 - price) )

with the default multiplier 0.07 for most markets (some markets use a different
multiplier - always confirm against https://kalshi.com/fee-schedule). The fee
peaks at the 50c price point (~1.75c/contract = 3.5% of price) and shrinks toward
zero at the extremes, which is exactly why a sub-$1k book should prefer entries
away from 50c. Maker orders are charged 25% of the taker fee.
"""

from __future__ import annotations

import math

DEFAULT_FEE_MULTIPLIER = 0.07
MAKER_FRACTION = 0.25

# Series whose MAKER fills are charged (25% of taker) rather than free. Kalshi's
# fee schedule calls these "designated" markets; the general schedule charges
# resting orders nothing. Empty until a traded series actually appears on that
# list - membership is a fact to verify against https://kalshi.com/fee-schedule,
# not a guess to default on.
DESIGNATED_MAKER_SERIES: tuple[str, ...] = ()


def is_designated(ticker: str) -> bool:
    """Does this market charge maker fees? Prefix match against the vetted list."""
    t = (ticker or "").strip().upper()
    return any(t.startswith(s) for s in DESIGNATED_MAKER_SERIES)


def maker_trade_fee(
    price: float,
    contracts: int,
    *,
    multiplier: float = DEFAULT_FEE_MULTIPLIER,
    designated: bool = False,
) -> float:
    """Total fee in dollars for a RESTING order that fills.

    Zero on standard markets (verified against the fee schedule 2026-08):
    the ``maker=True`` branch of `trade_fee` - 25% of taker - is the charge on
    Kalshi's *designated* markets only, so that path is opt-in via
    ``designated`` rather than the default. Keeping this separate from
    `trade_fee` means the whale sleeve's long-standing 25%-of-taker accounting
    does not silently change mid-sample.
    """
    if not 0.0 < price < 1.0:
        raise ValueError(f"price must be in (0, 1), got {price}")
    if contracts <= 0 or not designated:
        return 0.0
    return trade_fee(price, contracts, multiplier=multiplier, maker=True)


def _roundup_cent(dollars: float) -> float:
    """Round a dollar amount up to the next whole cent (Kalshi rounds fees up)."""
    return math.ceil(dollars * 100 - 1e-9) / 100


def trade_fee(
    price: float,
    contracts: int,
    *,
    multiplier: float = DEFAULT_FEE_MULTIPLIER,
    maker: bool = False,
) -> float:
    """Total fee in dollars for filling ``contracts`` at ``price`` (0..1)."""
    if not 0.0 < price < 1.0:
        raise ValueError(f"price must be in (0, 1), got {price}")
    if contracts <= 0:
        return 0.0
    raw = multiplier * contracts * price * (1.0 - price)
    if maker:
        raw *= MAKER_FRACTION
    return _roundup_cent(raw)


def fee_per_contract(
    price: float, *, multiplier: float = DEFAULT_FEE_MULTIPLIER, maker: bool = False
) -> float:
    """Unrounded per-contract fee - the right thing to use inside EV math.

    Using the rounded total divided by size would overstate the cost of a single
    contract, so for edge calculations we use the smooth, unrounded rate.
    """
    if not 0.0 < price < 1.0:
        raise ValueError(f"price must be in (0, 1), got {price}")
    rate = multiplier * price * (1.0 - price)
    return rate * MAKER_FRACTION if maker else rate
