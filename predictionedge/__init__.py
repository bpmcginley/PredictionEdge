"""PredictionEdge - a read-only, fee-aware edge detector for Kalshi prediction markets.

Primary edge: fade Kalshi prices against de-vigged sharp sportsbook consensus.
Supporting signal: smart-money / whale flow read from public Polymarket on-chain data.

This package is read-only by design. It detects and paper-trades opportunities;
it does not place live orders. Live execution is a deliberately separate, gated phase.
"""

__version__ = "0.1.0"
