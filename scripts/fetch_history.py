"""
fetch_history.py
================
Download paginated OANDA candlestick history for backtesting.

Testable core
-------------
  paginate_candles(request_page, start, end, *, granularity_minutes, page)
  write_csv(candles, path) -> int

CLI
---
  python scripts/fetch_history.py --months 6 --instruments EUR_USD,AUD_USD ...

The CLI hits the real OANDA API; the core functions never touch the network.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from forex_scalper.models import Candle

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Granularity map (OANDA granularity string -> minutes per bar)
# ---------------------------------------------------------------------------

_GRAN_MINUTES: dict[str, int] = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D": 1440,
}


# ---------------------------------------------------------------------------
# Core: paginate_candles
# ---------------------------------------------------------------------------


def paginate_candles(
    request_page: Callable[[str, int, bool], list[Candle]],
    start: datetime,
    end: datetime,
    *,
    granularity_minutes: int,
    page: int = 5000,
) -> list[Candle]:
    """Fetch all candles in [start, end) by repeatedly calling *request_page*.

    *request_page(from_iso, count, include_first)* returns COMPLETE candles
    ascending by time (possibly fewer than *count* near the end).

    Pagination strategy
    -------------------
    • First call: include_first=True from *start*.
    • Subsequent calls: include_first=True from the ISO timestamp one
      granularity-minute step past the last candle returned (so the overlap
      point is never re-requested from the API; boundary de-dup via seen-set
      handles any accidental overlaps robustly).

    Termination guarantees (no infinite loops)
    -------------------------------------------
    1. Empty page  → stop.
    2. Short page (< *page* candles) → stop (reached end of available data).
    3. Next cursor >= *end* → stop.
    4. Cursor did not advance vs. previous iteration → break (safety net).

    Candles at or past *end* are dropped.  De-duplication is performed via a
    set of seen ``candle.time`` values so boundary overlaps never appear twice.

    Returns a de-duplicated, time-sorted list[Candle].
    """
    seen: set[datetime] = set()
    result: list[Candle] = []

    cursor: datetime = start
    prev_cursor: datetime | None = None
    include_first = True

    while cursor < end:
        # Safety net: if the cursor didn't advance last iteration, break.
        if prev_cursor is not None and cursor <= prev_cursor:
            log.warning(
                "paginate_candles: cursor did not advance (cursor=%s prev=%s); breaking",
                cursor.isoformat(),
                prev_cursor.isoformat(),
            )
            break

        page_candles = request_page(cursor.isoformat(), page, include_first)

        if not page_candles:
            # Empty page: no more data.
            break

        prev_cursor = cursor
        short_page = len(page_candles) < page

        for candle in page_candles:
            if candle.time >= end:
                # Drop everything at or past the exclusive end boundary.
                continue
            if candle.time in seen:
                continue
            seen.add(candle.time)
            result.append(candle)

        # Advance cursor to one granularity step past the last candle's time.
        last_time = page_candles[-1].time
        cursor = last_time + timedelta(minutes=granularity_minutes)
        include_first = True  # consistent: always include_first from cursor

        if short_page:
            # Fewer bars than requested → reached the end of available data.
            break

    result.sort(key=lambda c: c.time)
    return result


# ---------------------------------------------------------------------------
# Core: write_csv
# ---------------------------------------------------------------------------


def write_csv(candles: list[Candle], path: Path | str) -> int:
    """Write candles to *path* as CSV (time,open,high,low,close).

    *time* is written as ``candle.time.isoformat()``.
    Returns the number of data rows written.
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["time", "open", "high", "low", "close"])
        for c in candles:
            writer.writerow([c.time.isoformat(), c.open, c.high, c.low, c.close])
    return len(candles)


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def _load_dotenv_inline(path: str | Path = ".env") -> None:
    """Minimal .env loader (no overwrite of already-set vars)."""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _build_api_client(token: str, environment: str) -> Any:  # type: ignore[return]
    """Build oandapyV20.API with truststore injected (required on Windows)."""
    try:
        import truststore  # type: ignore[import-untyped]

        truststore.inject_into_ssl()
    except ImportError:
        pass
    except Exception as exc:
        log.warning("truststore.inject_into_ssl() failed: %r", exc)

    import oandapyV20  # type: ignore[import-untyped]

    env = "practice" if environment == "practice" else "live"
    return oandapyV20.API(access_token=token, environment=env)


