"""
Characterization tests for forex_scalper.indicators.
These pin the CURRENT behavior — do not change source to make them pass.
"""

from datetime import datetime, timezone

from forex_scalper.indicators import atr, avg_body_ratio, ema, recent_swing_low
from forex_scalper.models import Candle


def _c(o, h, l, c):
    return Candle(datetime(2025, 1, 1, tzinfo=timezone.utc), o, h, l, c)


def test_ema_returns_none_when_too_short():
    assert ema([1.0, 2.0], 5) is None


def test_ema_seeds_with_sma_then_smooths():
    # period=5, seed SMA over first 5 (all 1.0) = 1.0; then one value of 2.0
    # EMA = 2.0 * k + 1.0 * (1-k), k = 2/(5+1) = 1/3
    vals = [1.0] * 5 + [2.0]
    assert abs(ema(vals, 5) - (2.0 * (2 / 6) + 1.0 * (1 - 2 / 6))) < 1e-12


def test_atr_needs_period_plus_one():
    # 5 candles, period=14 -> not enough (need 15)
    assert atr([_c(1, 1, 1, 1)] * 5, 14) is None


def test_atr_exact_minimum_candles():
    # 15 flat candles: every TR = max(H-L, |H-prev_close|, |L-prev_close|) = 0.2
    # seed = 0.2; Wilder smoothing over identical TRs stays at 0.2
    cs = [_c(1.0, 1.1, 0.9, 1.0)] * 15
    result = atr(cs, 14)
    assert result is not None
    assert abs(result - 0.2) < 1e-10


def test_swing_low_picks_min_over_lookback():
    cs = [_c(1, 2, 0.9, 1.5), _c(1, 2, 0.7, 1.5), _c(1, 2, 1.1, 1.5)]
    assert recent_swing_low(cs, 3) == 0.7


def test_avg_body_ratio_uniform_candles():
    # body = |close - open| = |1.15 - 1.0| = 0.15
    # range = high - low = 1.2 - 0.8 = 0.4
    # ratio = 0.15 / 0.4 = 0.375 for each candle -> average = 0.375
    cs = [_c(1.0, 1.2, 0.8, 1.15)] * 5
    result = avg_body_ratio(cs, 5)
    assert result is not None
    assert abs(result - 0.375) < 1e-10
