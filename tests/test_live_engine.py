"""
tests/test_live_engine.py
=========================
Unit tests for CrossRateBook and LiveEngine.  No network, no asyncio.

All tests use deterministic fakes:
  - FakeBroker     — returns prices from a dict or raises KeyError for unknown instruments.
  - Monotonic clock — passed explicitly so throttle tests are deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime

from forex_scalper.bot import ScalpBot
from forex_scalper.config import BotConfig
from forex_scalper.data import MarketState
from forex_scalper.demo_smoke import INSTR, PV, build_market
from forex_scalper.execution import Broker, PaperBroker
from forex_scalper.live_engine import CrossRateBook, LiveEngine
from forex_scalper.stream import StreamEvent

# ---------------------------------------------------------------------------
# Fake broker helper
# ---------------------------------------------------------------------------

class FakeBroker(Broker):
    """Returns prices from a pre-loaded dict; raises KeyError for unknowns."""

    def __init__(self, prices: dict[str, tuple[float, float]] | None = None) -> None:
        self._prices: dict[str, tuple[float, float]] = prices or {}
        self.call_count: int = 0

    def get_price(self, instrument: str) -> tuple[float, float]:
        self.call_count += 1
        # Raise KeyError for unknown instruments — same as PaperBroker.
        return self._prices[instrument]

    # Unused stubs required by the abstract base.
    def place_market_order(self, instrument, units, stop, take_profit, setup=""):
        raise NotImplementedError

    def close_trade(self, trade_id):
        raise NotImplementedError

    def open_trades(self):
        return []

    def modify_stop(self, trade_id, new_stop, *, instrument=None):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# CrossRateBook — direct rate
# ---------------------------------------------------------------------------

def test_cross_rate_book_direct_rate():
    """USD_SGD exists directly: EUR_USD pip value should use USD->SGD mid."""
    fb = FakeBroker({"USD_SGD": (1.3499, 1.3501)})
    book = CrossRateBook(fb, "SGD", ["EUR_USD"], refresh_secs=0)

    m = MarketState()
    m.set_account_ccy("SGD")

    book.refresh(m, now_monotonic=0.0)

    expected = 0.0001 * 1.3500  # pip_size("EUR_USD") * mid(USD_SGD)
    actual = m.pip_value_per_unit("EUR_USD")
    assert abs(actual - expected) < 1e-7, (
        f"pip_value_per_unit expected ~{expected} got {actual}"
    )


# ---------------------------------------------------------------------------
# CrossRateBook — inverted rate
# ---------------------------------------------------------------------------

def test_cross_rate_book_inverted_rate():
    """JPY_SGD does NOT exist; SGD_JPY does — inverted rate is used."""
    # SGD_JPY = 110 means 1 SGD = 110 JPY  =>  1 JPY = 1/110 SGD
    fb = FakeBroker({"SGD_JPY": (110.0, 110.0)})
    book = CrossRateBook(fb, "SGD", ["USD_JPY"], refresh_secs=0)

    m = MarketState()
    m.set_account_ccy("SGD")

    book.refresh(m, now_monotonic=0.0)

    # pip_size("USD_JPY") = 0.01;  JPY->SGD rate = 1/110
    expected = 0.01 * (1.0 / 110.0)
    actual = m.pip_value_per_unit("USD_JPY")
    assert abs(actual - expected) < 1e-9, (
        f"pip_value_per_unit expected ~{expected} got {actual}"
    )


# ---------------------------------------------------------------------------
# CrossRateBook — throttle
# ---------------------------------------------------------------------------

def test_cross_rate_book_throttle():
    """refresh() must skip the broker call if refresh_secs have not elapsed."""
    fb = FakeBroker({"USD_SGD": (1.35, 1.35)})
    book = CrossRateBook(fb, "SGD", ["EUR_USD"], refresh_secs=30.0)

    m = MarketState()
    m.set_account_ccy("SGD")

    book.refresh(m, now_monotonic=0.0)
    count_after_first = fb.call_count

    # Second call within window — must be skipped.
    book.refresh(m, now_monotonic=5.0)
    assert fb.call_count == count_after_first, "broker should NOT be called within throttle window"

    # Third call after window expires — must refresh.
    book.refresh(m, now_monotonic=40.0)
    assert fb.call_count > count_after_first, "broker SHOULD be called after throttle window expires"


# ---------------------------------------------------------------------------
# CrossRateBook — both instruments missing is safe
# ---------------------------------------------------------------------------

def test_cross_rate_book_both_missing_is_safe():
    """When the broker raises for every instrument, refresh must not raise.

    The pip_value_per_unit fallback is 0.0 — the risk manager will reject.
    """
    fb = FakeBroker()  # empty -> KeyError for everything
    book = CrossRateBook(fb, "SGD", ["EUR_USD"], refresh_secs=0)

    m = MarketState()
    m.set_account_ccy("SGD")

    # Must not raise.
    book.refresh(m, now_monotonic=0.0)

    # Safe fallback: 0.0 causes the risk manager to reject the trade.
    assert m.pip_value_per_unit("EUR_USD") == 0.0


# ---------------------------------------------------------------------------
# End-to-end smoke: StreamEvent through LiveEngine opens a paper trade
# ---------------------------------------------------------------------------

def test_engine_processes_trigger_candle_and_opens_trade():
    """The most important test: a StreamEvent through LiveEngine opens exactly 1 paper trade."""
    cfg = BotConfig(starting_balance=10_000.0)
    market = build_market()  # bias=LONG_ONLY + regime=TREND + Setup A ready

    # Remove the last 1M candle so we can deliver it via the engine.
    trigger = market._c[INSTR]["1M"].pop()

    broker = PaperBroker(pip_value={INSTR: PV})
    bid, ask = market._bid[INSTR], market._ask[INSTR]
    broker.set_price(INSTR, bid, ask)

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

    # build_market used set_pip_value override, so sizing works without cross
    # rates.  The CrossRateBook.refresh will try USD_SGD and fail (not set in
    # PaperBroker) — that must be silently swallowed per spec.
    ev = StreamEvent(INSTR, trigger, bid, ask)
    now = datetime(2025, 1, 6, 13, 30, tzinfo=UTC)  # inside London/NY session

    engine.process_event(ev, now=now)

    trades = broker.open_trades()
    assert len(trades) == 1, (
        f"Expected exactly 1 open trade but got {len(trades)}: {trades}"
    )


# ---------------------------------------------------------------------------
# Reconciler integration: broker-side SL close is detected each candle
# ---------------------------------------------------------------------------

def test_engine_reconciles_broker_close_each_event(tmp_path):
    """LiveEngine with a reconciler routes a broker-side close to risk on the
    next event, so daily_pnl and repo reflect the broker-fired SL.

    Steps:
      1. Process the trigger event -> opens a paper trade, persisted in repo,
         risk heat > 0.
      2. Simulate SL firing: clear the paper broker's open trades so
         open_trades() returns empty, and wire get_closed_pnl to return -120.0.
      3. Process a second event -> reconciler.sync detects the gap, routes the
         close through risk (daily_pnl == -120.0) and repo (open_trades == []).
    """
    from forex_scalper.persistence import Repository
    from forex_scalper.reconcile import PositionReconciler

    cfg = BotConfig(starting_balance=10_000.0)
    market = build_market()

    # Pop the trigger candle so we can deliver it via the engine.
    trigger = market._c[INSTR]["1M"].pop()

    broker = PaperBroker(pip_value={INSTR: PV})
    bid, ask = market._bid[INSTR], market._ask[INSTR]
    broker.set_price(INSTR, bid, ask)

    repo = Repository(str(tmp_path / "test.db"))

    # Build bot with the same repo so on_candle_close persists the open trade.
    bot = ScalpBot(cfg, market, broker, repo=repo)

    # Build reconciler using the same risk manager the bot owns.
    reconciler = PositionReconciler(
        repo,
        bot.risk,
        get_closed_pnl=lambda tid: -120.0,
    )

    engine = LiveEngine(
        cfg,
        market,
        broker,
        bot,
        account_ccy="SGD",
        tradeable=[INSTR],
        rate_refresh_secs=0,
        reconciler=reconciler,
    )

    now = datetime(2025, 1, 6, 13, 30, tzinfo=UTC)

    # Step 1: deliver trigger candle -> trade opens, persisted.
    ev1 = StreamEvent(INSTR, trigger, bid, ask)
    engine.process_event(ev1, now=now)

    trades = broker.open_trades()
    assert len(trades) == 1, f"Step 1: expected 1 open trade, got {trades}"
    assert len(repo.open_trades()) == 1, "Step 1: repo should have 1 open trade"
    assert bot.risk.gross_heat > 0, "Step 1: heat should be > 0 after fill"

    # Step 2: simulate SL firing at broker — clear broker-side trades.
    broker._trades.clear()  # type: ignore[attr-defined]
    assert broker.open_trades() == [], "Step 2: broker must report no open trades"

    # Step 3: deliver a second (benign) event -> reconciler.sync fires first.
    # Reuse the same trigger candle shape; the bot won't re-enter (duplicate guard).
    from datetime import timedelta

    from forex_scalper.models import Candle
    dummy_candle = Candle(
        now + timedelta(minutes=1),
        trigger.open,
        trigger.high,
        trigger.low,
        trigger.close,
        complete=True,
    )
    ev2 = StreamEvent(INSTR, dummy_candle, bid, ask)
    engine.process_event(ev2, now=now + timedelta(minutes=1))

    # The reconciler must have detected the close and routed it through risk.
    assert bot.risk.daily_pnl == -120.0, (
        f"Expected daily_pnl == -120.0, got {bot.risk.daily_pnl}"
    )
    assert repo.open_trades() == [], (
        f"Expected repo to have 0 open trades after reconcile, got {repo.open_trades()}"
    )

    repo.close()
