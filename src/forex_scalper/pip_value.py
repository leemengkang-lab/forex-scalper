"""
pip_value.py
============
Pure functions for computing pip value per unit in a given account currency.
No I/O, no network — the live loop feeds cross rates from outside.
"""

from __future__ import annotations

from collections.abc import Callable

from forex_scalper.models import pip_size


def quote_ccy(instrument: str) -> str:
    """Return the quote currency of an instrument (e.g. 'USD' from 'EUR_USD')."""
    return instrument.upper().split("_")[1]


def pip_value_per_unit(
    instrument: str,
    account_ccy: str,
    rate_to_account: Callable[[str], float],
) -> float:
    """Account-ccy value of a 1-pip move on 1 unit of `instrument`.

    Args:
        instrument: e.g. 'EUR_USD', 'USD_JPY'.
        account_ccy: the account denomination, e.g. 'SGD'.
        rate_to_account: callable that accepts a currency code and returns
            units of account_ccy per 1 unit of that currency. Not called
            when the quote currency already equals the account currency.
    """
    q = quote_ccy(instrument)
    factor = 1.0 if q == account_ccy.upper() else rate_to_account(q)
    return pip_size(instrument) * factor
