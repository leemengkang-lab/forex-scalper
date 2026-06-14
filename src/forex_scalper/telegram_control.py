"""telegram_control.py
=====================
Inbound Telegram command handlers (halt / resume / status / positions).

TelegramCommands is the testable core — it is a pure Python class that
takes duck-typed ``repo``, ``risk``, ``broker``, and ``allowed_chat_ids``
objects and acts on them.  No ``telegram`` imports needed here; the async
Application / CommandHandler wiring lives in ``build_telegram_app()`` so
it is lazily imported and kept out of the test path.

The ``message`` argument passed to each handler is duck-typed:

  * python-telegram-bot Update.message → has ``message.chat.id`` (nested)
    and ``async reply_text(text)``.
  * Test FakeMsg → has ``message.chat_id`` (flat) and
    ``async reply_text(text)``.

Both shapes are supported via ``getattr(message, "chat_id", None) or
message.chat.id``.

NOTE: The Application polling path (``build_telegram_app`` / ``run_live``
wiring) is covered by manual smoke testing with a real Telegram token, not
by unit tests.
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _chat_id(message: Any) -> int:
    """Extract chat-id from either a real Update.message or a test fake."""
    cid = getattr(message, "chat_id", None)
    if cid is not None:
        return int(cid)
    return int(message.chat.id)


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------

class TelegramCommands:
    """Authorise → act → reply.  No network calls; fully unit-testable."""

    def __init__(
        self,
        repo: Any,
        risk: Any,
        broker: Any,
        allowed_chat_ids: list[int],
        *,
        now_utc: Any = lambda: datetime.now(UTC),
    ) -> None:
        self._repo = repo
        self._risk = risk
        self._broker = broker
        self._allowed = set(allowed_chat_ids)
        self._now_utc = now_utc

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _authorize(self, message: Any) -> bool:
        if _chat_id(message) not in self._allowed:
            await message.reply_text("unauthorized")
            return False
        return True

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def handle_halt(self, message: Any) -> None:
        if not await self._authorize(message):
            return
        self._repo.set_halt("manual halt via telegram")
        self._risk.halted = True
        await message.reply_text("Bot HALTED. No new entries until /resume.")

    async def handle_resume(self, message: Any) -> None:
        if not await self._authorize(message):
            return
        self._repo.clear_halt()
        self._risk.halted = False
        await message.reply_text("Bot RESUMED.")

    async def handle_status(self, message: Any) -> None:
        if not await self._authorize(message):
            return

        halted = self._risk.halted or self._repo.is_halted()
        halt_reason = self._repo.halt_reason() if halted else None
        balance = self._risk.balance
        daily_pnl = self._risk.daily_pnl
        gross_heat = self._risk.gross_heat
        open_count = len(self._broker.open_trades())

        lines = [
            f"Status: {'HALTED' if halted else 'running'}",
        ]
        if halt_reason:
            lines.append(f"  halt reason: {halt_reason}")
        lines += [
            f"  balance:    {balance:.2f}",
            f"  daily P&L:  {daily_pnl:+.2f}",
            f"  gross heat: {gross_heat:.2f}",
            f"  open trades: {open_count}",
        ]

        # Optional: real equity from account_summary (OandaBroker only)
        acct_fn = getattr(self._broker, "account_summary", None)
        if acct_fn is not None:
            try:
                acct = acct_fn()
                lines.append(f"  NAV (broker): {acct.nav:.2f} {acct.currency}")
            except Exception as exc:
                logger.debug("account_summary failed: %r", exc)

        await message.reply_text("\n".join(lines))

    async def handle_positions(self, message: Any) -> None:
        if not await self._authorize(message):
            return
        trades = self._broker.open_trades()
        if not trades:
            await message.reply_text("No open positions.")
            return
        lines: list[str] = []
        for t in trades:
            instrument = getattr(t, "instrument", "?")
            units = getattr(t, "units", "?")
            entry = getattr(t, "entry_price", None)
            stop = getattr(t, "stop_price", None)
            entry_str = f"{entry:.5f}" if isinstance(entry, float) else str(entry)
            stop_str = f"{stop:.5f}" if isinstance(stop, float) else str(stop)
            lines.append(f"{instrument} units={units} entry={entry_str} stop={stop_str}")
        await message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Outbound notifier factory (builds TelegramNotifier from env, else Console)
# ---------------------------------------------------------------------------

def notifier_from_env() -> Any:
    """Return a TelegramNotifier if TELEGRAM_BOT_TOKEN is set, else ConsoleNotifier."""
    from forex_scalper.notifier import ConsoleNotifier, TelegramNotifier

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    raw_ids = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
    if not token:
        return ConsoleNotifier()
    chat_ids = [s.strip() for s in raw_ids.split(",") if s.strip()]
    if not chat_ids:
        logger.warning("TELEGRAM_BOT_TOKEN set but TELEGRAM_ALLOWED_CHAT_IDS is empty")
        return ConsoleNotifier()
    return TelegramNotifier(token, chat_ids[0])


def allowed_chat_ids_from_env() -> list[int]:
    """Parse TELEGRAM_ALLOWED_CHAT_IDS env var into a list of ints."""
    raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() or (part.startswith("-") and part[1:].isdigit()):
            ids.append(int(part))
    return ids


# ---------------------------------------------------------------------------
# Application builder (lazy-imports telegram; NOT called in tests)
# ---------------------------------------------------------------------------

def build_telegram_app(token: str, commands: TelegramCommands) -> Any:
    """Build a python-telegram-bot Application with command handlers registered.

    Lazy-imports ``telegram`` / ``telegram.ext`` so the module can be imported
    in environments where the library is absent (e.g. stripped CI images) —
    though in practice it is a declared dependency.

    NOTE: the Application polling path is covered by manual smoke testing with
    a real token, not by unit tests.
    """
    from telegram.ext import Application, CommandHandler

    async def _halt(update: Any, context: Any) -> None:
        if update.message:
            await commands.handle_halt(update.message)

    async def _resume(update: Any, context: Any) -> None:
        if update.message:
            await commands.handle_resume(update.message)

    async def _status(update: Any, context: Any) -> None:
        if update.message:
            await commands.handle_status(update.message)

    async def _positions(update: Any, context: Any) -> None:
        if update.message:
            await commands.handle_positions(update.message)

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("halt", _halt))
    app.add_handler(CommandHandler("resume", _resume))
    app.add_handler(CommandHandler("status", _status))
    app.add_handler(CommandHandler("positions", _positions))
    return app