def _parse_candle_page(resp: dict[str, Any]) -> list[Candle]:  # type: ignore[return]
    """Parse a raw InstrumentsCandles response into a list of COMPLETE Candles."""
    out: list[Candle] = []
    for c in resp.get("candles", []):
        if not c.get("complete", False):
            continue
        mid = c["mid"]
        ts = datetime.fromisoformat(c["time"].replace("Z", "+00:00")).astimezone(UTC)
        out.append(
            Candle(
                time=ts,
                open=float(mid["o"]),
                high=float(mid["h"]),
                low=float(mid["l"]),
                close=float(mid["c"]),
                complete=True,
            )
        )
    return out


def _fetch_page_with_retry(
    client: Any,  # type: ignore[return]
    instrument: str,
    granularity: str,
    from_iso: str,
    count: int,
    include_first: bool,
    *,
    max_attempts: int = 3,
    retry_delays: tuple[float, ...] = (2.0, 5.0),
) -> list[Candle]:
    """Call InstrumentsCandles with simple retry on transient errors."""
    import requests
    from oandapyV20.endpoints.instruments import (  # type: ignore[import-untyped]
        InstrumentsCandles,
    )

    params: dict[str, Any] = {
        "granularity": granularity,
        "count": count,
        "from": from_iso,
        "price": "M",
        "includeFirst": "true" if include_first else "false",
    }
    req = InstrumentsCandles(instrument=instrument, params=params)

    for attempt in range(1, max_attempts + 1):
        try:
            resp = client.request(req)
            return _parse_candle_page(resp)
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == max_attempts:
                raise
            delay = retry_delays[attempt - 1]
            log.warning("fetch page retry %d/%d after %.1fs: %r", attempt, max_attempts, delay, exc)
            time.sleep(delay)

    raise AssertionError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """Download OANDA history to CSV files."""
    parser = argparse.ArgumentParser(
        description="Download paginated OANDA candlestick history for backtesting."
    )
    parser.add_argument(
        "--months",
        type=int,
        default=6,
        help="Number of months of history to download (default: 6).",
    )
    parser.add_argument(
        "--instruments",
        default="EUR_USD,AUD_USD,USD_JPY,NZD_USD,USD_CHF",
        help="Comma-separated list of OANDA instruments (default: EUR_USD,AUD_USD,USD_JPY,NZD_USD,USD_CHF).",
    )
    parser.add_argument(
        "--granularity",
        default="M1",
        help="OANDA granularity string, e.g. M1, M15, H1 (default: M1).",
    )
    parser.add_argument(
        "--out-dir",
        default="data/history",
        help="Output directory for CSV files (default: data/history).",
    )
    args = parser.parse_args()

    # Load .env — try project's helper first, fall back to inline loader.
    try:
        from forex_scalper.live import _load_dotenv  # type: ignore[import-untyped]

        _load_dotenv()
    except ImportError:
        _load_dotenv_inline()

    token = os.environ["OANDA_TOKEN"]
    account_id = os.environ.get("OANDA_ACCOUNT_ID", "")
    environment = os.environ.get("OANDA_ENVIRONMENT", "practice")

    granularity = args.granularity.upper()
    gran_minutes = _GRAN_MINUTES.get(granularity)
    if gran_minutes is None:
        parser.error(
            f"Unknown granularity {granularity!r}. Supported: {list(_GRAN_MINUTES)}"
        )

    instruments = [i.strip() for i in args.instruments.split(",") if i.strip()]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    now_utc = datetime.now(UTC)
    start = now_utc - timedelta(days=args.months * 30)
    end = now_utc

    log.info("account=%s env=%s gran=%s months=%d", account_id, environment, granularity, args.months)

    client = _build_api_client(token, environment)

    for instrument in instruments:
        print(f"Fetching {instrument} ...", flush=True)

        def request_page(
            from_iso: str,
            count: int,
            include_first: bool,
            _instr: str = instrument,
            _gran: str = granularity,
            _client: Any = client,
        ) -> list[Candle]:
            return _fetch_page_with_retry(
                _client, _instr, _gran, from_iso, count, include_first
            )

        candles = paginate_candles(
            request_page,
            start,
            end,
            granularity_minutes=gran_minutes,  # type: ignore[arg-type]
            page=5000,
        )

        suffix = granularity
        csv_path = out_dir / f"{instrument}_{suffix}.csv"
        rows = write_csv(candles, csv_path)

        if candles:
            date_from = candles[0].time.strftime("%Y-%m-%d")
            date_to = candles[-1].time.strftime("%Y-%m-%d")
        else:
            date_from = date_to = "n/a"

        print(f"  {instrument}: {rows} rows  [{date_from} .. {date_to}]  -> {csv_path}")

    print("Done.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    main()
