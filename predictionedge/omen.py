"""Omen (omen.trade) venue rules - ADVISORY OUTPUT ONLY.

Omen funds prediction-market traders on simulated capital, pricing its markets off
*global* Polymarket (not Polymarket US). That is why this project's whale research
finally has a venue: the markets our signals come from are the markets Omen quotes.

IMPORTANT - why nothing here places an order. Omen's rules are explicit:

    "All trading on Omen must be placed manually by you, through the official
     Omen web or iOS app."
    "Omen does not offer a public API, and API access of any kind is not permitted."

Bots, scripts, browser automation, third-party tools and signal-following services
are all prohibited, and enforcement lands at withdrawal time (forfeited profits and
account closure). So this module models Omen's *sizing and eligibility rules* to
make human-placed trades well-formed; it deliberately exposes no order path.

Rules encoded (help centre, accounts opened after 2026-07-30):
  - every contract must be priced at or above $0.01
  - max 19 contracts per market, per $1,000 of account size
  - max 38 contracts per event, per $1,000 of account size
  - Omen charges no trading fee of its own; the underlying Polymarket market may
    charge one, and holding to resolution avoids it entirely
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MIN_CONTRACT_PRICE = 0.01
CONTRACTS_PER_MARKET_PER_1K = 19
CONTRACTS_PER_EVENT_PER_1K = 38


@dataclass(frozen=True)
class OmenAccount:
    """An Omen evaluation or funded account (balances are simulated capital)."""

    size: float = 100_000.0
    daily_loss_limit_fraction: float = 0.05   # "5% daily loss limit"
    per_trade_fraction: float = 0.01          # our own risk budget per idea, not Omen's

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
    capped_by: str            # "risk-budget" | "omen-market-limit" | "" (none)


def size_position(price: float, account: OmenAccount, *, conviction: float = 1.0,
                  already_on_event: int = 0) -> Sizing | None:
    """Contracts to buy at ``price``, respecting our risk budget and Omen's caps.

    ``conviction`` (0..1) scales the risk budget so a marginal idea stakes less than a
    strong one. ``already_on_event`` is contracts you already hold on the same event,
    which eats into Omen's per-event ceiling.
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
        contracts, capped_by = ceiling, "omen-market-limit"
    else:
        capped_by = "risk-budget"
    if contracts < 1:
        return None

    return Sizing(contracts=contracts, cost=round(contracts * price, 2),
                  max_payout=float(contracts), capped_by=capped_by)


def event_url(event_slug: str) -> str:
    """Polymarket event page - use it to eyeball the market before placing on Omen."""
    return f"https://polymarket.com/event/{event_slug}" if event_slug else ""
