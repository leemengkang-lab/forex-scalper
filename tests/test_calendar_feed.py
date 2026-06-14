"""tests/test_calendar_feed.py
==============================
Unit tests for the calendar_feed module.
"""
from __future__ import annotations

from datetime import UTC, datetime

from forex_scalper.calendar_feed import load_events, load_into_session
from forex_scalper.session import SessionConfig, SessionFilter


def test_load_events_groups_by_instrument(tmp_path):
    p = tmp_path / "news.csv"
    p.write_text("instrument,time\n*,2026-06-16T12:30:00Z\nUSD_JPY,2026-06-17T01:30:00Z\n")
    ev = load_events(p)
    assert "*" in ev and "USD_JPY" in ev
    assert ev["*"][0] == datetime(2026, 6, 16, 12, 30, tzinfo=UTC)


def test_missing_file_returns_empty():
    assert load_events("does_not_exist_12345.csv") == {}


def test_malformed_rows_skipped(tmp_path):
    p = tmp_path / "news.csv"
    p.write_text("instrument,time\nEUR_USD,not-a-date\nEUR_USD,2026-06-18T12:15:00Z\n")
    ev = load_events(p)
    assert len(ev.get("EUR_USD", [])) == 1  # bad row skipped


def test_load_into_session_blacks_out(tmp_path):
    p = tmp_path / "news.csv"
    p.write_text("instrument,time\nEUR_USD,2026-06-18T12:15:00Z\n")
    sess = SessionFilter(SessionConfig())
    n = load_into_session(sess, p)
    assert n == 1
    # 2 minutes before the event -> within the default before-buffer (5 min) -> blackout
    assert sess.news_blackout("EUR_USD", datetime(2026, 6, 18, 12, 13, tzinfo=UTC)) is True
    # well outside -> no blackout
    assert sess.news_blackout("EUR_USD", datetime(2026, 6, 18, 10, 0, tzinfo=UTC)) is False


def test_sorted_output(tmp_path):
    """Events within an instrument should be sorted ascending."""
    p = tmp_path / "news.csv"
    p.write_text(
        "instrument,time\n"
        "EUR_USD,2026-06-18T14:00:00Z\n"
        "EUR_USD,2026-06-18T12:00:00Z\n"
        "EUR_USD,2026-06-18T10:00:00Z\n"
    )
    ev = load_events(p)
    times = ev["EUR_USD"]
    assert times == sorted(times)


def test_naive_datetime_gets_utc(tmp_path):
    """Naive ISO timestamps (no tz) should be treated as UTC."""
    p = tmp_path / "news.csv"
    p.write_text("instrument,time\nUSD_JPY,2026-06-17T01:30:00\n")
    ev = load_events(p)
    dt = ev["USD_JPY"][0]
    assert dt.tzinfo is not None
    assert dt == datetime(2026, 6, 17, 1, 30, tzinfo=UTC)


def test_wildcard_blackout_applies_to_all_instruments(tmp_path):
    """'*' events should block any instrument."""
    p = tmp_path / "news.csv"
    p.write_text("instrument,time\n*,2026-06-16T12:30:00Z\n")
    sess = SessionFilter(SessionConfig())
    load_into_session(sess, p)
    # 1 minute before -> within 5-min buffer
    assert sess.news_blackout("GBP_USD", datetime(2026, 6, 16, 12, 29, tzinfo=UTC)) is True
    assert sess.news_blackout("EUR_USD", datetime(2026, 6, 16, 12, 29, tzinfo=UTC)) is True


def test_load_into_session_missing_file_returns_zero():
    """Missing file should return 0 and not raise."""
    sess = SessionFilter(SessionConfig())
    n = load_into_session(sess, "no_such_file_xyz.csv")
    assert n == 0


def test_empty_csv_returns_empty(tmp_path):
    """A CSV with only a header and no rows returns {}."""
    p = tmp_path / "news.csv"
    p.write_text("instrument,time\n")
    ev = load_events(p)
    assert ev == {}


def test_row_missing_instrument_skipped(tmp_path):
    """Rows missing the instrument field are skipped gracefully."""
    p = tmp_path / "news.csv"
    p.write_text("instrument,time\n,2026-06-16T12:30:00Z\nEUR_USD,2026-06-17T12:00:00Z\n")
    ev = load_events(p)
    assert "" not in ev
    assert "EUR_USD" in ev
