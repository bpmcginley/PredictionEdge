"""Risk manager - the brakes on an unattended bot.

Two gates: ``preflight`` runs once per cycle (can we trade at all?), and ``vet``
runs per opportunity (is this specific order allowed?). Everything here is a hard
stop, not a suggestion - when in doubt it blocks.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config
    from .edge import Opportunity
    from .state import StateStore


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str = "ok"

    def __bool__(self) -> bool:
        return self.allowed


def _ok() -> RiskDecision:
    return RiskDecision(True)


def _block(reason: str) -> RiskDecision:
    return RiskDecision(False, reason)


class RiskManager:
    def __init__(self, cfg: "Config", state: "StateStore"):
        self.cfg = cfg
        self.state = state

    def killswitch_engaged(self) -> bool:
        return Path(self.cfg.killswitch_path).exists()

    def preflight(self) -> RiskDecision:
        """Whole-account gate, evaluated before any orders in a cycle."""
        if self.killswitch_engaged():
            return _block(f"killswitch file present ({self.cfg.killswitch_path})")
        # today_realized_pnl is negative when down; -max_daily_loss is the floor.
        if self.state.today_realized_pnl() <= -self.cfg.max_daily_loss:
            return _block(f"daily loss limit hit (${self.cfg.max_daily_loss:.2f})")
        if self.state.open_position_count() >= self.cfg.max_open_positions:
            return _block(f"max open positions reached ({self.cfg.max_open_positions})")
        if self.state.open_exposure() >= self.cfg.max_total_exposure:
            return _block(f"max total exposure reached (${self.cfg.max_total_exposure:.2f})")
        return _ok()

    def vet(self, opp: "Opportunity") -> RiskDecision:
        """Per-order gate."""
        if opp.contracts <= 0:
            return _block("zero size")
        if not (self.cfg.min_price <= opp.price <= self.cfg.max_price):
            return _block(f"price {opp.price:.2f} outside [{self.cfg.min_price}, "
                          f"{self.cfg.max_price}] band")
        # An edge that looks too good almost always means a bad data/matching feed,
        # not free money. Refuse it rather than fire size into a glitch.
        if opp.edge_per_contract > self.cfg.sane_edge_ceiling:
            return _block(f"edge {opp.edge_per_contract:.3f} exceeds sanity ceiling "
                          f"{self.cfg.sane_edge_ceiling} (likely data error)")
        if self.state.has_active_market(opp.ticker):
            return _block(f"already active in {opp.ticker}")
        if self.state.open_position_count() >= self.cfg.max_open_positions:
            return _block("max open positions reached")
        if self.state.open_exposure() + opp.stake > self.cfg.max_total_exposure:
            return _block(f"would breach exposure cap (${self.cfg.max_total_exposure:.2f})")
        return _ok()

    def vet_hedge(self, opp: "Opportunity") -> RiskDecision:
        """Lighter gate for a hedge order - it is deliberately on an already-active
        market, so the 'already active' check is skipped, but caps still apply."""
        if self.killswitch_engaged():
            return _block("killswitch file present")
        if opp.contracts <= 0:
            return _block("zero size")
        if not (self.cfg.min_price <= opp.price <= self.cfg.max_price):
            return _block(f"hedge price {opp.price:.2f} outside band")
        if self.state.open_exposure() + opp.stake > self.cfg.max_total_exposure:
            return _block("hedge would breach exposure cap")
        return _ok()

    def remaining_exposure_budget(self) -> float:
        return max(0.0, self.cfg.max_total_exposure - self.state.open_exposure())
