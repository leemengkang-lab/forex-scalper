"""A signal whose direction contradicts a non-FLAT bias must be rejected."""

from datetime import UTC, datetime

from forex_scalper.bot import ScalpBot
from forex_scalper.config import BotConfig
from forex_scalper.demo_smoke import INSTR, PV, build_market
from forex_scalper.execution import PaperBroker
from forex_scalper.models import SHORT, Bias, Regime, SetupSignal


def test_short_signal_under_long_bias_is_rejected(tmp_path):
    market = build_market()
    cfg = BotConfig(journal_path=str(tmp_path / "j.csv"))
    broker = PaperBroker(pip_value={INSTR: PV})
    bid, ask = market._bid[INSTR], market._ask[INSTR]
    broker.set_price(INSTR, bid, ask)
    bot = ScalpBot(cfg, market, broker)

    # Hand _try_enter a SHORT while bias says LONG_ONLY -> must not open.
    sig = SetupSignal(INSTR, SHORT, bid, bid + 0.0012, "A", 0.4, "synthetic")
    bot._try_enter(sig, Bias.LONG_ONLY, Regime.TREND, datetime(2025, 1, 6, 13, 30, tzinfo=UTC))

    assert broker.open_trades() == []
