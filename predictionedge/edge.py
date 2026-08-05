"""Edge detection and position sizing.

Given a fair probability (de-vig consensus, optionally whale-blended) and the live
Kalshi prices, decide whether buying YES or NO carries positive expected value
*after fees*, and size it with fractional Kelly under hard caps.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import Config
from .fees import fee_per_contract, trade_fee


@dataclass(frozen=True)
class Quote:
    """The prices you'd actually pay on Kalshi, in dollars (0..1)."""
    yes_ask: float   # cost to buy a YES contract
    no_ask: float    # cost to buy a NO contract


@dataclass(frozen=True)
class Opportunity:
    ticker: str
    side: str                 # "yes" or "no"
    fair_prob: float          # our P(YES)
    price: float              # price paid for the chosen side
    edge_per_contract: float  # net EV per contract, dollars
    contracts: int
    stake: float              # dollars at risk
    est_fee: float            # modelled fill fee, dollars
    expected_value: float     # net EV across the whole position, dollars

    def describe(self) -> str:
        return (
            f"{self.ticker}: BUY {self.side.upper()} x{self.contracts} @ {self.price:.2f}  "
            f"fair(YES)={self.fair_prob:.3f}  edge={self.edge_per_contract*100:+.2f}c/ct  "
            f"EV=${self.expected_value:+.2f}  stake=${self.stake:.2f}  fee=${self.est_fee:.2f}"
        )


def _kelly_fraction(win_prob: float, price: float) -> float:
    """Full-Kelly fraction of bankroll for a contract bought at ``price``.

    Stake ``price`` to net ``1 - price`` on a win. f* = p - (1-p)*price/(1-price).
    """
    if not 0.0 < price < 1.0:
        return 0.0
    return win_prob - (1.0 - win_prob) * price / (1.0 - price)


def _evaluate_side(side: str, win_prob: float, price: float, cfg: Config) -> Opportunity | None:
    if not 0.0 < price < 1.0:
        return None  # 0/100c (e.g. a longshot's NO side) - no tradeable edge, skip
    net_per_ct = win_prob - price - fee_per_contract(
        price, multiplier=cfg.fee_multiplier, maker=cfg.assume_maker
    )
    if net_per_ct < cfg.min_edge:
        return None

    frac = cfg.kelly_fraction * _kelly_fraction(win_prob, price)
    frac = min(frac, cfg.per_market_max_fraction)
    if frac <= 0.0:
        return None

    contracts = min(int(math.floor(cfg.bankroll * frac / price)), cfg.max_contracts)
    if contracts <= 0:
        return None

    fee = trade_fee(price, contracts, multiplier=cfg.fee_multiplier, maker=cfg.assume_maker)
    return Opportunity(
        ticker="",  # filled in by caller
        side=side,
        fair_prob=win_prob if side == "yes" else 1.0 - win_prob,
        price=price,
        edge_per_contract=net_per_ct,
        contracts=contracts,
        stake=contracts * price,
        est_fee=fee,
        expected_value=net_per_ct * contracts,
    )


def find_edge(ticker: str, fair_prob: float, quote: Quote, cfg: Config) -> Opportunity | None:
    """Best positive-EV side for a market, or None if neither clears the threshold."""
    yes = _evaluate_side("yes", fair_prob, quote.yes_ask, cfg)
    no = _evaluate_side("no", 1.0 - fair_prob, quote.no_ask, cfg)

    best = max(
        (o for o in (yes, no) if o is not None),
        key=lambda o: o.expected_value,
        default=None,
    )
    if best is None:
        return None
    return Opportunity(**{**best.__dict__, "ticker": ticker})
