"""calendar_feed.py
==================
Load red-folder news events from a CSV file and inject them into
SessionFilter so the bot goes flat around high-impact releases.

CSV format (header required)::

    instrument,time
    *,2026-06-16T12:30:00Z
    USD_JPY,2026-06-17T01:30:00Z
    EUR_USD,2026-06-18T12:15:00Z

- ``instrument`` — OANDA pair (e.g. "EUR_USD") or "*" (all pairs).
- ``time``       — ISO-8601; UTC assumed for naive datetimes.

Resilient by design:
- Missing file → returns ``{}`` (logs a warning).
- Malformed rows → skipped (logged); rest of file still processed.
"""
from __future__ import annotations

import csv
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from forex_scalper.session import SessionFilter

log = logging.getLogger(__name__)


def load_events(path: str | Path) -> dict[str, list[datetime]]:
    """Load red-folder events from a CSV with header ``instrument,time``.

    Returns ``{instrument: [datetime, ...]}`` sorted ascending, suitable
    for ``SessionFilter.set_events``.  Missing file → ``{}``.  Malformed
    rows are skipped and logged.
    """
    p = Path(path)
    if not p.exists():
        log.warning("calendar_feed: news CSV not found at %s — no events loaded", p)
        return {}

    events: dict[str, list[datetime]] = {}

    with p.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for lineno, row in enumerate(reader, start=2):  # start=2: row 1 is header
            instrument = (row.get("instrument") or "").strip()
            raw_time = (row.get("time") or "").strip()

            if not instrument or not raw_time:
                log.warning(
                    "calendar_feed: skipping row %d — missing instrument or time: %r",
                    lineno,
                    row,
                )
                continue

            try:
                # Replace trailing Z so fromisoformat handles it on Python < 3.11
                dt = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
                # Attach UTC if naive
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
            except (ValueError, TypeError) as exc:
                log.warning(
                    "calendar_feed: skipping row %d — unparseable time %r: %s",
                    lineno,
                    raw_time,
                    exc,
                )
                continue

            events.setdefault(instrument, []).append(dt)

    # Sort each list ascending so callers can rely on ordering
    for key in events:
        events[key].sort()

    total = sum(len(v) for v in events.values())
    log.info("calendar_feed: loaded %d events for %d instruments from %s", total, len(events), p)
    return events


def load_into_session(session: SessionFilter, path: str | Path) -> int:
    """Load events from *path* and call ``session.set_events(...)``.

    Returns the total number of event datetimes loaded (0 if file missing
    or entirely malformed).
    """
    events = load_events(path)
    session.set_events(events)
    return sum(len(v) for v in events.values())
