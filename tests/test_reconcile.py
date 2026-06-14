"""
test_reconcile.py
=================
Tests for PositionReconciler: broker-close detection + startup risk rebuild.

Uses real Repository + real RiskManager + FakeBroker + dict-backed get_closed_pnl.
No asyncio, no network.
"""

from datetime import UTC, datetime

from forex_scalper.execution import OpenTrade
from forex_scalper.persistence import Repository
from forex_scalper.reconcile import PositionReconciler
from forex_scalper.risk_manager import RiskConfig, RiskManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ot(tid: str, units: int = 1000, instr: str = "EUR_USD") -> OpenTrade:
    return OpenTrade(
        tid, instr, units, 1.0850, 1.0838, 1.0890,
        datetime(2025, 1, 6, 13, 30, tzinfo=UTC), "A",
    )


class FakeBroker:
    def __init__(self, trades: list[OpenTrade]) -> None:
        self._t = list(trades)

    def open_trades(self) -> list[OpenTrade]:
        return list(self._t)

    def set_open(self, trades: list[OpenTrade]) -> None:
        self._t = list(trades)


def _setup(tmp_path, balance: float = 10_000):  # type: ignore[no-untyped-def]
    repo = Repository(tmp_path / "bot.db")
    risk = RiskManager(RiskConfig(), balance=balance)
    return repo, risk


_NOW = datetime(2025, 1, 6, 13, 45, tzinfo=UTC)


# ---------------------------------------------------------------------------
# sync() tests
# ---------------------------------------------------------------------------

def test_sync_detects_close_and_routes_pnl(tmp_path):  # type: ignore[no-untyped-def]
    repo, risk = _setup(tmp_path)
    t = _ot("101")
    repo.record_open(t, direction=1, risk_amount=100.0)
    risk.register_fill("EUR_USD", 1, 100.0)

    broker = FakeBroker([t])  # currently open
    rec = PositionReconciler(repo, risk, get_closed_pnl=lambda tid: -150.0)

    # trade still open -> no close
    assert rec.sync(broker) == []

    broker.set_open([])  # SL fired -> gone at broker
    closed = rec.sync(broker, now=_NOW)

    assert closed == ["101"]
    assert repo.open_trades() == []         # marked closed in DB
    assert risk.gross_heat == 0.0           # heat freed
    assert risk.daily_pnl == -150.0         # routed to daily P&L
    repo.close()


def test_sync_arms_and_persists_halt(tmp_path):  # type: ignore[no-untyped-def]
    repo, risk = _setup(tmp_path)
    t = _ot("201")
    repo.record_open(t, direction=1, risk_amount=100.0)
    risk.register_fill("EUR_USD", 1, 100.0)

    broker = FakeBroker([])  # already closed at broker
    # -350 on a $10k account = -3.5% > daily_loss_limit of 3%
    rec = PositionReconciler(repo, risk, get_closed_pnl=lambda tid: -350.0)
    rec.sync(broker, now=_NOW)

    assert risk.halted is True
    assert repo.is_halted() is True         # persisted to DB
    repo.close()


def test_sync_none_pnl_falls_back_to_zero(tmp_path):  # type: ignore[no-untyped-def]
    """get_closed_pnl returning None must not crash; P&L defaults to 0.0."""
    repo, risk = _setup(tmp_path)
    t = _ot("301")
    repo.record_open(t, direction=1, risk_amount=100.0)
    risk.register_fill("EUR_USD", 1, 100.0)

    broker = FakeBroker([])
    rec = PositionReconciler(repo, risk, get_closed_pnl=lambda tid: None)
    closed = rec.sync(broker, now=_NOW)

    assert closed == ["301"]
    assert repo.open_trades() == []
    assert risk.daily_pnl == 0.0            # fallback, not crash
    repo.close()


def test_sync_multiple_closes_in_one_call(tmp_path):  # type: ignore[no-untyped-def]
    """Two trades both gone at broker -> both detected and closed."""
    repo, risk = _setup(tmp_path)
    t1, t2 = _ot("401", 1000, "EUR_USD"), _ot("402", -2000, "USD_JPY")
    repo.record_open(t1, direction=1, risk_amount=100.0)
    repo.record_open(t2, direction=-1, risk_amount=120.0)
    risk.register_fill("EUR_USD", 1, 100.0)
    risk.register_fill("USD_JPY", -1, 120.0)

    pnl_map = {"401": 50.0, "402": -30.0}
    broker = FakeBroker([])
    rec = PositionReconciler(repo, risk, get_closed_pnl=lambda tid: pnl_map.get(tid))
    closed = rec.sync(broker, now=_NOW)

    assert set(closed) == {"401", "402"}
    assert repo.open_trades() == []
    assert abs(risk.daily_pnl - 20.0) < 1e-9   # 50 - 30
    assert risk.gross_heat == 0.0
    repo.close()


