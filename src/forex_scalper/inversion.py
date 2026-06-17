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


def parse_invert_instruments(raw: str | None) -> list[str]:
    """Parse the INVERT_INSTRUMENTS env var: comma-separated, e.g. "EUR_USD,AUD_USD".

    Whitespace is stripped, blanks dropped, names upper-cased to match the
    canonical instrument format ("EUR_USD"). Returns [] for None/blank.
    """
    if not raw:
        return []
    return [token.strip().upper() for token in raw.split(",") if token.strip()]
