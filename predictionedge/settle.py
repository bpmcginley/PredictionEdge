"""Settlement - close out resolved markets and label the bet for the backtest.

Binary contracts settle to $1 (winning side) or $0. Each cycle this checks every
active order's market; when it has settled, it books realised PnL (feeding the
daily-loss breaker and the ledger) and appends a labelled record to the settled-bets
dataset that the backtest validates against.
"""

from __future__ import annotations

import json
from pathlib import Path

from .fees import is_designated, maker_trade_fee, trade_fee

# Kalshi does not return the single string this module originally tested for. A market
# that has paid out reports `finalized`; `settled` was never observed in production.
# The consequence was total and silent: `settle_positions` matched nothing, ever, so
# `data/settled_bets.jsonl` was never created, so `backtest.py` - Deflated Sharpe, PSR,
# block bootstrap, calibration, the whole walk-forward gate - has never had an input
# file. Every performance question about this project was unanswerable because of this
# one comparison.
#
# It is a SET rather than a corrected literal so that a vocabulary change costs nothing,
# and `result` carries the real decision: a market with result yes/no has been called.
_TERMINAL_STATUSES = frozenset({"finalized", "settled", "determined"})


def rested_and_filled(row) -> bool:
    """Did this position EARN a maker discount, or is that merely being assumed?

    The same question `papertrial.rested_and_filled` asks of a trial row, answered by
    the same rule: the discount needs evidence of a resting fill and is never a default.
    The `orders` table (state.py) records none - a row goes placed -> open -> settled
    and never says how it filled - so this is False on every row that exists today and
    settlement charges the taker fee.

    It is a function rather than a `False` constant because the day a fill event is
    recorded, this is the one place that has to learn about it; what it must never
    become again is a flat config flag that answers yes for every position at once.
    """
    keys = row.keys() if hasattr(row, "keys") else ()
    return "maker" in keys and bool(row["maker"])


def settle_positions(cfg, state, kalshi, ledger, log) -> dict:
    """Settle any resolved active orders. Returns {settled, realized}."""
    settled = 0
    realized_total = 0.0
    bets_path = Path(cfg.settled_path)
    bets_path.parent.mkdir(parents=True, exist_ok=True)

    for row in state.active_orders():
        market = kalshi.market(row["ticker"])
        if market is None or market.result not in ("yes", "no"):
            continue
        if market.status not in _TERMINAL_STATUSES:
            # Decided but wearing a status we do not recognise. Skipping is the safe
            # half - we do not book cash we are unsure of - but doing it QUIETLY is the
            # bug that cost seven weeks, so say it out loud. An unknown terminal string
            # now surfaces within one cycle instead of never.
            log.warning(
                "%s has result %r but unrecognised status %r - not settling. If Kalshi "
                "renamed a terminal status, add it to settle._TERMINAL_STATUSES.",
                row["ticker"], market.result, market.status,
            )
            continue

        won = row["side"] == market.result
        price, contracts, stake = row["price"], row["contracts"], row["stake"]
        # TAKER unless the fill is KNOWN to have rested. A `cfg.assume_maker` flag decided
        # this before (deleted 2026-08-17); it was wrong twice: entries are at the ask, and
        # 25%-of-taker
        # is Kalshi's DESIGNATED-series maker rate - a resting fill on a standard market
        # costs nothing at all, which is why the earned branch goes through
        # `maker_trade_fee` rather than `trade_fee(maker=True)`.
        #
        # This one is not only a label on the record. It lands in `realized`, which goes
        # to `add_realized` and therefore into the `max_daily_loss` breaker and into the
        # labelled dataset `backtest.py` scores. A fee booked at a quarter of its size
        # makes the brakes late and the evidence flattering, in that order.
        if rested_and_filled(row):
            fee = maker_trade_fee(price, contracts, multiplier=cfg.fee_multiplier,
                                  designated=is_designated(row["ticker"]))
        else:
            fee = trade_fee(price, contracts, multiplier=cfg.fee_multiplier, maker=False)
        # Win: receive $1/contract, having staked `stake`. Loss: forfeit the stake.
        realized = (contracts * (1.0 - price) - fee) if won else (-stake - fee)

        state.add_realized(realized)
        state.update_order_status(row["client_order_id"], "settled")
        ledger.resolve(row["ticker"], market.result == "yes")
        _append(bets_path, {
            "ticker": row["ticker"],
            "kind": row["kind"],
            "pred": row["entry_event_prob"],   # our P(this side wins) at entry
            "price": price,
            "contracts": contracts,
            "stake": round(stake, 4),
            "fee": fee,
            "won": won,
            "realized": round(realized, 4),
        })
        settled += 1
        realized_total += realized

    if settled:
        log.info("settled %d position(s), realised $%.2f", settled, realized_total)
    return {"settled": settled, "realized": round(realized_total, 2)}


def _append(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
