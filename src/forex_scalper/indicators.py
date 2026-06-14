"""
indicators.py
=============
Pure functions over lists of Candles / floats. Deterministic, stdlib only.
Kept separate so they can be unit-tested in isolation.
"""

from __future__ import annotations

from forex_scalper.models import Candle


def ema(values: list[float], period: int) -> float | None:
    """Latest EMA value, or None if not enough data."""
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period          # seed with SMA
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def ema_series(values: list[float], period: int) -> list[float | None]:
    """Full EMA series aligned to `values` (None until seeded)."""
    out: list[float | None] = [None] * len(values)
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


def atr(candles: list[Candle], period: int = 14) -> float | None:
    """Wilder's ATR. Returns latest value or None if insufficient data."""
    if len(candles) < period + 1:
        return None
    trs = [true_range(candles[i - 1].close, candles[i]) for i in range(1, len(candles))]
    a = sum(trs[:period]) / period             # seed
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period   # Wilder smoothing
    return a


def atr_pips(instrument_pip: float, candles: list[Candle], period: int = 14) -> float | None:
    a = atr(candles, period)
    return None if a is None else a / instrument_pip


def recent_swing_low(candles: list[Candle], lookback: int = 5) -> float | None:
    if len(candles) < lookback:
        return None
    return min(c.low for c in candles[-lookback:])


def recent_swing_high(candles: list[Candle], lookback: int = 5) -> float | None:
    if len(candles) < lookback:
        return None
    return max(c.high for c in candles[-lookback:])


def avg_body_ratio(candles: list[Candle], lookback: int = 10) -> float | None:
    """Mean (body / range) over recent candles. High => directional; low => choppy/wicky."""
    sample = candles[-lookback:]
    rngs = [c for c in sample if c.range > 0]
    if not rngs:
        return None
    return sum(c.body / c.range for c in rngs) / len(rngs)
