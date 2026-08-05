"""Durable state for an unattended bot - SQLite (stdlib, no deps).

Tracks every order the bot places (so a restart never double-fills or forgets an
open position), realised P&L per day (for the daily-loss circuit breaker), and a
small key/value meta table (heartbeat, demo-run gate). Order rows also store the
fair probability at entry, which the arbitrage review needs to compute the shift q.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Statuses that still tie up capital / count as "in the market".
ACTIVE_STATUSES = ("paper", "placed", "open", "partial", "filled")
# Only REAL positions count toward risk caps + dedup. Paper (dry-run simulation) must
# never block a live order or consume the live exposure budget.
LIVE_STATUSES = ("placed", "open", "partial", "filled")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                client_order_id TEXT PRIMARY KEY,
                ts              TEXT NOT NULL,
                ticker          TEXT NOT NULL,
                side            TEXT NOT NULL,
                price           REAL NOT NULL,
                contracts       INTEGER NOT NULL,
                stake           REAL NOT NULL,
                entry_event_prob REAL NOT NULL,
                kind            TEXT NOT NULL DEFAULT 'entry',
                status          TEXT NOT NULL,
                kalshi_order_id TEXT,
                note            TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_orders_ticker ON orders(ticker);
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

            CREATE TABLE IF NOT EXISTS pnl (
                date     TEXT PRIMARY KEY,
                realized REAL NOT NULL DEFAULT 0.0
            );

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        self.db.commit()

    # --- orders -------------------------------------------------------------
    def record_order(
        self, *, client_order_id: str, ticker: str, side: str, price: float,
        contracts: int, stake: float, entry_event_prob: float, status: str,
        kind: str = "entry", kalshi_order_id: str | None = None, note: str = "",
    ) -> None:
        self.db.execute(
            """INSERT OR REPLACE INTO orders
               (client_order_id, ts, ticker, side, price, contracts, stake,
                entry_event_prob, kind, status, kalshi_order_id, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (client_order_id, _utc_now(), ticker, side, price, contracts, stake,
             entry_event_prob, kind, status, kalshi_order_id, note),
        )
        self.db.commit()

    def update_order_status(
        self, client_order_id: str, status: str, kalshi_order_id: str | None = None
    ) -> None:
        if kalshi_order_id is not None:
            self.db.execute(
                "UPDATE orders SET status=?, kalshi_order_id=? WHERE client_order_id=?",
                (status, kalshi_order_id, client_order_id),
            )
        else:
            self.db.execute(
                "UPDATE orders SET status=? WHERE client_order_id=?",
                (status, client_order_id),
            )
        self.db.commit()

    def recent_orders(self, limit: int = 300) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM orders ORDER BY ts DESC LIMIT ?", (limit,)))

    def active_orders(self) -> list[sqlite3.Row]:
        q = ",".join("?" * len(ACTIVE_STATUSES))
        return list(self.db.execute(
            f"SELECT * FROM orders WHERE status IN ({q})", ACTIVE_STATUSES
        ))

    def has_active_market(self, ticker: str, statuses=LIVE_STATUSES) -> bool:
        q = ",".join("?" * len(statuses))
        row = self.db.execute(
            f"SELECT 1 FROM orders WHERE ticker=? AND status IN ({q}) LIMIT 1",
            (ticker, *statuses),
        ).fetchone()
        return row is not None

    def open_exposure(self, statuses=LIVE_STATUSES) -> float:
        q = ",".join("?" * len(statuses))
        row = self.db.execute(
            f"SELECT COALESCE(SUM(stake),0) AS s FROM orders WHERE status IN ({q})",
            statuses,
        ).fetchone()
        return float(row["s"])

    def open_position_count(self, statuses=LIVE_STATUSES) -> int:
        q = ",".join("?" * len(statuses))
        row = self.db.execute(
            f"SELECT COUNT(DISTINCT ticker) AS c FROM orders WHERE status IN ({q})",
            statuses,
        ).fetchone()
        return int(row["c"])

    # --- pnl ----------------------------------------------------------------
    def add_realized(self, amount: float, date: str | None = None) -> None:
        date = date or _utc_date()
        self.db.execute(
            """INSERT INTO pnl(date, realized) VALUES(?, ?)
               ON CONFLICT(date) DO UPDATE SET realized = realized + excluded.realized""",
            (date, amount),
        )
        self.db.commit()

    def today_realized_pnl(self) -> float:
        row = self.db.execute(
            "SELECT realized FROM pnl WHERE date=?", (_utc_date(),)
        ).fetchone()
        return float(row["realized"]) if row else 0.0

    # --- meta ---------------------------------------------------------------
    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            """INSERT INTO meta(key, value) VALUES(?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, value),
        )
        self.db.commit()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def close(self) -> None:
        self.db.close()
