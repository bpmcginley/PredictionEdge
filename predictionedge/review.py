"""Order review - revisit resting orders when the odds have moved.

Runs at the top of every cycle. For each still-active order it recomputes the
event's current probability, derives the shift factor ``q`` versus entry, and:

  * cancels an unfilled maker order whose probability has moved against it past
    ``stale_cancel_band`` (adverse-selection / pick-off protection); and
  * when the move is in our favour (``q >= 1``), uses the closed-form
    ``optimal_arbitrage_orders`` to size a hedge on the inverse side and places it
    if the locked-in slack ratio clears ``arb_min_ratio``.

This is the "check in on old orders in the future" behaviour from the spec.
"""

from __future__ import annotations

import math

from .arbitrage import optimal_arbitrage_orders
from .config import Config
from .edge import Opportunity
from .fairvalue import blended_fair_prob
from .fees import trade_fee
from .matching import DEFAULT_LINKS, orient
from .odds import consensus_fair_prob
from .scanner import event_prob_for_side

_LINK_BY_TICKER = {link.kalshi_ticker: link for link in DEFAULT_LINKS}


def review_open_orders(cfg, state, kalshi, odds, whales, executor, risk, log) -> int:
    """Returns the number of review actions (cancels + hedges) taken."""
    actions = 0
    active = state.active_orders()
    if not active:
        return 0

    events = {e.event_id: e for e in odds.events()}

    for row in active:
        # Only entry orders are reviewed; hedges are deliberate lock-ins, left alone.
        if row["kind"] != "entry":
            continue
        ticker = row["ticker"]
        link = _LINK_BY_TICKER.get(ticker)
        if link is None:
            continue
        event = events.get(link.event_id)
        if event is None:
            continue
        p0 = consensus_fair_prob(event, cfg.devig_method)
        if p0 is None:
            continue
        fair_yes_now = blended_fair_prob(
            orient(p0, link), whales.signal_for(ticker), cfg.whale_weight
        )

        a_entry = row["entry_event_prob"]
        a_now = event_prob_for_side(fair_yes_now, row["side"])
        if a_entry <= 0.0:
            continue
        q = a_now / a_entry

        # 1) Stale-quote protection: unfilled maker order, probability moved against us.
        if row["status"] in ("placed", "open") and q < 1.0 - cfg.stale_cancel_band:
            res = executor.cancel(row["client_order_id"], row["kalshi_order_id"])
            if res.ok:
                actions += 1
                log.info("REVIEW cancel stale %s on %s (q=%.3f, A %.3f->%.3f)",
                         row["client_order_id"], ticker, q, a_entry, a_now)
            continue

        # 2) Favourable move -> hedge the inverse side, if we haven't already.
        if not cfg.arb_enabled:
            continue
        if _already_hedged(state, ticker):
            continue
        try:
            plan = optimal_arbitrage_orders(x=row["stake"], A=a_entry, q=q)
        except ValueError:
            continue
        if not plan["feasible"] or not plan["z"] or plan["r_max"] is None:
            continue
        if plan["r_max"] < cfg.arb_min_ratio:
            continue

        hedge = _build_hedge(cfg, kalshi, row["side"], ticker, plan["z"])
        if hedge is None:
            continue
        decision = risk.vet_hedge(hedge)
        if not decision.allowed:
            log.info("REVIEW hedge blocked on %s: %s", ticker, decision.reason)
            continue
        # fair_yes for the hedge's own side accounting
        res = executor.place(hedge, fair_yes_now, kind="hedge",
                             note=f"arb r_max={plan['r_max']:.3f} case={plan['case']}")
        if res.ok:
            actions += 1
            log.info("REVIEW hedge %s on %s: z=$%.2f -> %dct @ %.2f (r_max=%.3f)",
                     hedge.side, ticker, plan["z"], hedge.contracts, hedge.price,
                     plan["r_max"])

    return actions


def _already_hedged(state, ticker: str) -> bool:
    return any(r["ticker"] == ticker and r["kind"] == "hedge" for r in state.active_orders())


def _build_hedge(cfg: Config, kalshi, entry_side: str, ticker: str, z_dollars: float):
    """Size an inverse-side hedge of ~z dollars at the current inverse ask."""
    market = kalshi.market(ticker)
    if market is None:
        return None
    hedge_side = "no" if entry_side == "yes" else "yes"
    price = market.no_ask if hedge_side == "no" else market.yes_ask
    if not (cfg.min_price <= price <= cfg.max_price):
        return None
    contracts = min(int(math.floor(z_dollars / price)), cfg.max_contracts)
    if contracts <= 0:
        return None
    fee = trade_fee(price, contracts, multiplier=cfg.fee_multiplier, maker=cfg.assume_maker)
    return Opportunity(
        ticker=ticker, side=hedge_side, fair_prob=0.0, price=price,
        edge_per_contract=0.0, contracts=contracts, stake=contracts * price,
        est_fee=fee, expected_value=0.0,
    )
