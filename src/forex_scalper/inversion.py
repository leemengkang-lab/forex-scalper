"""
inversion.py
============
Execution-policy helper: optionally take the OPPOSITE side of a detected setup.

When an instrument is listed in `invert_instruments`, we flip the signal's
direction (buy<->sell). Only `direction` (and `note`) change: the risk manager
re-derives the stop from direction + magnitude-only distance, and the bot's
take-profit is computed from direction, so entry_price and stop_price are left
untouched. Pure and total — no failure modes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from forex_scalper.models import SetupSignal


def maybe_invert(sig: SetupSignal, invert_instruments: Sequence[str]) -> SetupSignal:
    """Return an opposite-side copy of `sig` if its instrument is inverted, else `sig`."""
    if sig.instrument not in invert_instruments:
        return sig
    return replace(sig, direction=-sig.direction, note=f"INVERTED: {sig.note}")
