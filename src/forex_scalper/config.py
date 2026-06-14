"""
config.py
=========
One place for every knob. Import RiskConfig from the (already-written) risk
manager so there's a single source of truth for risk numbers.

Reminder from the blueprint: ONE rule set for all pairs. No per-pair overrides
unless they survive out-of-sample testing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from bias import BiasConfig
from regime import RegimeConfig
from risk_manager import DEFAULT_USD_SIGN, RiskConfig
from session import SessionConfig
from setups import SetupConfig
from trade_manager import TradeManagerConfig


@dataclass
class BotConfig:
    instruments: List[str] = field(default_factory=lambda: [
        "EUR_USD", "AUD_USD", "USD_JPY", "NZD_USD", "USD_CHF",
    ])
    usd_sign: Dict[str, int] = field(default_factory=lambda: dict(DEFAULT_USD_SIGN))

    risk: RiskConfig = field(default_factory=RiskConfig)
    bias: BiasConfig = field(default_factory=BiasConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    setup: SetupConfig = field(default_factory=SetupConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    trade: TradeManagerConfig = field(default_factory=TradeManagerConfig)

    tp_atr_mult: float = 2.5         # take-profit distance in ATR (your finding)
    starting_balance: float = 10_000.0
    journal_path: str = "journal.csv"
    live: bool = False               # MUST be flipped on purpose to trade real money
