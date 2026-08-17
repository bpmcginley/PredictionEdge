"""Edge detection and position sizing.

Given a fair probability (de-vig consensus, optionally whale-blended) and the live
Kalshi prices, decide whether buying YES or NO carries positive expected value
*after fees*, and size it with fractional Kelly under hard caps.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import Config
from .fees import DEFAULT_FEE_MULTIPLIER, fee_per_contract, trade_fee


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
    # TAKER, unconditionally. `Quote` is built from the book's OFFERS, so the price
    # under test is an ask and a fill at it lifts it - and this line is a BAR, not
    # bookkeeping: it is the cost an edge has to clear before a ticket is published.
    # Reading the old `cfg.assume_maker` flag here (deleted 2026-08-17) was wrong twice
    # over.
    #
    #   - At 25% of taker the bar sat up to 1.31c/contract below the truth (the taker
    #     fee peaks at 1.75c/ct at the 50c price point), which is 44% of the 3c
    #     `min_edge`. A 3.4c gap at 50c therefore read as a 3c edge when it was a 1.7c
    #     one. Understating a fee in the accounting misstates a result; understating it
    #     HERE buys the marginal ticket, and it points the wrong way - the error only
    #     ever admits bets, never rejects them.
    #   - 25%-of-taker is not the maker rate on a standard market anyway. Kalshi
    #     charges a resting order nothing there; the quarter rate is its DESIGNATED
    #     series only (`fees.maker_trade_fee` / `fees.DESIGNATED_MAKER_SERIES`).
    #
    # `LiveExecutor` does submit `post_only`, so a fill genuinely can rest - and a rest
    # on a standard market is FREE, i.e. strictly cheaper than this bar. That is the
    # only safe direction for a threshold to be wrong in: an edge that exists only if
    # the queue is kind to you is not an edge you can act on.
    net_per_ct = win_prob - price - fee_per_contract(
        price, multiplier=cfg.fee_multiplier, maker=False
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

    # The same schedule the bar above was tested against. A reported fee that
    # disagreed with the gate's would put one number in `describe()` and the paper
    # ledger and a different one behind the decision that produced them.
    fee = trade_fee(price, contracts, multiplier=cfg.fee_multiplier, maker=False)
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


def copy_give_up_c(entry: float, whale_price: float, *,
                   multiplier: float = DEFAULT_FEE_MULTIPLIER) -> float:
    """Cents per contract handed away versus the fill being copied: drift PLUS the fee.

    The copy sleeve has no fair-value model. `find_edge` needs a `fair_prob` and there
    is nothing to hand it - which is why not one of the 293 published opinions ever
    passed through it, and why the whole sleeve reached the record with no fee bar at
    all. What it does have is the whale's own price: their money asserts fair is at
    least what they paid. That makes `board_max_drift_c` an edge budget measured from
    that floor - how far past the whale's asserted value we will chase, on the belief
    their information is worth more than they paid for it.

    Charged against that budget, the taker fee belongs in it. It is money gone the
    moment the fill lands, exactly like drift, and leaving it out let a ticket sit 5.75c
    worse than the trade it claimed to copy under a 4c cap. Because the fee peaks at 50c
    and shrinks toward the extremes, counting it also tightens the budget precisely
    where `fees` says it should: 4c buys 2.25c of drift at 50c and 3.4c at 90c.

    This is not the conviction score in another coat. Over the settled rows conviction
    predicts realised edge not at all - the 0.30 band (below the buy bar, recorded as a
    probe) ran +10.3 points/bet and the 0.40 band -1.0 - so scaling the budget by it
    would be fitting to noise. Price and drift are things we observe before the bet.

    Taker, unconditionally, for the reason `_evaluate_side` gives: this is a BAR, and a
    bar you clear only if the queue is kind to you is not a bar.
    """
    drift_c = (entry - whale_price) * 100.0
    if not 0.0 < entry < 1.0:
        # Unpriceable, so there is no fee to quote. Return the drift alone rather than
        # raising: the price-band gate owns this case and refuses it first.
        return drift_c
    return drift_c + 100.0 * fee_per_contract(entry, multiplier=multiplier, maker=False)


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
