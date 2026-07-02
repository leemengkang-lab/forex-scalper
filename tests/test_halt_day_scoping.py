"""
Regression tests for the daily-loss halt lifecycle.

Bug: a daily-loss kill-switch halt was persisted as a sticky flag and restored
verbatim at every boot, so a halt from a PRIOR trading day permanently bricked
the bot across restarts. A daily-loss halt must expire on a new trading day at
startup; a manual (/halt) halt must stay sticky until /resume.
"""

from datetime import UTC, datetime

from forex_scalper.persistence import Repository
from forex_scalper.reconcile import PositionReconciler


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _Risk:
    def __init__(self) -> None:
        self.halted = False
        self.open_positions: list = []

    def close_position(self, *a, **k) -> None:
        pass

    def register_fill(self, *a, **k) -> None:
        pass


class _Broker:
    def open_trades(self) -> list:
        return []


def _reconciler(repo: Repository, risk: _Risk) -> PositionReconciler:
    return PositionReconciler(repo, risk, get_closed_pnl=lambda _t: 0.0)


# --------------------------------------------------------------------------- #
# Persistence level
# --------------------------------------------------------------------------- #
def test_set_halt_with_day_records_halt_day():
    repo = Repository(":memory:")
    repo.set_halt("daily loss limit reached", day="2026-07-01")
    assert repo.is_halted() and repo.halt_day() == "2026-07-01"
    repo.clear_halt()
    assert repo.is_halted() is False
    assert repo.halt_day() is None


def test_set_halt_without_day_has_no_halt_day():
    repo = Repository(":memory:")
    repo.set_halt("manual halt via telegram")
    assert repo.halt_day() is None


# --------------------------------------------------------------------------- #
# Startup restore (reconcile_on_startup) — day scoping
# --------------------------------------------------------------------------- #
def test_startup_clears_daily_halt_from_prior_day():
    repo, risk = Repository(":memory:"), _Risk()
    repo.set_halt("daily loss limit reached", day="2026-07-01")
    summary = _reconciler(repo, risk).reconcile_on_startup(
        _Broker(), now=datetime(2026, 7, 2, 11, 40, tzinfo=UTC)
    )
    assert risk.halted is False
    assert repo.is_halted() is False
    assert summary.halt_restored is False


def test_startup_keeps_daily_halt_from_same_day():
    repo, risk = Repository(":memory:"), _Risk()
    repo.set_halt("daily loss limit reached", day="2026-07-02")
    summary = _reconciler(repo, risk).reconcile_on_startup(
        _Broker(), now=datetime(2026, 7, 2, 15, 0, tzinfo=UTC)
    )
    assert risk.halted is True
    assert repo.is_halted() is True
    assert summary.halt_restored is True


def test_startup_keeps_manual_halt_regardless_of_day():
    repo, risk = Repository(":memory:"), _Risk()
    repo.set_halt("manual halt via telegram")  # no day -> sticky
    summary = _reconciler(repo, risk).reconcile_on_startup(
        _Broker(), now=datetime(2026, 7, 2, 11, 40, tzinfo=UTC)
    )
    assert risk.halted is True
    assert repo.is_halted() is True
    assert summary.halt_restored is True


def test_startup_clears_legacy_daily_halt_without_day():
    # Halts set by the OLD code have no halt_day; a daily-loss halt with no
    # recorded day is treated as expired (self-heals the deployed stale flag).
    repo, risk = Repository(":memory:"), _Risk()
    repo.set_halt("daily loss limit reached")  # legacy: no day
    summary = _reconciler(repo, risk).reconcile_on_startup(
        _Broker(), now=datetime(2026, 7, 2, 11, 40, tzinfo=UTC)
    )
    assert risk.halted is False
    assert repo.is_halted() is False
    assert summary.halt_restored is False
