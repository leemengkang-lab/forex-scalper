"""Setup B (level rejection) only fires when enable_setup_b is True."""

from datetime import UTC, datetime

from forex_scalper import setups as setups_mod
from forex_scalper.data import MarketState
from forex_scalper.models import Bias, Candle, Regime
from forex_scalper.setups import SetupConfig


def _range_market():
    """A false-break-above-then-close-inside at the 1.1000 round number."""
    m = MarketState()
    t0 = datetime(2025, 1, 6, 12, 0, tzinfo=UTC)
    # enough 1M candles for ATR; last one false-breaks 1.1000 and closes back inside
    for _i in range(20):
        m.add_candle("EUR_USD", "1M", Candle(t0, 1.0990, 1.0995, 1.0985, 1.0990))
    m.add_candle("EUR_USD", "1M", Candle(t0, 1.0996, 1.1006, 1.0994, 1.0997))
    m.set_price("EUR_USD", 1.0997, 1.0998)
    return m


def test_setup_b_suppressed_when_disabled():
    m = _range_market()
    cfg = SetupConfig(enable_setup_b=False)
    sig = setups_mod.detect(m, "EUR_USD", Bias.FLAT, Regime.RANGE, cfg)
    assert sig is None


def test_setup_b_fires_when_enabled():
    m = _range_market()
    cfg = SetupConfig(enable_setup_b=True)
    sig = setups_mod.detect(m, "EUR_USD", Bias.FLAT, Regime.RANGE, cfg)
    assert sig is not None and sig.setup == "B"
