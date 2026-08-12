"""Execution: turn an Opportunity into an order.

Two implementations behind one interface. ``DryRunExecutor`` records what it would
have done (state + paper ledger) and never touches the API - the default. The
``LiveExecutor`` places real maker-first limit orders via the Kalshi trading client,
and is only ever constructed when ``cfg.live_trading`` is explicitly enabled.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from .config import Config
from .edge import Opportunity
from .ledger import PaperLedger
from .scanner import event_prob_for_side
from .state import StateStore


@dataclass(frozen=True)
class ExecResult:
    ok: bool
    client_order_id: str
    detail: str
    kalshi_order_id: str | None = None


def _new_coid() -> str:
    # Kalshi wants a fresh uuid4 per order for idempotent retry dedup.
    return str(uuid.uuid4())


class Executor(Protocol):
    live: bool
    def place(self, opp: Opportunity, fair_yes: float, *, kind: str = "entry",
              note: str = "") -> ExecResult: ...
    def cancel(self, client_order_id: str, kalshi_order_id: str | None) -> ExecResult: ...


class DryRunExecutor:
    """Records intended orders to state + paper ledger. Places nothing."""

    live = False

    def __init__(self, cfg: Config, state: StateStore, ledger: PaperLedger, logger):
        self.cfg, self.state, self.ledger, self.log = cfg, state, ledger, logger

    def place(self, opp: Opportunity, fair_yes: float, *, kind: str = "entry",
              note: str = "") -> ExecResult:
        # Dry-run dedup: don't re-simulate a market we already have a paper position in
        # (paper no longer blocks via the live-default checks, so dedup here).
        if kind == "entry" and self.state.has_active_market(opp.ticker, statuses=("paper",)):
            return ExecResult(False, "", "paper dup (already simulated)")
        coid = _new_coid()
        self.state.record_order(
            client_order_id=coid, ticker=opp.ticker, side=opp.side, price=opp.price,
            contracts=opp.contracts, stake=opp.stake,
            entry_event_prob=event_prob_for_side(fair_yes, opp.side),
            status="paper", kind=kind, note=note,
        )
        self.ledger.record(opp, note=f"[{kind}] {note}")
        self.log.info("DRY-RUN %s: %s", kind, opp.describe())
        return ExecResult(True, coid, "paper")

    def cancel(self, client_order_id: str, kalshi_order_id: str | None) -> ExecResult:
        self.state.update_order_status(client_order_id, "cancelled")
        self.log.info("DRY-RUN cancel %s", client_order_id)
        return ExecResult(True, client_order_id, "cancelled (paper)")


class LiveExecutor:
    """Places real maker-first limit orders. Constructed only when live_trading=True."""

    live = True

    def __init__(self, cfg: Config, state: StateStore, ledger: PaperLedger, logger, client):
        if getattr(cfg, "research_only", False):
            # The advisory pivot: the bot advises, a human places the trade. Refusing in
            # the constructor means no code path can reach an order without this being off.
            raise RuntimeError(
                "research_only=True: PredictionEdge is in advisory mode and will not "
                "place orders. Set PE_RESEARCH_ONLY=0 to re-enable execution."
            )
        self.cfg, self.state, self.ledger, self.log = cfg, state, ledger, logger
        self.client = client  # LiveKalshiTradingClient

    def place(self, opp: Opportunity, fair_yes: float, *, kind: str = "entry",
              note: str = "") -> ExecResult:
        coid = _new_coid()
        price_cents = round(opp.price * 100)
        # Record as 'placed' BEFORE the call so a crash mid-flight is never silent.
        self.state.record_order(
            client_order_id=coid, ticker=opp.ticker, side=opp.side, price=opp.price,
            contracts=opp.contracts, stake=opp.stake,
            entry_event_prob=event_prob_for_side(fair_yes, opp.side),
            status="placed", kind=kind, note=note,
        )
        try:
            resp = self.client.create_order(
                ticker=opp.ticker, side=opp.side, action="buy",
                count=opp.contracts, price_cents=price_cents,
                client_order_id=coid, post_only=True,
            )
        except Exception as exc:  # noqa: BLE001 - we must not let one order kill the loop
            self.state.update_order_status(coid, "rejected")
            self.log.error("LIVE place FAILED %s: %s", opp.ticker, exc)
            return ExecResult(False, coid, f"error: {exc}")

        kalshi_id = (resp.get("order") or {}).get("order_id") or resp.get("order_id")
        self.state.update_order_status(coid, "open", kalshi_order_id=kalshi_id)
        self.ledger.record(opp, note=f"[LIVE {kind}] {note}")
        self.log.info("LIVE %s placed %s (kalshi_id=%s): %s", kind, coid, kalshi_id,
                      opp.describe())
        return ExecResult(True, coid, "placed", kalshi_id)

    def cancel(self, client_order_id: str, kalshi_order_id: str | None) -> ExecResult:
        try:
            if kalshi_order_id:
                self.client.cancel_order(kalshi_order_id)
            self.state.update_order_status(client_order_id, "cancelled")
            self.log.info("LIVE cancel %s (kalshi_id=%s)", client_order_id, kalshi_order_id)
            return ExecResult(True, client_order_id, "cancelled")
        except Exception as exc:  # noqa: BLE001
            self.log.error("LIVE cancel FAILED %s: %s", client_order_id, exc)
            return ExecResult(False, client_order_id, f"error: {exc}")
