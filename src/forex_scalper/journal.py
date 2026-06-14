"""
journal.py
==========
Logs every trade with full context. This file IS your edge-monitoring system:
it feeds walk-forward validation, hour-of-day analysis, and the "is my live
edge decaying vs backtest?" check. Log everything; you can't fix what you never
recorded.
"""

from __future__ import annotations

import csv
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

FIELDS = [
    "ts", "event", "instrument", "setup", "direction", "bias", "regime",
    "entry", "stop", "take_profit", "stop_pips", "units", "risk_amount",
    "spread_pips", "atr_pips", "exit", "pnl", "exit_reason", "note",
]


@dataclass
class JournalRow:
    ts: str = ""
    event: str = ""            # "signal" | "open" | "close" | "reject"
    instrument: str = ""
    setup: str = ""
    direction: int = 0
    bias: str = ""
    regime: str = ""
    entry: float = 0.0
    stop: float = 0.0
    take_profit: float = 0.0
    stop_pips: float = 0.0
    units: int = 0
    risk_amount: float = 0.0
    spread_pips: float = 0.0
    atr_pips: float = 0.0
    exit: float = 0.0
    pnl: float = 0.0
    exit_reason: str = ""
    note: str = ""


class Journal:
    def __init__(self, path: str = "journal.csv"):
        self.path = path
        if not os.path.exists(path):
            with open(path, "w", newline="") as f:
                csv.writer(f).writerow(FIELDS)

    def log(self, row: JournalRow) -> None:
        if not row.ts:
            row.ts = datetime.now(UTC).isoformat()
        d = asdict(row)
        with open(self.path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writerow(d)
