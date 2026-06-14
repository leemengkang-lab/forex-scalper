"""
Characterization tests for the bias / regime / setups pipeline.
Uses the synthetic uptrend market from demo_smoke.build_market() which is
designed to produce: bias=LONG_ONLY, regime=TREND, Setup A long signal.

These pin CURRENT behavior — do not change source to make them pass.
"""

from forex_scalper import bias as bias_mod
from forex_scalper import regime as regime_mod
from forex_scalper import setups as setups_mod
from forex_scalper.config import BotConfig
from forex_scalper.demo_smoke import INSTR, build_market
from forex_scalper.models import Bias, LONG, Regime


def test_pipeline_produces_long_only_trend_setup_a():
    market = build_market()
    cfg = BotConfig()

    bias = bias_mod.get_bias(market, INSTR, cfg.bias)
    regime = regime_mod.get_regime(market, INSTR, cfg.regime)
    sig = setups_mod.detect(market, INSTR, bias, regime, cfg.setup)

    # bias must agree the trend is up
    assert bias == Bias.LONG_ONLY

    # regime must classify the expanding ATR + clean bodies as TREND
    assert regime == Regime.TREND

    # the pullback detector must fire and return a Setup A long signal
    assert sig is not None, "Expected a SetupSignal but got None"
    assert sig.setup == "A"
    assert sig.direction == LONG
    # structural stop must be below entry for a long trade
    assert sig.stop_price < sig.entry_price
