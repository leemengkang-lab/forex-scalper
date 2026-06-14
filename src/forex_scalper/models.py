"""
models.py
=========
Shared data types used across the bot. No internal dependencies, so every
other module can import from here safely.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


# --- direction ------------------------------------------------------------- #
LONG = +1
SHORT = -1


# --- higher-timeframe bias ------------------------------------------------- #
class Bias(Enum):
    LONG_ONLY = "long_only"
    SHORT_ONLY = "short_only"
    FLAT = "flat"          # no directional edge -> take nothing


# --- market regime (decides which setup is allowed to fire) ---------------- #
class Regime(Enum):
    TREND = "trend"        # expanding ATR + clean candles  -> Setup A (pullback)
    RANGE = "range"        # contained, two-sided           -> Setup B (rejection)
    DEAD = "dead"          # no volatility / chop           -> stand flat


@dataclass
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    complete: bool = True

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def is_up(self) -> bool:
        return self.close >= self.open


@dataclass
class SetupSignal:
    """
    Emitted by a setup detector. The orchestrator enriches this into a
    risk_manager.TradeSignal by adding `pip_value_per_unit` from live rates.
    """
    instrument: str
    direction: int          # LONG / SHORT
    entry_price: float
    stop_price: float
    setup: str              # "A" pullback / "B" rejection
    spread_pips: float = 0.0
    note: str = ""


def pip_size(instrument: str) -> float:
    """0.01 for JPY quote pairs, else 0.0001."""
    return 0.01 if instrument.upper().endswith("JPY") else 0.0001


def to_pips(instrument: str, price_distance: float) -> float:
    return abs(price_distance) / pip_size(instrument)
