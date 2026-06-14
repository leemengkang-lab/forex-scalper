"""
setups.py
=========
The two entry detectors. Both only fire when bias AND regime agree — they are
never "always on".

Setup A  - Trend pullback : in an uptrend, price pulls back into the 20 EMA,
           prints a rejection, momentum resumes -> enter with the trend.
Setup B  - Level rejection: price false-breaks a key level (prior-day high/low,
           round number) then rejects hard back inside -> fade toward the mean.

Each returns a SetupSignal (entry + structural stop) or None. The orchestrator
adds pip_value and hands it to the risk manager, which has the final say.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from forex_scalper import indicators as ind
from forex_scalper.data import MarketState
from forex_scalper.models import LONG, SHORT, Bias, Candle, Regime, SetupSignal, pip_size


@dataclass
class SetupConfig:
    timeframe: str = "1M"
    ema_period: int = 20
    pullback_touch_atr: float = 0.25   # how close to EMA counts as "into" it (in ATR)
    rejection_wick_ratio: float = 0.5  # wick must be >= this * range to count
    swing_lookback: int = 5
    level_tol_atr: float = 0.30        # how near a level counts as a test
    round_step: float = 0.0050         # 50-pip round numbers for non-JPY
    round_step_jpy: float = 0.50


def detect(market: MarketState, instrument: str, bias: Bias, regime: Regime,
           cfg: SetupConfig) -> Optional[SetupSignal]:
    if regime == Regime.TREND and bias in (Bias.LONG_ONLY, Bias.SHORT_ONLY):
        sig = _pullback(market, instrument, bias, cfg)
        if sig:
            return sig
    if regime == Regime.RANGE:
        sig = _level_rejection(market, instrument, bias, cfg)
        if sig:
            return sig
    return None


# --------------------------------------------------------------------------- #
def _pullback(market, instrument, bias, cfg) -> Optional[SetupSignal]:
    candles = market.candles(instrument, cfg.timeframe)
    if len(candles) < cfg.ema_period + 5:
        return None
    ema = market.ema(instrument, cfg.timeframe, cfg.ema_period)
    atr = market.atr(instrument, cfg.timeframe)
    if ema is None or atr is None:
        return None

    last = candles[-1]
    spread = market.spread_pips(instrument) or 0.0
    tol = cfg.pullback_touch_atr * atr

    if bias == Bias.LONG_ONLY:
        touched = last.low <= ema + tol            # dipped into the EMA
        rejection = (last.close - last.low) >= cfg.rejection_wick_ratio * max(last.range, 1e-9)
        resumed = last.is_up
        if touched and rejection and resumed:
            stop = ind.recent_swing_low(candles, cfg.swing_lookback)
            if stop is None or stop >= last.close:
                return None
            return SetupSignal(instrument, LONG, last.close, stop, "A", spread,
                               "pullback to EMA, bullish rejection")
    else:  # SHORT_ONLY
        touched = last.high >= ema - tol
        rejection = (last.high - last.close) >= cfg.rejection_wick_ratio * max(last.range, 1e-9)
        resumed = not last.is_up
        if touched and rejection and resumed:
            stop = ind.recent_swing_high(candles, cfg.swing_lookback)
            if stop is None or stop <= last.close:
                return None
            return SetupSignal(instrument, SHORT, last.close, stop, "A", spread,
                               "pullback to EMA, bearish rejection")
    return None


# --------------------------------------------------------------------------- #
def _level_rejection(market, instrument, bias, cfg) -> Optional[SetupSignal]:
    candles = market.candles(instrument, cfg.timeframe)
    if len(candles) < 5:
        return None
    atr = market.atr(instrument, cfg.timeframe)
    if atr is None:
        return None

    last = candles[-1]
    spread = market.spread_pips(instrument) or 0.0
    tol = cfg.level_tol_atr * atr
    level = _nearest_level(instrument, last.high, last.low, cfg)
    if level is None:
        return None

    # false break ABOVE the level, then close back inside -> short the failure
    broke_up = last.high >= level + tol and last.close < level
    if broke_up and (bias != Bias.LONG_ONLY):
        stop = last.high + 0.1 * atr
        if stop <= last.close:
            return None
        return SetupSignal(instrument, SHORT, last.close, stop, "B", spread,
                           f"false break above {level:.5f}")

    # false break BELOW the level, then close back inside -> long the failure
    broke_dn = last.low <= level - tol and last.close > level
    if broke_dn and (bias != Bias.SHORT_ONLY):
        stop = last.low - 0.1 * atr
        if stop >= last.close:
            return None
        return SetupSignal(instrument, LONG, last.close, stop, "B", spread,
                           f"false break below {level:.5f}")
    return None


def _nearest_level(instrument: str, high: float, low: float, cfg: SetupConfig) -> Optional[float]:
    """Nearest round-number level to the current candle (cheap proxy for S/R)."""
    step = cfg.round_step_jpy if instrument.upper().endswith("JPY") else cfg.round_step
    mid = (high + low) / 2
    return round(mid / step) * step
