"""
bias.py
=======
Higher-timeframe bias. Looks only at the 1H. Answers ONE question:
long-only, short-only, or stand aside today.

Robust + simple beats clever here. We require three things to agree before we
commit to a direction:
  1. price is on the right side of a slow EMA,
  2. the slow EMA is sloping the right way,
  3. recent structure confirms (higher highs / lower lows).
If they disagree -> FLAT (take nothing).
"""

from __future__ import annotations

from dataclasses import dataclass

from data import MarketState
from models import Bias


@dataclass
class BiasConfig:
    timeframe: str = "1H"
    ema_period: int = 50
    slope_lookback: int = 3      # candles back to measure EMA slope
    structure_lookback: int = 5


def get_bias(market: MarketState, instrument: str, cfg: BiasConfig) -> Bias:
    closes = market.closes(instrument, cfg.timeframe)
    if len(closes) < cfg.ema_period + cfg.slope_lookback + 1:
        return Bias.FLAT

    e_now = market.ema(instrument, cfg.timeframe, cfg.ema_period)
    # EMA a few candles ago, to read slope
    e_prev = _ema_at(closes[:-cfg.slope_lookback], cfg.ema_period)
    if e_now is None or e_prev is None:
        return Bias.FLAT

    price = closes[-1]
    candles = market.candles(instrument, cfg.timeframe, cfg.structure_lookback + 1)
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]

    up = price > e_now and e_now > e_prev and highs[-1] >= max(highs[:-1])
    down = price < e_now and e_now < e_prev and lows[-1] <= min(lows[:-1])

    if up and not down:
        return Bias.LONG_ONLY
    if down and not up:
        return Bias.SHORT_ONLY
    return Bias.FLAT


def _ema_at(closes, period):
    import indicators as ind
    return ind.ema(closes, period)
