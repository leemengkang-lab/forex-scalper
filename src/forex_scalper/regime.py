"""
regime.py
=========
The switch that stops you running a trend strategy into chop. Classifies the
15M market into TREND / RANGE / DEAD using two cheap, robust signals:

  * ATR expansion  : current ATR vs its own recent average (is volatility
                     waking up or dying?)
  * body ratio     : average (body / range) of recent candles — high means
                     clean directional candles, low means wicky two-sided chop.

TREND -> Setup A (pullback) allowed.
RANGE -> Setup B (rejection) allowed.
DEAD  -> stand flat. Flat is a position.
"""

from __future__ import annotations

from dataclasses import dataclass

from forex_scalper import indicators as ind
from forex_scalper.data import MarketState
from forex_scalper.models import Regime


@dataclass
class RegimeConfig:
    timeframe: str = "15M"
    atr_period: int = 14
    atr_avg_period: int = 50     # baseline to compare current ATR against
    expansion_trend: float = 1.10  # ATR >= 110% of baseline -> volatility expanding
    dead_floor: float = 0.70       # ATR <= 70% of baseline -> dead
    body_trend: float = 0.55       # clean directional candles
    body_chop: float = 0.40        # wicky / two-sided
    body_lookback: int = 10


def get_regime(market: MarketState, instrument: str, cfg: RegimeConfig) -> Regime:
    candles = market.candles(instrument, cfg.timeframe)
    if len(candles) < cfg.atr_avg_period + cfg.atr_period + 2:
        return Regime.DEAD

    atr_now = ind.atr(candles, cfg.atr_period)
    atr_base = _avg_atr(candles, cfg.atr_period, cfg.atr_avg_period)
    body = ind.avg_body_ratio(candles, cfg.body_lookback)
    if atr_now is None or atr_base in (None, 0) or body is None:
        return Regime.DEAD

    expansion = atr_now / atr_base

    if expansion <= cfg.dead_floor or body < cfg.body_chop:
        return Regime.DEAD
    if expansion >= cfg.expansion_trend and body >= cfg.body_trend:
        return Regime.TREND
    return Regime.RANGE


def _avg_atr(candles: list, atr_period: int, window: int):
    """Mean ATR over the last `window` rolling computations."""
    vals = []
    # compute ATR at several recent end-points to get a baseline
    for end in range(len(candles) - window, len(candles) + 1, max(1, window // 10)):
        seg = candles[:end]
        a = ind.atr(seg, atr_period)
        if a is not None:
            vals.append(a)
    return sum(vals) / len(vals) if vals else None
