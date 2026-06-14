"""
tests/test_bot_persistence.py
==============================
Unit tests verifying that ScalpBot persists opened trades to the SQLite
Repository when a repo= is supplied, and that repo=None (the backtest path)
continues to work without any crash.
"""
from __future__ import annotations

from datetime import UTC, datetime

from forex_scalper.bot import ScalpBot
from forex_scalper.config import BotConfig
from forex_scalper.demo_smoke import INSTR, PV, build_market
from forex_scalper.execution import PaperBroker
from forex_scalper.persistence import Repository


def test_open_trade_is_persisted(tmp_path):
    cfg = BotConfig(starting_balance=10_000.0)
    market = build_market()
    broker = PaperBroker(pip_value={INSTR: PV})
    bid, ask = market._bid[INSTR], market._ask[INSTR]
    broker.set_price(INSTR, bid, ask)
    repo = Repository(tmp_path / "bot.db")
    bot = ScalpBot(cfg, market, broker, repo=repo)
    bot.on_candle_close(INSTR, datetime(2025, 1, 6, 13, 30, tzinfo=UTC))
    rows = repo.open_trades()
    assert len(rows) == 1
    assert rows[0]["instrument"] == INSTR and rows[0]["direction"] == 1
    assert rows[0]["risk_amount"] > 0
    # the persisted trade_id matches the broker's open trade
    assert rows[0]["trade_id"] == broker.open_trades()[0].trade_id
    repo.close()


def test_no_persist_without_repo(tmp_path):
    # ScalpBot with repo=None must still work (no crash, just no persistence)
    cfg = BotConfig(starting_balance=10_000.0)
    market = build_market()
    broker = PaperBroker(pip_value={INSTR: PV})
    broker.set_price(INSTR, market._bid[INSTR], market._ask[INSTR])
    ScalpBot(cfg, market, broker).on_candle_close(
        INSTR, datetime(2025, 1, 6, 13, 30, tzinfo=UTC)
    )
    assert len(broker.open_trades()) == 1
