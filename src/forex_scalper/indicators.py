"""
indicators.py
=============
Pure functions over lists of Candles / floats. Deterministic, stdlib only.
Kept separate so they can be unit-tested in isolation.
"""

from __future__ import annotations

from typing import List, Optional

from models import Candle


def ema(values: List[float], period: int) -> Optional[float]:
    """Latest EMA value, or None if not enough data."""
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period          # seed with SMA
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def ema_series(values: List[float], period: int) -> List[Optional[float]]:
    """Full EMA series aligned to `values` (None until seeded)."""
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    out[period - 1] = e
    for i in range(period, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def true_range(prev_close: float, c: Candle) -> float:
    return max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))


def atr(candles: List[Candle], period: int = 14) -> Optional[float]:
    """Wilder's ATR. Returns latest value or None if insufficient data."""
    if len(candles) < period + 1:
        return None
    trs = [true_range(candles[i - 1].close, candles[i]) for i in range(1, len(candles))]
    a = sum(trs[:period]) / period             # seed
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period   # Wilder smoothing
    return a


def atr_pips(instrument_pip: float, candles: List[Candle], period: int = 14) -> Optional[float]:
    a = atr(candles, period)
    return None if a is None else a / instrument_pip


def recent_swing_low(candles: List[Candle], lookback: int = 5) -> Optional[float]:
    if len(candles) < lookback:
        return None
    return min(c.low for c in candles[-lookback:])


def recent_swing_high(candles: List[Candle], lookback: int = 5) -> Optional[float]:
    if len(candles) < lookback:
        return None
    return max(c.high for c in candles[-lookback:])


def avg_body_ratio(candles: List[Candle], lookback: int = 10) -> Optional[float]:
    """Mean (body / range) over recent candles. High => directional; low => choppy/wicky."""
    sample = candles[-lookback:]
    rngs = [c for c in sample if c.range > 0]
    if not rngs:
        return None
    return sum(c.body / c.range for c in rngs) / len(rngs)
