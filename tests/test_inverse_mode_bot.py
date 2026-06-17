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
