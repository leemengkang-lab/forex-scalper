"""tests/test_live_runner.py
==========================
Unit tests for the async live runner (live.py).

All tests are fully in-process — no real network, no OANDA credentials.

Design choices for determinism
-------------------------------
Test 3 (run_live end-to-end):
  FakeStream.iter_events() yields ONE trigger StreamEvent, then enters a
  blocking spin-loop (checking a threading.Event) so the producer thread
  never sends STREAM_DEAD.  The max_runtime timer (0.5 s) wins the
  asyncio.wait and triggers a clean shutdown.  The KEY assertion is that
  the event was processed and a paper trade opened.

  Why spin-loop instead of letting iter_events return?
  If iter_events returns normally, _run_producer calls on_dead() which puts
  STREAM_DEAD on the queue, consumer raises FatalStreamError, and run_live
  re-raises it — making the test need pytest.raises(FatalStreamError) and
  still racing against max_runtime.  The spin-loop approach gives a clean,
  guaranteed path where max_runtime always wins.
"""
from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from forex_scalper.bot import ScalpBot
from forex_scalper.config import BotConfig
from forex_scalper.data import MarketState
from forex_scalper.demo_smoke import INSTR, PV, build_market
from forex_scalper.execution import PaperBroker
from forex_scalper.live import (
    STREAM_DEAD,
    FatalStreamError,
    _consume,
    _watchdog,
    run_live,
)
from forex_scalper.live_engine import LiveEngine
from forex_scalper.notifier import Notifier
from forex_scalper.stream import StreamEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class CaptureNotifier(Notifier):
    """Collects all messages for test assertions."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str) -> None:
        self.messages.append(text)


def _build_engine(
    market: MarketState,
    broker: PaperBroker,
) -> tuple[LiveEngine, ScalpBot]:
    """Wire up a LiveEngine + ScalpBot from a pre-built MarketState."""
    cfg = BotConfig(starting_balance=10_000.0)
    bot = ScalpBot(cfg, market, broker)
    engine = LiveEngine(
        cfg,
        market,
        broker,
        bot,
        account_ccy="SGD",
        tradeable=[INSTR],
        rate_refresh_secs=0,
    )
    return engine, bot


def _make_trigger_event(market: MarketState) -> tuple[StreamEvent, PaperBroker]:
    """Pop the trigger candle from market, build a StreamEvent and PaperBroker."""
    trigger = market._c[INSTR]["1M"].pop()
    bid, ask = market._bid[INSTR], market._ask[INSTR]

    broker = PaperBroker(pip_value={INSTR: PV})
    broker.set_price(INSTR, bid, ask)

    ev = StreamEvent(INSTR, trigger, bid, ask)
    return ev, broker


# ---------------------------------------------------------------------------
# Test 1: _consume processes a real event then raises FatalStreamError on sentinel
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_consume_processes_event_then_raises_on_stream_dead() -> None:
    """_consume must process a StreamEvent AND raise FatalStreamError on STREAM_DEAD."""
    market = build_market()
    ev, broker = _make_trigger_event(market)
    engine, _ = _build_engine(market, broker)

    notifier = CaptureNotifier()
    queue: asyncio.Queue[object] = asyncio.Queue()

    # Enqueue the trigger event followed by the sentinel.
    await queue.put(ev)
    await queue.put(STREAM_DEAD)

    # Consumer must raise FatalStreamError after processing the real event.
    with pytest.raises(FatalStreamError):
        now = datetime(2025, 1, 6, 13, 30, tzinfo=UTC)  # inside London/NY session
        # _consume calls engine.process_event but doesn't accept a `now` override;
        # we need the session gate to pass, so we monkey-patch now_utc on the engine.
        engine._now_utc = lambda: now  # type: ignore[method-assign]
        await _consume(queue, engine, notifier)

    # The real event must have been processed and triggered a paper trade.
    trades = broker.open_trades()
    assert len(trades) == 1, (
        f"Expected 1 open trade after trigger event, got {len(trades)}: {trades}"
    )

    # Notifier must have received the CRITICAL message.
    assert any("CRITICAL" in m or "stream died" in m for m in notifier.messages), (
        f"Expected CRITICAL message in notifier, got: {notifier.messages}"
    )


# ---------------------------------------------------------------------------
# Test 2: _watchdog closes a stop-less trade
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_watchdog_closes_stopless_trade() -> None:
    """_watchdog must close any open trade whose stop_price is 0 or NaN."""
    broker = PaperBroker(pip_value={INSTR: PV})
    bid, ask = 1.0950, 1.0952
    broker.set_price(INSTR, bid, ask)

    # Place a trade with a valid stop first (place_market_order requires a price).
    # Then mutate its stop_price to 0.0 to simulate the missing-stop condition.
    trade = broker.place_market_order(INSTR, units=1000, stop=1.0900, take_profit=1.1000)
    assert trade is not None
    trade.stop_price = 0.0  # simulate missing stop

    notifier = CaptureNotifier()

    task = asyncio.create_task(
        _watchdog(broker, notifier, interval=0.01)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Watchdog must have closed the stop-less trade.
    assert broker.open_trades() == [], (
        f"Expected no open trades after watchdog, got: {broker.open_trades()}"
    )

    # Notifier must have received an EMERGENCY message.
    assert any("EMERGENCY" in m for m in notifier.messages), (
        f"Expected EMERGENCY message in notifier, got: {notifier.messages}"
    )


@pytest.mark.asyncio
async def test_watchdog_leaves_valid_trades_open() -> None:
    """_watchdog must NOT close trades that already have a proper stop-loss."""
    broker = PaperBroker(pip_value={INSTR: PV})
    broker.set_price(INSTR, 1.0950, 1.0952)
    trade = broker.place_market_order(INSTR, units=1000, stop=1.0900, take_profit=1.1000)
    assert trade is not None
    # stop_price is 1.09 — valid; watchdog must leave it alone.

    notifier = CaptureNotifier()
    task = asyncio.create_task(_watchdog(broker, notifier, interval=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(broker.open_trades()) == 1, "Valid trade must NOT be closed by watchdog"
    assert not any("EMERGENCY" in m for m in notifier.messages)


@pytest.mark.asyncio
async def test_watchdog_closes_nan_stop_trade() -> None:
    """_watchdog must also close trades whose stop_price is NaN."""
    broker = PaperBroker(pip_value={INSTR: PV})
    broker.set_price(INSTR, 1.0950, 1.0952)
    trade = broker.place_market_order(INSTR, units=1000, stop=1.0900, take_profit=1.1000)
    assert trade is not None
    trade.stop_price = math.nan

    notifier = CaptureNotifier()
    task = asyncio.create_task(_watchdog(broker, notifier, interval=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert broker.open_trades() == [], "NaN-stop trade must be closed by watchdog"
    assert any("EMERGENCY" in m for m in notifier.messages)


# ---------------------------------------------------------------------------
# Test 3: run_live end-to-end with FakeStream + max_runtime timer
# ---------------------------------------------------------------------------

class _FakeStream:
    """Yields ONE trigger StreamEvent then blocks in a spin-loop.

    The blocking loop means the producer thread never calls on_dead(), so
    STREAM_DEAD is never put on the queue.  The max_runtime timer wins
    asyncio.wait and causes a clean shutdown — deterministic and race-free.

    The spin-loop checks _stop_event every 20 ms; the producer thread is
    daemon=True so it dies with the process when the test exits.
    """

    def __init__(self, events: list[StreamEvent]) -> None:
        self._events = events
        self._stop_event = threading.Event()

    def iter_events(self) -> Iterator[StreamEvent]:
        yield from self._events
        # Block until the stop_event is set by the runner's finally block
        # (which calls stop_event.set()).  The producer thread detects
        # stop_event via its own threading.Event, but _FakeStream has its
        # own — we just spin until the test's asyncio loop has had time to
        # process the yielded event.
        while not self._stop_event.is_set():
            time.sleep(0.02)


@pytest.mark.asyncio
async def test_run_live_processes_trigger_and_stops_on_max_runtime() -> None:
    """run_live must process the trigger event, open a paper trade, and exit
    cleanly when max_runtime_seconds elapses.  No FatalStreamError expected.
    """
    market = build_market()
    ev, broker = _make_trigger_event(market)

    # We need the engine inside run_live to use our `now` so the session gate
    # passes.  LiveEngine uses `now_utc` which defaults to datetime.now(UTC).
    # We can't inject it directly; instead we widen the session window to
    # span the whole day so the test is not time-of-day sensitive.
    from datetime import time as dtime

    from forex_scalper.session import SessionConfig

    cfg = BotConfig(
        starting_balance=10_000.0,
        session=SessionConfig(
            start_utc=dtime(0, 0),
            end_utc=dtime(23, 59),
        ),
    )

    # FakeStream: yields the trigger then blocks so max_runtime wins.
    fake_stream = _FakeStream([ev])

    notifier = CaptureNotifier()

    # run_live must return cleanly (max-runtime path, no FatalStreamError).
    await run_live(
        cfg,
        token="fake-token",
        account_id="fake-account",
        environment="practice",
        account_ccy="SGD",
        tradeable=[INSTR],
        max_runtime_seconds=0.5,
        broker=broker,
        stream=fake_stream,   # type: ignore[arg-type]
        market=market,
        notifier=notifier,
        watchdog_interval=60.0,  # don't trigger watchdog during this short run
        warmup=False,  # skip broker.fetch_candles — PaperBroker doesn't support it
    )

    # Signal the fake stream to unblock (in case it's still spinning).
    fake_stream._stop_event.set()

    # Key assertion: the trigger event was processed and a paper trade opened.
    trades = broker.open_trades()
    assert len(trades) == 1, (
        f"Expected 1 open trade after trigger, got {len(trades)}: {trades}"
    )

    # Notifier must have seen the startup and shutdown messages.
    all_msgs = " ".join(notifier.messages)
    assert "live runner started" in all_msgs
    assert "live runner stopped" in all_msgs


# ---------------------------------------------------------------------------
# Test 4: run_live with PaperBroker does not crash (getattr fallback for
# closed_trade_pnl and account_summary)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_live_paper_broker_no_crash() -> None:
    """run_live must not crash when broker is a PaperBroker that has neither
    closed_trade_pnl nor account_summary.  The getattr fallback must kick in
    silently and the runner must complete cleanly within max_runtime_seconds.
    """
    from datetime import time as dtime

    from forex_scalper.session import SessionConfig

    cfg = BotConfig(
        starting_balance=10_000.0,
        session=SessionConfig(
            start_utc=dtime(0, 0),
            end_utc=dtime(23, 59),
        ),
    )

    market = build_market()
    ev, broker = _make_trigger_event(market)

    # PaperBroker has neither closed_trade_pnl nor account_summary;
    # run_live must handle both gracefully via getattr.
    assert not hasattr(broker, "closed_trade_pnl"), "PaperBroker must NOT have closed_trade_pnl"
    assert not hasattr(broker, "account_summary"), "PaperBroker must NOT have account_summary"

    fake_stream = _FakeStream([ev])
    notifier = CaptureNotifier()

    # Must return cleanly with no exception.
    await run_live(
        cfg,
        token="fake-token",
        account_id="fake-account",
        environment="practice",
        account_ccy="SGD",
        tradeable=[INSTR],
        max_runtime_seconds=0.5,
        broker=broker,
        stream=fake_stream,   # type: ignore[arg-type]
        market=market,
        notifier=notifier,
        watchdog_interval=60.0,
        warmup=False,
    )

    fake_stream._stop_event.set()

    all_msgs = " ".join(notifier.messages)
    assert "live runner started" in all_msgs
    assert "live runner stopped" in all_msgs
    # reconcile summary must appear (empty state — no positions to reconcile)
    assert "reconciled:" in all_msgs, (
        f"Expected reconcile notification in messages, got: {notifier.messages}"
    )
