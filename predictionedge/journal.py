"""A record of what you actually did with each suggestion.

The board can only earn trust if we can check it later, and since every trade is placed
by hand there is no fill feed to reconcile against - you are the source of truth. So the
board asks two questions: did you take this one, and how did it end?

That gives the one number that matters: of the tickets the board rated highly, how
many won, versus the ones you skipped. Without it we are back to guessing whether
whale-following works, which is exactly how the June batch happened.

Append-only JSONL at ``data/manual_journal.jsonl``; one row per decision, and a
later row updates an earlier one by ``id`` (last write wins).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

TAKEN = "taken"
SKIPPED = "skipped"
WON = "won"
LOST = "lost"


@dataclass
class JournalEntry:
    id: str                      # market_id + outcome, stable across refreshes
    ts: float
    title: str
    side_label: str
    entry_price: float
    contracts: int
    cost: float
    conviction: float
    status: str                  # taken | skipped | won | lost
    note: str = ""
    extra: dict = field(default_factory=dict)


class Journal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:  # noqa: BLE001 - a torn line must not kill the board
                continue
        return rows

    def record(self, entry: JournalEntry) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(entry)) + "\n")

    def latest(self) -> dict[str, dict]:
        """Current state per ticket id (last row wins)."""
        out: dict[str, dict] = {}
        for r in self._rows():
            rid = r.get("id")
            if rid:
                out[rid] = r
        return out

    def summary(self) -> dict:
        rows = list(self.latest().values())
        taken = [r for r in rows if r.get("status") in (TAKEN, WON, LOST)]
        won = [r for r in rows if r.get("status") == WON]
        lost = [r for r in rows if r.get("status") == LOST]
        settled = len(won) + len(lost)

        staked = sum(float(r.get("cost") or 0.0) for r in won + lost)
        returned = sum(float(r.get("contracts") or 0) for r in won)  # $1 per winning contract
        return {
            "logged": len(rows),
            "taken": len(taken),
            "skipped": sum(1 for r in rows if r.get("status") == SKIPPED),
            "open": len(taken) - settled,
            "won": len(won),
            "lost": len(lost),
            "hit_rate": round(len(won) / settled, 3) if settled else None,
            "staked": round(staked, 2),
            "pnl": round(returned - staked, 2) if settled else 0.0,
            "avg_conviction_taken": (
                round(sum(float(r.get("conviction") or 0) for r in taken) / len(taken), 3)
                if taken else None
            ),
        }


def entry_from_ticket(ticket, status: str, note: str = "") -> JournalEntry:
    return JournalEntry(
        id=f"{ticket.market_id}:{ticket.outcome}",
        ts=time.time(),
        title=ticket.title,
        side_label=ticket.side_label,
        entry_price=ticket.entry_price,
        contracts=ticket.contracts,
        cost=ticket.cost,
        conviction=ticket.conviction,
        status=status,
        note=note,
    )
