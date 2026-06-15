"""
test_backtest_costs.py
======================
Tests for per-instrument spreads, slippage, and CSV robustness in the
backtest harness. The metrics math and trade-resolution logic are NOT tested
here — that's covered by test_backtest_smoke.py.
"""

from __future__ import annotations

import textwrap
from datetime import UTC

from forex_scalper.backtest import (
    DEFAULT_SPREAD_FALLBACK,
    DEFAULT_SPREADS_PIPS,
    BacktestBroker,
    load_csv,
)
from forex_scalper.models import pip_size

# --------------------------------------------------------------------------- #
# Spread tests
# --------------------------------------------------------------------------- #

def test_per_instrument_spread_applied():
    """spread_pips=None -> use DEFAULT_SPREADS_PIPS map."""
    b = BacktestBroker({"USD_CHF": 0.0001})   # spread_pips=None -> use map
    b.set_price_from_close("USD_CHF", 0.9000)
    bid, ask = b.get_price("USD_CHF")
    spread_pips = (ask - bid) / pip_size("USD_CHF")
    assert abs(spread_pips - DEFAULT_SPREADS_PIPS["USD_CHF"]) < 1e-6


def test_flat_spread_backcompat():
    """A float spread_pips applies uniformly — backward-compat path."""
    b = BacktestBroker({"EUR_USD": 0.00013}, spread_pips=0.6)
    b.set_price_from_close("EUR_USD", 1.10)
    bid, ask = b.get_price("EUR_USD")
    assert abs(((ask - bid) / pip_size("EUR_USD")) - 0.6) < 1e-6


def test_dict_spread_uses_fallback_for_unknown():
    """A dict spread_pips falls back to DEFAULT_SPREAD_FALLBACK for unlisted pairs."""
    b = BacktestBroker({"GBP_USD": 0.00013}, spread_pips={"EUR_USD": 0.8})
    b.set_price_from_close("GBP_USD", 1.30)
    bid, ask = b.get_price("GBP_USD")
    spread_pips = (ask - bid) / pip_size("GBP_USD")
    assert abs(spread_pips - DEFAULT_SPREAD_FALLBACK) < 1e-6


def test_spread_map_none_uses_default_map_for_known_pair():
    """EUR_USD with spread_pips=None picks DEFAULT_SPREADS_PIPS['EUR_USD'] = 0.8."""
    b = BacktestBroker({"EUR_USD": 0.00013})  # spread_pips defaults to None
    b.set_price_from_close("EUR_USD", 1.10)
    bid, ask = b.get_price("EUR_USD")
    spread_pips = (ask - bid) / pip_size("EUR_USD")
    assert abs(spread_pips - DEFAULT_SPREADS_PIPS["EUR_USD"]) < 1e-6


def test_spread_symmetric_around_close():
    """Bid and ask should be equidistant from the close price."""
    b = BacktestBroker({"AUD_USD": 0.00013}, spread_pips=None)
    close = 0.6500
    b.set_price_from_close("AUD_USD", close)
    bid, ask = b.get_price("AUD_USD")
    assert abs((bid + ask) / 2 - close) < 1e-9


# --------------------------------------------------------------------------- #
# Slippage tests
# --------------------------------------------------------------------------- #

def test_slippage_worsens_fill():
    """Slippage of 1 pip worsens a long fill above ask."""
    b = BacktestBroker({"EUR_USD": 0.00013}, spread_pips=0.0, slippage_pips=1.0)
    b.set_price_from_close("EUR_USD", 1.10)
    long = b.place_market_order("EUR_USD", 1000, 1.09, 1.11)
    # with 0 spread, ask==1.10; +1 pip slippage -> fill 1.1001
    assert long is not None
    assert abs(long.entry_price - 1.1001) < 1e-9


