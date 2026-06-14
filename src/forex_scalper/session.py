"""
session.py
==========
Two gates that block NEW entries (open trades are still managed):

  * Session window  — only trade the London/NY overlap (deepest liquidity,
    tightest spreads). Times are UTC; adjust to taste.
  * News blackout   — flat around red-folder events. You inject a list of event
    times per instrument (from your calendar source); we enforce a buffer
    before and after each.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta


@dataclass
class SessionConfig:
    # London/NY overlap ~ 12:00-16:00 UTC. Widen if you want more London.
    start_utc: time = time(12, 0)
    end_utc: time = time(16, 0)
    news_buffer_before_min: int = 5
    news_buffer_after_min: int = 15


class SessionFilter:
    def __init__(self, cfg: SessionConfig):
        self.cfg = cfg
        # instrument -> list of event datetimes (UTC). "*" applies to all.
        self._events: dict[str, list[datetime]] = {}

    def set_events(self, events: dict[str, list[datetime]]) -> None:
        self._events = events

    def in_session(self, now: datetime) -> bool:
        t = now.astimezone(UTC).time()
        return self.cfg.start_utc <= t < self.cfg.end_utc

    def news_blackout(self, instrument: str, now: datetime) -> bool:
        now = now.astimezone(UTC)
        before = timedelta(minutes=self.cfg.news_buffer_before_min)
        after = timedelta(minutes=self.cfg.news_buffer_after_min)
        relevant = self._events.get(instrument, []) + self._events.get("*", [])
        for ev in relevant:
            ev = ev.astimezone(UTC)
            if ev - before <= now <= ev + after:
                return True
        return False

    def can_enter(self, instrument: str, now: datetime) -> bool:
        return self.in_session(now) and not self.news_blackout(instrument, now)
