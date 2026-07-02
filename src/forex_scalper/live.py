"""live.py
=========
Async live runner — wires the blocking OANDA stream into the asyncio-based
LiveEngine via the producer/queue/consumer pattern proven in the reference bot.

Concurrent tasks:
  1. producer thread  — calls stream.iter_events() (blocking) and enqueues
                        StreamEvents into an asyncio.Queue via
                        run_coroutine_threadsafe.
  2. consumer task    — awaits events from the queue, feeds them to LiveEngine;
                        raises FatalStreamError when the STREAM_DEAD sentinel
                        arrives so systemd (or the caller) can restart.
  3. watchdog task    — every ``watchdog_interval`` seconds, checks all open
                        trades have a non-zero stop; closes any that don't.
  4. max-runtime task — optional asyncio.sleep timer for smoke-testing.

asyncio.wait(FIRST_COMPLETED) drives the whole loop: whichever task finishes
first triggers graceful cancellation of the rest.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import math
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forex_scalper.bot import ScalpBot
from forex_scalper.calendar_feed import load_into_session
from forex_scalper.config import BotConfig
from forex_scalper.data import MarketState
from forex_scalper.execution import OandaBroker, PaperBroker  # noqa: F401 (re-exported for tests)
from forex_scalper.live_engine import LiveEngine
from forex_scalper.notifier import ConsoleNotifier, Notifier
from forex_scalper.persistence import Repository
from forex_scalper.reconcile import PositionReconciler
from forex_scalper.stream import MultiInstrumentStream, StreamEvent
from forex_scalper.warmup import warm_up

# Telegram helpers are imported lazily inside build_telegram_app / run_live
# to keep the import chain clean when enable_telegram=False (default).

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sentinel & exception
# ---------------------------------------------------------------------------

STREAM_DEAD = object()
"""Placed on the queue by the producer thread when the stream dies."""


class FatalStreamError(Exception):
    """Raised by _consume when the STREAM_DEAD sentinel is received.

    Causes run_live to exit non-zero so the caller (or systemd) can restart
    the process with a fresh stream connection.
    """


# ---------------------------------------------------------------------------
# Producer bridge (blocking stream -> asyncio queue)
# ---------------------------------------------------------------------------

def _run_producer(
    stream: MultiInstrumentStream,
    on_event: Callable[[StreamEvent], object],
    on_dead: Callable[[], object],
    stop_event: threading.Event,
) -> None:
    """Pump StreamEvents to on_event.  On ANY exception (stream exhausted,
    reconnect exhausted, or unexpected), call on_dead — never die silently."""
    try:
        for ev in stream.iter_events():
            if stop_event.is_set():
                return
            on_event(ev)
    except Exception as exc:
        log.error("stream.dead error=%r", exc)
        on_dead()


def _start_producer(
    stream: MultiInstrumentStream,
    queue: asyncio.Queue[object],
    loop: asyncio.AbstractEventLoop,
    stop_event: threading.Event,
) -> threading.Thread:
    """Spawn the producer as a daemon thread; return the Thread object."""

    def _on_event(ev: StreamEvent) -> None:
        if not loop.is_closed():
            asyncio.run_coroutine_threadsafe(queue.put(ev), loop)

    def _on_dead() -> None:
        # Best-effort: if the loop already closed (another task triggered
        # shutdown), the sentinel is moot.
        if not loop.is_closed():
            asyncio.run_coroutine_threadsafe(queue.put(STREAM_DEAD), loop)

    def _target() -> None:
        _run_producer(stream, _on_event, _on_dead, stop_event)

    t = threading.Thread(target=_target, name="oanda-stream-producer", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Consumer coroutine
# ---------------------------------------------------------------------------

async def _consume(
    queue: asyncio.Queue[object],
    engine: LiveEngine,
    notifier: Notifier,
) -> None:
    """Pull events from *queue* and feed them to *engine*.

    Raises FatalStreamError when STREAM_DEAD arrives — the caller's
    asyncio.wait will see the task exception and begin shutdown.
    """
    while True:
        ev = await queue.get()
        if ev is STREAM_DEAD:
            # notifier.send is SYNCHRONOUS (ConsoleNotifier logs to stderr)
            notifier.send("CRITICAL: price stream died and could not recover. Restarting.")
            raise FatalStreamError("price stream unrecoverable")
        assert isinstance(ev, StreamEvent)  # narrow type for mypy
        engine.process_event(ev)


# ---------------------------------------------------------------------------
# Watchdog coroutine
# ---------------------------------------------------------------------------

async def _watchdog(
    broker: Any,
    notifier: Notifier,
    *,
    interval: float = 60.0,
    close_reasons: Any | None = None,
) -> None:
    """Every *interval* seconds, ensure every open trade has a valid stop-loss.

    Any trade with stop_price <= 0 or NaN is closed immediately.  Cancellation
    propagates cleanly (required for graceful shutdown).
    """
    while True:
        try:
            for t in broker.open_trades():
                if t.stop_price is None or t.stop_price <= 0 or math.isnan(t.stop_price):
                    if close_reasons is not None:
                        close_reasons.mark(t.trade_id, "watchdog_no_stop")
                    broker.close_trade(t.trade_id)
                    notifier.send(
                        f"EMERGENCY: {t.trade_id} had no stop; closed by watchdog"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            notifier.send(f"watchdog error: {exc}")
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Telegram application builder (lazy; NOT exercised by unit tests)
# ---------------------------------------------------------------------------

def _build_telegram_live_app(tg_token: str, repo: Any, risk: Any, broker: Any) -> Any:
    """Construct a python-telegram-bot Application wired to TelegramCommands.

    Lazy-imports telegram_control (which lazy-imports the telegram package) so
    the default path (enable_telegram=False) never touches those imports.

    NOTE: Application polling is covered by manual smoke testing with a real
    token, not by unit tests.
    """
    from forex_scalper.telegram_control import (
        TelegramCommands,
        allowed_chat_ids_from_env,
        build_telegram_app,
    )

    allowed = allowed_chat_ids_from_env()
    commands = TelegramCommands(repo, risk, broker, allowed)
    return build_telegram_app(tg_token, commands)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def run_live(
    cfg: BotConfig,
    *,
    token: str,
    account_id: str,
    environment: str = "practice",
    account_ccy: str,
    tradeable: list[str],
    max_runtime_seconds: float | None = None,
    broker: Any | None = None,
    stream: Any | None = None,
    market: MarketState | None = None,
    notifier: Notifier | None = None,
    watchdog_interval: float = 60.0,
    warmup: bool = True,
    db_path: str = ":memory:",
    repo: Any | None = None,
    reconciler: Any | None = None,
    starting_balance: float | None = None,
    news_csv: str | None = None,
    enable_telegram: bool = False,
) -> None:
    """Run the live trading loop until stopped.

    Injectable seams (broker, stream, market, notifier, repo, reconciler) allow
    tests to drive the runner with no real network calls and a bounded runtime.

    New optional params (all have safe defaults so existing callers are unaffected):
      db_path          — SQLite path for the Repository; default ":memory:" (in-process).
      repo             — pre-built Repository (takes precedence over db_path).
      reconciler       — pre-built PositionReconciler (takes precedence over auto-build).
      starting_balance — explicit starting balance; auto-fetched from account_summary()
                         when None and broker supports it.
      news_csv         — path to a CSV of red-folder news events (instrument,time).
                         Loaded into bot.session via SessionFilter.set_events so the
                         bot goes flat around those events.  None = skip.  A load
                         failure is non-fatal and never aborts startup.
      enable_telegram  — when True AND TELEGRAM_BOT_TOKEN is in env, starts
                         python-telegram-bot polling for /halt /resume /status
                         /positions.  Default False so all tests and CI are
                         unaffected.  The Application polling path is covered by
                         manual smoke testing with a real token, not by unit tests.

    Raises FatalStreamError if the price stream dies unrecoverably (so the
    caller/systemd can restart the process).
    """
    # --- Telegram outbound notifier (opt-in; default path is unchanged) ------
    # When enable_telegram is True and a token is present, swap ConsoleNotifier
    # for TelegramNotifier so alerts reach the operator's phone.
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "") if enable_telegram else ""
    if enable_telegram and tg_token and notifier is None:
        from forex_scalper.telegram_control import notifier_from_env
        notifier = notifier_from_env()

    notifier = notifier or ConsoleNotifier()
    market = market or MarketState()
    broker = broker or OandaBroker(
        account_id, token, practice=(environment == "practice")
    )
    stream = stream or MultiInstrumentStream(
        token, account_id, tradeable, environment
    )

    if warmup:
        try:
            warm_up(market, broker, list(tradeable))
        except Exception as exc:
            notifier.send(f"warm-up failed (non-fatal): {exc}")

    # --- persistence + bot ---------------------------------------------------
    repo = repo or Repository(db_path)
    bot = ScalpBot(cfg, market, broker, notifier, repo=repo)

    # --- starting balance -----------------------------------------------------
    # Auto-fetch from the broker when not provided and broker supports it.
    # Direct attribute assignment is the documented startup init path.
    if starting_balance is None:
        _acct_summary = getattr(broker, "account_summary", None)
        if _acct_summary is not None:
            try:
                starting_balance = _acct_summary().balance
            except Exception as exc:
                log.warning("run_live: could not fetch account_summary for balance: %r", exc)

    if starting_balance is not None:
        bot.risk.balance = starting_balance  # startup init — direct assignment is intentional
        bot.risk._day_start_balance = starting_balance  # startup init — private attr access is intentional
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        repo.upsert_day(today, starting_balance)

    # --- reconciler -----------------------------------------------------------
    get_closed_pnl = getattr(broker, "closed_trade_pnl", lambda _tid: None)
    reconciler = reconciler or PositionReconciler(
        repo, bot.risk, get_closed_pnl=get_closed_pnl,
        journal=bot.journal, close_reasons=bot.close_reasons,
    )

    # --- engine ---------------------------------------------------------------
    engine = LiveEngine(
        cfg,
        market,
        broker,
        bot,
        account_ccy=account_ccy,
        tradeable=list(tradeable),
        reconciler=reconciler,
    )

    # --- Telegram inbound polling (opt-in; NOT exercised by unit tests) -------
    # The Application polling path is covered by manual smoke testing with a
    # real token, not by unit tests.
    tg_app: Any | None = None
    if enable_telegram and tg_token:
        try:
            tg_app = _build_telegram_live_app(tg_token, repo, bot.risk, broker)
            await tg_app.initialize()
            await tg_app.start()
            if tg_app.updater is not None:
                await tg_app.updater.start_polling()
            log.info("Telegram polling started")
        except Exception as exc:
            log.warning("Telegram polling failed to start: %r", exc)
            tg_app = None

    # --- startup reconcile ----------------------------------------------------
    try:
        summary = reconciler.reconcile_on_startup(broker)
        notifier.send(
            f"reconciled: closed={len(summary.closed)} "
            f"rebuilt_open={summary.rebuilt_open} "
            f"halt_restored={summary.halt_restored}"
        )
    except Exception as exc:
        notifier.send(f"startup reconcile failed (non-fatal): {exc}")
        log.warning("run_live: reconcile_on_startup raised", exc_info=True)

    # --- news calendar --------------------------------------------------------
    if news_csv is not None:
        try:
            n = load_into_session(bot.session, news_csv)
            notifier.send(f"loaded {n} news events from {news_csv}")
        except Exception as exc:
            notifier.send(f"news calendar load failed (non-fatal): {exc}")
            log.warning("run_live: load_into_session raised", exc_info=True)

    notifier.send(
        f"live runner started — environment={environment} "
        f"instruments={tradeable} account_ccy={account_ccy}"
    )

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[object] = asyncio.Queue()
    stop_event = threading.Event()

    producer_thread = _start_producer(stream, queue, loop, stop_event)  # noqa: F841

    tasks: list[asyncio.Task[object]] = [
        asyncio.create_task(_consume(queue, engine, notifier), name="consumer"),
        asyncio.create_task(
            _watchdog(broker, notifier, interval=watchdog_interval,
                      close_reasons=bot.close_reasons), name="watchdog"
        ),
    ]
    if max_runtime_seconds is not None:
        tasks.append(
            asyncio.create_task(
                asyncio.sleep(max_runtime_seconds), name="max-runtime"
            )
        )

    fatal: BaseException | None = None
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.get_name() == "max-runtime":
                log.info("max_runtime reached seconds=%.1f", max_runtime_seconds)
            else:
                task_exc = task.exception()
                if task_exc is not None:
                    log.error("task died name=%s error=%r", task.get_name(), task_exc)
                    if isinstance(task_exc, FatalStreamError):
                        fatal = task_exc
    finally:
        stop_event.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        # --- Telegram cleanup (mirror reference main_live.py pattern) ---------
        if tg_app is not None:
            with contextlib.suppress(Exception):
                if tg_app.updater is not None:
                    await tg_app.updater.stop()
            with contextlib.suppress(Exception):
                await tg_app.stop()
            with contextlib.suppress(Exception):
                await tg_app.shutdown()
        notifier.send("live runner stopped")
        # producer_thread is daemon=True; it dies with the process / loop exit.

    if fatal is not None:
        raise fatal


# ---------------------------------------------------------------------------
# .env loader (port from reference)
# ---------------------------------------------------------------------------

def _load_dotenv(path: str | Path = ".env") -> None:
    """Populate os.environ from a .env file using setdefault (no overwrites).

    On the production VM, systemd already sets env vars via EnvironmentFile=.
    For local dev we load .env so the developer doesn't need to source it.
    """
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entrypoint: read env/args, resolve account currency, run the loop."""
    parser = argparse.ArgumentParser(description="forex-scalper live runner")
    parser.add_argument(
        "--max-runtime-seconds",
        type=float,
        default=None,
        metavar="SECS",
        help="Exit cleanly after this many seconds (smoke-test flag).",
    )
    parser.add_argument(
        "--practice",
        dest="environment",
        action="store_const",
        const="practice",
        help="Force practice environment.",
    )
    parser.add_argument(
        "--live",
        dest="environment",
        action="store_const",
        const="live",
        help="Force live environment (real money).",
    )
    args = parser.parse_args()

    _load_dotenv()

    token = os.environ["OANDA_TOKEN"]
    account_id = os.environ["OANDA_ACCOUNT_ID"]
    environment = args.environment or os.environ.get("OANDA_ENVIRONMENT", "practice")

    # Resolve account currency from the broker so we never hardcode a currency.
    _probe = OandaBroker(account_id, token, practice=(environment == "practice"))
    summary = _probe.account_summary()
    account_ccy = summary.currency

    cfg = BotConfig()
    tradeable = cfg.instruments

    Path("data").mkdir(exist_ok=True)

    _news_csv_default = "config/news_events.csv"
    news_csv: str | None = _news_csv_default if Path(_news_csv_default).exists() else None

    # Enable Telegram when a bot token is available; off by default so CI/tests
    # are unaffected (Application polling requires a real token + network).
    enable_telegram = bool(os.environ.get("TELEGRAM_BOT_TOKEN", ""))

    asyncio.run(
        run_live(
            cfg,
            token=token,
            account_id=account_id,
            environment=environment,
            account_ccy=account_ccy,
            tradeable=tradeable,
            max_runtime_seconds=args.max_runtime_seconds,
            db_path="data/bot.db",
            news_csv=news_csv,
            enable_telegram=enable_telegram,
            # starting_balance left as None so it is auto-fetched from account_summary()
        )
    )


if __name__ == "__main__":
    main()
