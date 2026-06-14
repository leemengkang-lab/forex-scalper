"""
persistence.py
==============
SQLite-backed repository for bot state that must survive restarts:

  * Open-trade ledger (records opened / closed positions)
  * Halt flag (persisted key-value state)
  * Daily equity snapshots
  * Append-only event log

Uses stdlib sqlite3 only — no ORM, no third-party deps.
All datetimes are stored as ISO-8601 UTC strings and returned as
tz-aware datetime objects.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from forex_scalper.execution import OpenTrade

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS trades (
    trade_id    TEXT PRIMARY KEY,
    instrument  TEXT NOT NULL,
    direction   INTEGER NOT NULL,
    units       INTEGER NOT NULL,
    entry       REAL NOT NULL,
    stop        REAL NOT NULL,
    take_profit REAL NOT NULL,
    risk_amount REAL NOT NULL,
    setup       TEXT NOT NULL,
    opened_at   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    exit_price  REAL,
    pnl         REAL,
    exit_reason TEXT,
    closed_at   TEXT
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS day_snapshots (
    day           TEXT PRIMARY KEY,
    start_balance REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_utc(dt: datetime) -> datetime:
    """Return a tz-aware UTC datetime; treat naive as UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _iso(dt: datetime) -> str:
    return _to_utc(dt).isoformat()


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(UTC)


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

class Repository:
    """Wraps a single SQLite connection; autocommit (isolation_level=None)."""

    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._apply_schema()

    def _apply_schema(self) -> None:
        self._conn.executescript(_DDL)

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------

    def record_open(
        self, trade: OpenTrade, *, direction: int, risk_amount: float
    ) -> None:
        """Insert (or replace) a trade row with status='open'."""
        self._conn.execute(
            """
            INSERT OR REPLACE INTO trades
                (trade_id, instrument, direction, units, entry, stop, take_profit,
                 risk_amount, setup, opened_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
            """,
            (
                trade.trade_id,
                trade.instrument,
                direction,
                trade.units,
                trade.entry_price,
                trade.stop_price,
                trade.take_profit,
                risk_amount,
                trade.setup,
                _iso(trade.opened_at),
            ),
        )

    def record_close(
        self,
        trade_id: str,
        *,
        exit_price: float,
        pnl: float,
        reason: str,
        closed_at: datetime,
    ) -> None:
        """Mark a trade as closed."""
        self._conn.execute(
            """
            UPDATE trades
            SET status='closed', exit_price=?, pnl=?, exit_reason=?, closed_at=?
            WHERE trade_id=?
            """,
            (exit_price, pnl, reason, _iso(closed_at), trade_id),
        )

    def open_trades(self) -> list[dict]:  # type: ignore[type-arg]
        """Return all open trade rows as dicts (opened_at is a datetime)."""
        rows = self._conn.execute(
            """
            SELECT trade_id, instrument, direction, units, entry, stop,
                   take_profit, risk_amount, setup, opened_at
            FROM trades
            WHERE status = 'open'
            """
        ).fetchall()
        out: list[dict] = []  # type: ignore[type-arg]
        for r in rows:
            d = dict(r)
            d["opened_at"] = _parse_dt(d["opened_at"])
            out.append(d)
        return out

    def last_close(self) -> tuple[datetime | None, bool | None]:
        """
        Return (closed_at, was_loss) of the most recently closed trade,
        or (None, None) if no closed trades exist.
        """
        row = self._conn.execute(
            """
            SELECT closed_at, pnl
            FROM trades
            WHERE status = 'closed'
            ORDER BY closed_at DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None, None
        closed_at = _parse_dt(row["closed_at"])
        was_loss = row["pnl"] < 0
        return closed_at, was_loss

    # ------------------------------------------------------------------
    # Halt flag
    # ------------------------------------------------------------------

    def set_halt(self, reason: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO state (key, value) VALUES ('halted', '1')"
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO state (key, value) VALUES ('halt_reason', ?)",
            (reason,),
        )

    def clear_halt(self) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO state (key, value) VALUES ('halted', '0')"
        )
        self._conn.execute("DELETE FROM state WHERE key = 'halt_reason'")

    def is_halted(self) -> bool:
        row = self._conn.execute(
            "SELECT value FROM state WHERE key = 'halted'"
        ).fetchone()
        return row is not None and row["value"] == "1"

    def halt_reason(self) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM state WHERE key = 'halt_reason'"
        ).fetchone()
        return row["value"] if row is not None else None

    # ------------------------------------------------------------------
    # Daily snapshots
    # ------------------------------------------------------------------

    def upsert_day(self, day: str, start_balance: float) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO day_snapshots (day, start_balance) VALUES (?, ?)",
            (day, start_balance),
        )

    def day_start_balance(self, day: str) -> float | None:
        row = self._conn.execute(
            "SELECT start_balance FROM day_snapshots WHERE day = ?", (day,)
        ).fetchone()
        return float(row["start_balance"]) if row is not None else None

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def append_event(
        self,
        kind: str,
        payload: dict,  # type: ignore[type-arg]
        *,
        at: datetime | None = None,
    ) -> None:
        ts = _iso(at if at is not None else datetime.now(UTC))
        self._conn.execute(
            "INSERT INTO events (at, kind, payload) VALUES (?, ?, ?)",
            (ts, kind, json.dumps(payload)),
        )

    def recent_events(self, limit: int = 50) -> list[dict]:  # type: ignore[type-arg]
        """Return up to *limit* events, newest first."""
        rows = self._conn.execute(
            "SELECT at, kind, payload FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out: list[dict] = []  # type: ignore[type-arg]
        for r in rows:
            out.append(
                {
                    "at": _parse_dt(r["at"]),
                    "kind": r["kind"],
                    "payload": json.loads(r["payload"]),
                }
            )
        return out
