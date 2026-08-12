"""House sizing rules - ADVISORY OUTPUT ONLY.

This module turns a price and a risk budget into a contract count for the manual
trade board. It deliberately exposes no order path: the board ranks and explains,
a human decides and places. Going live is a separate decision behind its own
deploy gate, not something this module can reach.

Rules encoded:
  - every contract must be priced at or above $0.01
  - max 19 contracts per market, per $1,000 of account size
  - max 38 contracts per event, per $1,000 of account size
  - a 5% daily-loss guardrail on the account

The per-market/per-event caps started life as a prop-firm's account rules; the
venue is gone but the caps survive because they are sane concentration limits
for an uncalibrated signal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MIN_CONTRACT_PRICE = 0.01
CONTRACTS_PER_MARKET_PER_1K = 19
CONTRACTS_PER_EVENT_PER_1K = 38


@dataclass(frozen=True)
class Account:
    """The capital the board sizes against (paper unless you decide otherwise)."""

    size: float = 100_000.0
    daily_loss_limit_fraction: float = 0.05   # house 5% daily-loss guardrail
    per_trade_fraction: float = 0.01          # our own risk budget per idea

    @property
    def max_contracts_per_market(self) -> int:
        return int(self.size / 1000.0 * CONTRACTS_PER_MARKET_PER_1K)

    @property
    def max_contracts_per_event(self) -> int:
        return int(self.size / 1000.0 * CONTRACTS_PER_EVENT_PER_1K)

    @property
    def daily_loss_limit(self) -> float:
        return self.size * self.daily_loss_limit_fraction


@dataclass(frozen=True)
class Sizing:
    contracts: int
    cost: float               # what you pay to open (contracts * price)
    max_payout: float         # contracts * $1 at resolution
    capped_by: str            # "risk-budget" | "per-market-limit" | "" (none)


def size_position(price: float, account: Account, *, conviction: float = 1.0,
                  already_on_event: int = 0) -> Sizing | None:
    """Contracts to buy at ``price``, respecting our risk budget and the house caps.

    ``conviction`` (0..1) scales the risk budget so a marginal idea stakes less than a
    strong one. ``already_on_event`` is contracts you already hold on the same event,
    which eats into the per-event ceiling.
    """
    if not (MIN_CONTRACT_PRICE <= price < 1.0) or account.size <= 0:
        return None
    conviction = max(0.0, min(1.0, conviction))
    if conviction <= 0:
        return None

    budget = account.size * account.per_trade_fraction * conviction
    want = int(math.floor(budget / price))
    if want < 1:
        return None

    capped_by = ""
    event_room = max(0, account.max_contracts_per_event - already_on_event)
    ceiling = min(account.max_contracts_per_market, event_room)
    contracts = want
    if contracts > ceiling:
        contracts, capped_by = ceiling, "per-market-limit"
    else:
        capped_by = "risk-budget"
    if contracts < 1:
        return None

    return Sizing(contracts=contracts, cost=round(contracts * price, 2),
                  max_payout=float(contracts), capped_by=capped_by)


def event_url(event_slug: str) -> str:
    """Polymarket event page - use it to eyeball the market before acting on a ticket."""
    return f"https://polymarket.com/event/{event_slug}" if event_slug else ""
