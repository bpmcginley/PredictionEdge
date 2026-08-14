"""Paper-trade ledger - append-only JSONL of detected/executed (paper) opportunities.

Read-only project: "execution" here means recording what we *would* have done so
the edge can be measured against real resolutions before any capital is at risk.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .edge import Opportunity


class PaperLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, opp: Opportunity, note: str = "", extra: dict | None = None) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "status": "open",
            "ticker": opp.ticker,
            "side": opp.side,
            "price": opp.price,
            "contracts": opp.contracts,
            "stake": round(opp.stake, 4),
            "est_fee": opp.est_fee,
            "fair_prob": round(opp.fair_prob, 4),
            "edge_per_contract": round(opp.edge_per_contract, 4),
            "expected_value": round(opp.expected_value, 4),
            "note": note,
        }
        # Sleeve metadata carried verbatim (the sports sleeve's fair_mult /
        # fair_power / fair_shin A/B fields arrive this way). setdefault, not
        # update: a stray key may annotate a row, never falsify its accounting.
        for k, v in (extra or {}).items():
            entry.setdefault(k, v)
        self._append(entry)

    def resolve(self, ticker: str, yes_won: bool) -> None:
        """Record a market resolution so realised PnL can be reconciled later."""
        self._append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "status": "resolved",
            "ticker": ticker,
            "yes_won": yes_won,
        })

    def _append(self, entry: dict) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def summary(self) -> dict:
        if not self.path.exists():
            return {"trades": 0, "total_stake": 0.0, "total_expected_value": 0.0}
        trades = stake = ev = 0.0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("status") == "open":
                trades += 1
                stake += row.get("stake", 0.0)
                ev += row.get("expected_value", 0.0)
        return {
            "trades": int(trades),
            "total_stake": round(stake, 2),
            "total_expected_value": round(ev, 2),
        }
