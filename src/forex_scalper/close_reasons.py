"""
close_reasons.py
================
Tiny shared registry so the site that CLOSES a trade (time stop, watchdog) can
tell the reconciler — which is the single place that records/journals closes —
WHY it closed. Unmarked trades default to the reconciler's reason (broker SL/TP).
"""

from __future__ import annotations


class CloseReasons:
    def __init__(self) -> None:
        self._reasons: dict[str, str] = {}

    def mark(self, trade_id: str, reason: str) -> None:
        self._reasons[trade_id] = reason

    def take(self, trade_id: str, default: str) -> str:
        """Return and consume the reason for trade_id, or `default` if unmarked."""
        return self._reasons.pop(trade_id, default)