# ---------------------------------------------------------------------------
# reconcile_on_startup() tests
# ---------------------------------------------------------------------------

def test_startup_rebuilds_risk_from_broker(tmp_path):  # type: ignore[no-untyped-def]
    repo, _ = _setup(tmp_path)
    t1 = _ot("501", 1000, "EUR_USD")
    t2 = _ot("502", -2000, "USD_JPY")
    repo.record_open(t1, direction=1, risk_amount=100.0)
    repo.record_open(t2, direction=-1, risk_amount=120.0)

    # Fresh risk manager simulating a restart (empty in-memory state)
    risk = RiskManager(RiskConfig(), balance=10_000)
    broker = FakeBroker([t1, t2])
    rec = PositionReconciler(repo, risk, get_closed_pnl=lambda tid: None)
    summary = rec.reconcile_on_startup(broker)

    assert summary.rebuilt_open == 2
    assert abs(risk.gross_heat - 220.0) < 1e-9  # 100 + 120 rebuilt
    assert summary.closed == []
    assert summary.halt_restored is False
    repo.close()


def test_startup_restores_halt(tmp_path):  # type: ignore[no-untyped-def]
    repo, risk = _setup(tmp_path)
    repo.set_halt("prior daily limit")

    broker = FakeBroker([])
    rec = PositionReconciler(repo, risk, get_closed_pnl=lambda tid: None)
    s = rec.reconcile_on_startup(broker)

    assert s.halt_restored is True
    assert risk.halted is True
    repo.close()


def test_startup_closes_orphan_in_db(tmp_path):  # type: ignore[no-untyped-def]
    """Trade in DB but gone at broker (closed while bot was down)."""
    repo, _ = _setup(tmp_path)
    t = _ot("601")
    repo.record_open(t, direction=1, risk_amount=100.0)

    # Fresh risk manager (restart) — no in-memory positions
    risk2 = RiskManager(RiskConfig(), balance=10_000)
    broker = FakeBroker([])
    rec = PositionReconciler(repo, risk2, get_closed_pnl=lambda tid: 25.0)
    s = rec.reconcile_on_startup(
        broker, now=datetime(2025, 1, 6, 14, 0, tzinfo=UTC)
    )

    assert repo.open_trades() == []         # orphan closed in DB
    assert risk2.daily_pnl == 25.0
    assert "601" in s.closed
    repo.close()


def test_startup_persists_orphan_in_broker(tmp_path):  # type: ignore[no-untyped-def]
    """Trade at broker but missing from DB -> inserted with risk_amount=0.0."""
    repo, _ = _setup(tmp_path)
    # DB is empty; broker has one trade open
    t = _ot("701", 1000, "EUR_USD")

    risk = RiskManager(RiskConfig(), balance=10_000)
    broker = FakeBroker([t])
    rec = PositionReconciler(repo, risk, get_closed_pnl=lambda tid: None)
    s = rec.reconcile_on_startup(broker, now=_NOW)

    assert "701" in s.orphan_in_broker
    # DB now has it open
    open_rows = repo.open_trades()
    assert len(open_rows) == 1
    assert open_rows[0]["trade_id"] == "701"
    assert open_rows[0]["risk_amount"] == 0.0
    # And risk was rebuilt from broker truth
    assert s.rebuilt_open == 1
    assert abs(risk.gross_heat - 0.0) < 1e-9   # risk_amount 0.0 for unknown
    repo.close()


def test_startup_rebuilds_correct_direction(tmp_path):  # type: ignore[no-untyped-def]
    """Short trade (negative units) gets direction=-1 in rebuilt risk."""
    repo, _ = _setup(tmp_path)
    t_short = _ot("801", -500, "USD_JPY")
    repo.record_open(t_short, direction=-1, risk_amount=80.0)

    risk = RiskManager(RiskConfig(), balance=10_000)
    broker = FakeBroker([t_short])
    rec = PositionReconciler(repo, risk, get_closed_pnl=lambda tid: None)
    rec.reconcile_on_startup(broker)

    assert len(risk.open_positions) == 1
    pos = risk.open_positions[0]
    assert pos.direction == -1
    assert abs(pos.risk_amount - 80.0) < 1e-9
    repo.close()
