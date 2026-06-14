"""tests/test_telegram_control.py
================================
Unit tests for TelegramCommands.

All tests run in-process with fakes — no network, no real Telegram token.
asyncio_mode = "auto" is set project-wide in pyproject.toml so every async
test function is picked up automatically; @pytest.mark.asyncio is added
explicitly for clarity.
"""
from __future__ import annotations

import pytest

from forex_scalper.persistence import Repository
from forex_scalper.risk_manager import RiskConfig, RiskManager
from forex_scalper.telegram_control import TelegramCommands

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeMsg:
    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class FakeBroker:
    def __init__(self, trades: tuple = ()) -> None:
        self._t = list(trades)

    def open_trades(self) -> list:
        return list(self._t)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cmds(
    tmp_path,
    allowed: tuple[int, ...] = (111,),
) -> tuple[TelegramCommands, Repository, RiskManager]:
    repo = Repository(tmp_path / "bot.db")
    risk = RiskManager(RiskConfig(), balance=1000.0)
    return TelegramCommands(repo, risk, FakeBroker(), list(allowed)), repo, risk


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_halt_sets_persisted_flag_and_risk(tmp_path):
    cmds, repo, risk = _cmds(tmp_path)
    m = FakeMsg(111)
    await cmds.handle_halt(m)
    assert risk.halted is True
    assert repo.is_halted() is True
    assert any("HALT" in r.upper() for r in m.replies)
    repo.close()


@pytest.mark.asyncio
async def test_resume_clears(tmp_path):
    cmds, repo, risk = _cmds(tmp_path)
    await cmds.handle_halt(FakeMsg(111))
    await cmds.handle_resume(FakeMsg(111))
    assert risk.halted is False
    assert repo.is_halted() is False
    repo.close()


@pytest.mark.asyncio
async def test_unauthorized_chat_rejected(tmp_path):
    cmds, repo, risk = _cmds(tmp_path, allowed=(111,))
    m = FakeMsg(999)
    await cmds.handle_halt(m)
    assert risk.halted is False  # NOT halted by an unauthorized user
    assert any("unauthorized" in r.lower() for r in m.replies)
    repo.close()


@pytest.mark.asyncio
async def test_status_and_positions_reply(tmp_path):
    cmds, repo, _risk = _cmds(tmp_path)
    sm = FakeMsg(111)
    await cmds.handle_status(sm)
    assert sm.replies

    pm = FakeMsg(111)
    await cmds.handle_positions(pm)
    assert pm.replies
    repo.close()


@pytest.mark.asyncio
async def test_status_shows_halted_state(tmp_path):
    cmds, repo, _risk = _cmds(tmp_path)
    await cmds.handle_halt(FakeMsg(111))
    m = FakeMsg(111)
    await cmds.handle_status(m)
    assert any("HALTED" in r for r in m.replies)
    repo.close()


@pytest.mark.asyncio
async def test_status_shows_running_after_resume(tmp_path):
    cmds, repo, _risk = _cmds(tmp_path)
    await cmds.handle_halt(FakeMsg(111))
    await cmds.handle_resume(FakeMsg(111))
    m = FakeMsg(111)
    await cmds.handle_status(m)
    assert any("running" in r for r in m.replies)
    repo.close()


@pytest.mark.asyncio
async def test_positions_no_open(tmp_path):
    cmds, repo, _risk = _cmds(tmp_path)
    m = FakeMsg(111)
    await cmds.handle_positions(m)
    assert any("no open" in r.lower() for r in m.replies)
    repo.close()


@pytest.mark.asyncio
async def test_resume_unauthorized_does_not_clear(tmp_path):
    cmds, repo, risk = _cmds(tmp_path, allowed=(111,))
    # Halt legitimately first
    await cmds.handle_halt(FakeMsg(111))
    assert risk.halted is True

    # Unauthorized attempt to resume
    bad = FakeMsg(999)
    await cmds.handle_resume(bad)
    assert risk.halted is True  # still halted
    assert repo.is_halted() is True
    assert any("unauthorized" in r.lower() for r in bad.replies)
    repo.close()


@pytest.mark.asyncio
async def test_multiple_allowed_chat_ids(tmp_path):
    cmds, repo, risk = _cmds(tmp_path, allowed=(111, 222))
    m1 = FakeMsg(111)
    m2 = FakeMsg(222)
    await cmds.handle_halt(m1)
    assert risk.halted is True
    await cmds.handle_resume(m2)
    assert risk.halted is False
    repo.close()
