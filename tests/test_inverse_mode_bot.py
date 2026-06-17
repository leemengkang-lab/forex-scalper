"""
Integration tests for EUR/USD inverse mode through the orchestrator.
Reuses demo_smoke.build_market() which yields a Setup A LONG on EUR_USD.
"""

from datetime import UTC, datetime

from forex_scalper.bot import ScalpBot
from forex_scalper.config import BotConfig
from forex_scalper.demo_smoke import INSTR, PV, build_market
from forex_scalper.execution import PaperBroker


def test_invert_instruments_defaults_to_empty():
    assert BotConfig().invert_instruments == []


def _run(invert_instruments, tmp_path):
    market = build_market()
    cfg = BotConfig(
        starting_balance=10_000.0,
        invert_instruments=invert_instruments,
        journal_path=str(tmp_path / "journal.csv"),
    )
    broker = PaperBroker(pip_value={INSTR: PV})
    bid, ask = market._bid[INSTR], market._ask[INSTR]
    broker.set_price(INSTR, bid, ask)
    bot = ScalpBot(cfg, market, broker)
    # time inside the London/NY overlap so the session gate passes
    now = datetime(2025, 1, 6, 13, 30, tzinfo=UTC)
    bot.on_candle_close(INSTR, now)
    return broker.open_trades()


def test_normal_mode_opens_long_on_eurusd(tmp_path):
    trades = _run([], tmp_path)
    assert len(trades) == 1
    t = trades[0]
    assert t.units > 0                       # LONG
    assert t.stop_price < t.entry_price      # stop below entry for a long
    assert t.take_profit > t.entry_price     # tp above entry for a long


def test_inverse_mode_opens_short_on_eurusd(tmp_path):
    trades = _run(["EUR_USD"], tmp_path)
    assert len(trades) == 1
    t = trades[0]
    assert t.units < 0                       # SHORT (flipped)
    assert t.stop_price > t.entry_price      # stop above entry for a short
    assert t.take_profit < t.entry_price     # tp below entry for a short