def test_slippage_worsens_short_fill():
    """Slippage on a short worsens the fill below bid."""
    b = BacktestBroker({"EUR_USD": 0.00013}, spread_pips=0.0, slippage_pips=1.0)
    b.set_price_from_close("EUR_USD", 1.10)
    short = b.place_market_order("EUR_USD", -1000, 1.11, 1.09)
    # with 0 spread, bid==1.10; -1 pip slippage -> fill 1.0999
    assert short is not None
    assert abs(short.entry_price - 1.0999) < 1e-9


def test_zero_slippage_is_unchanged():
    """Default slippage=0 must not affect fill price."""
    b = BacktestBroker({"EUR_USD": 0.00013}, spread_pips=0.6, slippage_pips=0.0)
    b.set_price_from_close("EUR_USD", 1.10)
    _bid, ask = b.get_price("EUR_USD")
    trade = b.place_market_order("EUR_USD", 1000, 1.09, 1.11)
    assert trade is not None
    assert abs(trade.entry_price - ask) < 1e-9


# --------------------------------------------------------------------------- #
# load_csv robustness tests
# --------------------------------------------------------------------------- #

def _make_csv(rows: str) -> str:
    """Wrap header + rows into a temp-file-like string."""
    return "time,open,high,low,close\n" + textwrap.dedent(rows)


def _load_from_string(content: str):
    """Write content to a temporary file and load it."""
    import os
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="") as f:
        f.write(content)
        path = f.name
    try:
        return load_csv(path)
    finally:
        os.unlink(path)


def test_load_csv_trailing_newline():
    """Trailing blank lines must not produce extra candles."""
    content = _make_csv(
        "2025-01-06T12:00:00+00:00,1.0800,1.0810,1.0790,1.0805\n\n\n"
    )
    candles = _load_from_string(content)
    assert len(candles) == 1


def test_load_csv_z_suffix():
    """Timestamps ending in 'Z' must be parsed as UTC."""
    content = _make_csv(
        "2025-01-06T12:00:00Z,1.0800,1.0810,1.0790,1.0805\n"
    )
    candles = _load_from_string(content)
    assert len(candles) == 1
    assert candles[0].time.tzinfo is not None


def test_load_csv_naive_datetime_gets_utc():
    """Naive datetimes (no offset) must be attached to UTC."""
    content = _make_csv(
        "2025-01-06 12:00:00,1.0800,1.0810,1.0790,1.0805\n"
    )
    candles = _load_from_string(content)
    assert len(candles) == 1
    assert candles[0].time.tzinfo == UTC


def test_load_csv_sorted_ascending():
    """Output must be sorted ascending by time regardless of CSV order."""
    content = _make_csv(
        "2025-01-06T12:02:00+00:00,1.0810,1.0820,1.0800,1.0815\n"
        "2025-01-06T12:00:00+00:00,1.0800,1.0810,1.0790,1.0805\n"
        "2025-01-06T12:01:00+00:00,1.0805,1.0815,1.0795,1.0810\n"
    )
    candles = _load_from_string(content)
    assert len(candles) == 3
    times = [c.time for c in candles]
    assert times == sorted(times)


def test_load_csv_skips_blank_fields():
    """Rows with empty/missing price fields must be silently skipped."""
    content = _make_csv(
        "2025-01-06T12:00:00+00:00,1.0800,1.0810,1.0790,1.0805\n"
        "2025-01-06T12:01:00+00:00,,1.0820,,1.0815\n"  # missing open/low
        "2025-01-06T12:02:00+00:00,1.0810,1.0820,1.0800,1.0815\n"
    )
    candles = _load_from_string(content)
    assert len(candles) == 2


def test_load_csv_values_correct():
    """OHLC values must be loaded accurately."""
    content = _make_csv(
        "2025-01-06T12:00:00+00:00,1.0800,1.0810,1.0790,1.0805\n"
    )
    candles = _load_from_string(content)
    c = candles[0]
    assert c.open == 1.0800
    assert c.high == 1.0810
    assert c.low == 1.0790
    assert c.close == 1.0805
