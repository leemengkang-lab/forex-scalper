"""
tests/test_persistence.py
=========================
TDD suite for Repository (SQLite persistence layer).

All tests use pytest's tmp_path fixture so each test gets an isolated
DB file.  Connections are explicitly closed after each write-then-reopen
sequence to avoid Windows file-lock issues on teardown.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from forex_scalper.execution import OpenTrade
from forex_scalper.persistence import Repository

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trade(tid: str = "101", units: int = 1000) -> OpenTrade:
    return OpenTrade(
        trade_id=tid,
        instrument="EUR_USD",
        units=units,
        entry_price=1.0850,
        stop_price=1.0838,
        take_profit=1.0890,
        opened_at=datetime(2025, 1, 6, 13, 30, tzinfo=UTC),
        setup="A",
    )


# ---------------------------------------------------------------------------
# Test 1: record_open + open_trades
# ---------------------------------------------------------------------------

def test_record_open_returns_correct_row(tmp_path):
    repo = Repository(tmp_path / "bot.db")
    try:
        repo.record_open(_trade(), direction=1, risk_amount=100.0)
        rows = repo.open_trades()
        assert len(rows) == 1
        row = rows[0]
        assert row["trade_id"] == "101"
        assert row["instrument"] == "EUR_USD"
        assert row["units"] == 1000
        assert row["direction"] == 1
        assert row["risk_amount"] == pytest.approx(100.0)
        assert row["setup"] == "A"
        # opened_at should come back as a tz-aware datetime
        assert isinstance(row["opened_at"], datetime)
        assert row["opened_at"].tzinfo is not None
        assert row["opened_at"] == datetime(2025, 1, 6, 13, 30, tzinfo=UTC)
    finally:
        repo.close()


def test_open_trades_empty_on_fresh_db(tmp_path):
    repo = Repository(tmp_path / "bot.db")
    try:
        assert repo.open_trades() == []
    finally:
        repo.close()


# ---------------------------------------------------------------------------
# Test 2: record_close moves trade out of open_trades
# ---------------------------------------------------------------------------

def test_open_then_close(tmp_path):
    repo = Repository(tmp_path / "bot.db")
    try:
        repo.record_open(_trade(), direction=1, risk_amount=100.0)
        rows = repo.open_trades()
        assert len(rows) == 1 and rows[0]["units"] == 1000

        repo.record_close(
            "101",
            exit_price=1.0890,
            pnl=12.5,
            reason="target",
            closed_at=datetime(2025, 1, 6, 13, 45, tzinfo=UTC),
        )
        assert repo.open_trades() == []
    finally:
        repo.close()


def test_record_close_stores_fields(tmp_path):
    """Closing a trade stores pnl, exit_price, reason."""
    repo = Repository(tmp_path / "bot.db")
    try:
        repo.record_open(_trade(), direction=1, risk_amount=100.0)
        repo.record_close(
            "101",
            exit_price=1.0890,
            pnl=12.5,
            reason="target",
            closed_at=datetime(2025, 1, 6, 13, 45, tzinfo=UTC),
        )
        # Verify via last_close
        closed_at, was_loss = repo.last_close()
        assert closed_at == datetime(2025, 1, 6, 13, 45, tzinfo=UTC)
        assert was_loss is False
    finally:
        repo.close()


# ---------------------------------------------------------------------------
# Test 3: halt flag persists across reopen
# ---------------------------------------------------------------------------

def test_halt_flag_survives_reopen(tmp_path):
    p = tmp_path / "bot.db"
    repo1 = Repository(p)
    repo1.set_halt("daily limit")
    repo1.close()

    repo2 = Repository(p)
    try:
        assert repo2.is_halted() is True
        assert repo2.halt_reason() == "daily limit"
    finally:
        repo2.close()


def test_clear_halt_survives_reopen(tmp_path):
    p = tmp_path / "bot.db"
    repo1 = Repository(p)
    repo1.set_halt("daily limit")
    repo1.clear_halt()
    repo1.close()

    repo2 = Repository(p)
    try:
        assert repo2.is_halted() is False
        assert repo2.halt_reason() is None
    finally:
        repo2.close()


def test_is_halted_false_on_fresh_db(tmp_path):
    repo = Repository(tmp_path / "bot.db")
    try:
        assert repo.is_halted() is False
        assert repo.halt_reason() is None
    finally:
        repo.close()


# ---------------------------------------------------------------------------
# Test 4: last_close returns most recently closed trade
# ---------------------------------------------------------------------------

def test_last_close_none_when_no_closed_trades(tmp_path):
    repo = Repository(tmp_path / "bot.db")
    try:
        assert repo.last_close() == (None, None)
    finally:
        repo.close()


def test_last_close_returns_latest_and_correct_was_loss(tmp_path):
    repo = Repository(tmp_path / "bot.db")
    try:
        # Trade 1: loss, closed earlier
        repo.record_open(_trade("T1"), direction=1, risk_amount=50.0)
        repo.record_close(
            "T1",
            exit_price=1.0838,
            pnl=-5.0,
            reason="stop",
            closed_at=datetime(2025, 1, 6, 13, 0, tzinfo=UTC),
        )
        # Trade 2: win, closed later
        repo.record_open(_trade("T2"), direction=1, risk_amount=50.0)
        repo.record_close(
            "T2",
            exit_price=1.0890,
            pnl=12.5,
            reason="target",
            closed_at=datetime(2025, 1, 6, 14, 0, tzinfo=UTC),
        )

        closed_at, was_loss = repo.last_close()
        assert closed_at == datetime(2025, 1, 6, 14, 0, tzinfo=UTC)
        assert was_loss is False
    finally:
        repo.close()


def test_last_close_loss_detection(tmp_path):
    repo = Repository(tmp_path / "bot.db")
    try:
        repo.record_open(_trade("T1"), direction=-1, risk_amount=50.0)
        repo.record_close(
            "T1",
            exit_price=1.0870,
            pnl=-8.0,
            reason="stop",
            closed_at=datetime(2025, 1, 6, 15, 0, tzinfo=UTC),
        )
        _, was_loss = repo.last_close()
        assert was_loss is True
    finally:
        repo.close()


# ---------------------------------------------------------------------------
# Test 5: upsert_day + day_start_balance
# ---------------------------------------------------------------------------

def test_day_snapshot_round_trip(tmp_path):
    repo = Repository(tmp_path / "bot.db")
    try:
        repo.upsert_day("2025-01-06", 1000.0)
        assert repo.day_start_balance("2025-01-06") == pytest.approx(1000.0)
    finally:
        repo.close()


def test_day_snapshot_upsert_overwrites(tmp_path):
    repo = Repository(tmp_path / "bot.db")
    try:
        repo.upsert_day("2025-01-06", 1000.0)
        repo.upsert_day("2025-01-06", 1050.0)
        assert repo.day_start_balance("2025-01-06") == pytest.approx(1050.0)
    finally:
        repo.close()


def test_day_snapshot_missing_returns_none(tmp_path):
    repo = Repository(tmp_path / "bot.db")
    try:
        assert repo.day_start_balance("2099-01-01") is None
    finally:
        repo.close()


# ---------------------------------------------------------------------------
# Test 6: append_event + recent_events
# ---------------------------------------------------------------------------

def test_events_round_trip(tmp_path):
    repo = Repository(tmp_path / "bot.db")
    try:
        t = datetime(2025, 1, 6, 13, 0, tzinfo=UTC)
        repo.append_event("trade_open", {"id": "101", "pnl": None}, at=t)
        events = repo.recent_events()
        assert len(events) == 1
        ev = events[0]
        assert ev["kind"] == "trade_open"
        assert ev["payload"] == {"id": "101", "pnl": None}
        assert isinstance(ev["at"], datetime)
        assert ev["at"] == t
    finally:
        repo.close()


def test_recent_events_newest_first(tmp_path):
    repo = Repository(tmp_path / "bot.db")
    try:
        t1 = datetime(2025, 1, 6, 13, 0, tzinfo=UTC)
        t2 = datetime(2025, 1, 6, 14, 0, tzinfo=UTC)
        repo.append_event("first", {"seq": 1}, at=t1)
        repo.append_event("second", {"seq": 2}, at=t2)
        events = repo.recent_events()
        assert events[0]["kind"] == "second"
        assert events[1]["kind"] == "first"
    finally:
        repo.close()


def test_recent_events_limit(tmp_path):
    repo = Repository(tmp_path / "bot.db")
    try:
        t = datetime(2025, 1, 6, 13, 0, tzinfo=UTC)
        for i in range(10):
            repo.append_event("ping", {"i": i}, at=t)
        events = repo.recent_events(limit=3)
        assert len(events) == 3
    finally:
        repo.close()


# ---------------------------------------------------------------------------
# Test 7: open_trades on fresh DB
# ---------------------------------------------------------------------------

def test_open_trades_empty_after_fresh_open(tmp_path):
    repo = Repository(tmp_path / "bot.db")
    try:
        result = repo.open_trades()
        assert result == []
    finally:
        repo.close()


# ---------------------------------------------------------------------------
# Bonus: negative units (short trade) stored correctly
# ---------------------------------------------------------------------------

def test_short_trade_units_signed(tmp_path):
    repo = Repository(tmp_path / "bot.db")
    try:
        repo.record_open(_trade("S1", units=-500), direction=-1, risk_amount=75.0)
        rows = repo.open_trades()
        assert len(rows) == 1
        assert rows[0]["units"] == -500
        assert rows[0]["direction"] == -1
    finally:
        repo.close()
