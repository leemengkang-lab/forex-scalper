"""
reconcile.py
============
Position reconciliation: detect trades that closed at the broker (SL/TP fired
while we were running or while we were down) and rebuild risk state from broker
truth on startup.

No asyncio, no live-loop wiring — pure logic consumed by the live engine.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("reconcile")

# Callable that returns realized P&L for a closed trade, or None if unknown.
GetClosedPnl = Callable[[str], "float | None"]


# ---------------------------------------------------------------------------
# Structural protocols (avoid importing concrete classes as type annotations,
# since we consume repo/risk as duck-typed objects in tests)
# ---------------------------------------------------------------------------

@runtime_checkable
class _Repo(Protocol):
    def open_trades(self) -> list[dict[str, Any]]: ...
    def record_open(self, trade: Any, *, direction: int, risk_amount: float) -> None: ...
    def record_close(
        self,
        trade_id: str,
        *,
        exit_price: float,
        pnl: float,
        reason: str,
        closed_at: datetime,
    ) -> None: ...
    def is_halted(self) -> bool: ...
    def set_halt(self, reason: str) -> None: ...


@runtime_checkable
class _Risk(Protocol):
    halted: bool
    open_positions: list[Any]

    def close_position(self, instrument: str, pnl: float, now: datetime | None = None) -> None: ...
    def register_fill(self, instrument: str, direction: int, risk_amount: float) -> None: ...


@runtime_checkable
class _Broker(Protocol):
    def open_trades(self) -> list[Any]: ...


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class ReconcileSummary:
    closed: list[str] = field(default_factory=list)
    rebuilt_open: int = 0
    orphan_in_broker: list[str] = field(default_factory=list)
    halt_restored: bool = False


class PositionReconciler:
    def __init__(
        self,
        repo: _Repo,
        risk: _Risk,
        *,
        get_closed_pnl: GetClosedPnl,
        now_utc: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repo = repo
        self._risk = risk
        self._get_closed_pnl = get_closed_pnl
        self._now_utc = now_utc

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _close_one(self, trade_id: str, instrument: str, now: datetime) -> None:
        """Fetch P&L (with fallback), route to risk, and persist record_close."""
        pnl = self._get_closed_pnl(trade_id)
        if pnl is None:
            logger.warning(
                "reconcile: get_closed_pnl returned None for trade_id=%s; falling back to pnl=0.0",
                trade_id,
            )
            pnl = 0.0

        self._risk.close_position(instrument, pnl, now=now)
        self._repo.record_close(
            trade_id,
            exit_price=0.0,
            pnl=pnl,
            reason="broker_close",
            closed_at=now,
        )

        if self._risk.halted and not self._repo.is_halted():
            self._repo.set_halt("daily loss limit reached")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sync(self, broker: _Broker, *, now: datetime | None = None) -> list[str]:
        """Detect DB-open trades that are no longer open at the broker.

        For each closed trade: fetch realized P&L via get_closed_pnl (fallback
        0.0 + log if None), route to risk.close_position, and record_close in
        the DB. If risk becomes halted, persist repo.set_halt.

        Returns the list of closed trade_ids.
        """
        now = now or self._now_utc()

        broker_ids: set[str] = {t.trade_id for t in broker.open_trades()}
        db_trades: list[dict[str, Any]] = self._repo.open_trades()

        closed: list[str] = []
        for row in db_trades:
            tid: str = row["trade_id"]
            if tid not in broker_ids:
                self._close_one(tid, row["instrument"], now)
                closed.append(tid)

        return closed

    def reconcile_on_startup(
        self, broker: _Broker, *, now: datetime | None = None
    ) -> ReconcileSummary:
        """Called once at boot before trading begins.

        Steps:
        1. If repo.is_halted(): restore the kill switch by setting risk.halted = True.
        2. Compute broker open ids vs DB open ids.
        3. orphan_in_db (DB-open, gone at broker) = closed while we were down ->
           same handling as sync (close_position + record_close).
        4. orphan_in_broker (at broker, not in DB) -> persist via repo.record_open
           with risk_amount=0.0 so the DB matches reality.
        5. Rebuild risk open positions from BROKER truth: clear risk.open_positions,
           then for each broker open trade register_fill using the DB row's
           risk_amount (captured before recording orphan closes) or 0.0 for unknowns.

        Returns a ReconcileSummary.
        """
        now = now or self._now_utc()
        summary = ReconcileSummary()

        # Step 1: restore halt flag
        if self._repo.is_halted():
            self._risk.halted = True
            summary.halt_restored = True

        # Snapshot DB open trades BEFORE any closes (to preserve risk_amounts for rebuild)
        db_trades: list[dict[str, Any]] = self._repo.open_trades()
        db_by_id: dict[str, dict[str, Any]] = {row["trade_id"]: row for row in db_trades}

        broker_trades = broker.open_trades()
        broker_by_id: dict[str, Any] = {t.trade_id: t for t in broker_trades}

        db_ids: set[str] = set(db_by_id)
        broker_ids: set[str] = set(broker_by_id)

        # Step 3: orphan_in_db — closed while bot was down
        for tid in db_ids - broker_ids:
            row = db_by_id[tid]
            self._close_one(tid, row["instrument"], now)
            summary.closed.append(tid)

        # Step 4: orphan_in_broker — at broker but missing from DB
        for tid in broker_ids - db_ids:
            bt = broker_by_id[tid]
            direction = 1 if bt.units > 0 else -1
            self._repo.record_open(bt, direction=direction, risk_amount=0.0)
            summary.orphan_in_broker.append(tid)

        # Step 5: rebuild risk open positions from broker truth
        # Reassign to a fresh list (avoids mutation while iterating; also a clean state reset)
        self._risk.open_positions = []

        for bt in broker_trades:
            direction = 1 if bt.units > 0 else -1
            risk_amount = (
                db_by_id[bt.trade_id]["risk_amount"] if bt.trade_id in db_by_id else 0.0
            )
            self._risk.register_fill(bt.instrument, direction, risk_amount)
            summary.rebuilt_open += 1

        return summary
