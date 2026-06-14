"""
execution.py
============
Broker abstraction so the bot logic never touches a vendor SDK directly.

  * Broker        — the interface every broker must implement.
  * PaperBroker   — in-memory fills for demo / shadow mode. Runs with no creds.
  * OandaBroker   — live OANDA v20 adapter. Uses oandapyV20 0.7.2.

The bot calls: get_price, place_market_order, close_trade, open_trades.
"""

from __future__ import annotations

import itertools
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import requests
from oandapyV20.exceptions import V20Error  # type: ignore[import-untyped]

from forex_scalper.models import Candle, pip_size

logger = logging.getLogger("execution")

# ---------------------------------------------------------------------------
# Retry configuration (reads only — NOT used for mutations)
# ---------------------------------------------------------------------------
_RETRY_HTTP_CODES: frozenset[int] = frozenset({500, 502, 503, 504})


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, (requests.Timeout, requests.ConnectionError)) or (
        isinstance(exc, V20Error) and getattr(exc, "code", None) in _RETRY_HTTP_CODES
    )


# ---------------------------------------------------------------------------
# Price formatting helper
# ---------------------------------------------------------------------------

def _fmt_price(instrument: str, price: float) -> str:
    """Format a price to 3 dp for JPY pairs, 5 dp otherwise."""
    # pip_size returns exactly 0.01 (JPY pairs) or 0.0001 — equality is safe here
    if pip_size(instrument) == 0.01:
        return f"{price:.3f}"
    return f"{price:.5f}"


_TF_TO_GRAN: dict[str, str] = {"1M": "M1", "15M": "M15", "1H": "H1"}


@dataclass
class AccountSummary:
    balance: float
    nav: float
    currency: str
    open_trade_count: int


@dataclass
class OpenTrade:
    trade_id: str
    instrument: str
    units: int                 # signed: + long, - short
    entry_price: float
    stop_price: float
    take_profit: float
    opened_at: datetime
    setup: str = ""


class Broker(ABC):
    @abstractmethod
    def get_price(self, instrument: str) -> tuple[float, float]:
        """(bid, ask)."""

    @abstractmethod
    def place_market_order(self, instrument: str, units: int, stop: float,
                           take_profit: float, setup: str = "") -> OpenTrade | None:
        ...

    @abstractmethod
    def close_trade(self, trade_id: str) -> float | None:
        """Returns realised P&L in account ccy, or None if not found."""

    @abstractmethod
    def open_trades(self) -> list[OpenTrade]:
        ...

    @abstractmethod
    def modify_stop(
        self, trade_id: str, new_stop: float, *, instrument: str | None = None
    ) -> bool:
        """
        Update the stop-loss price on a live position.

        Returns True on success, False if the trade is unknown or the request
        fails non-fatally. A failed trailing-stop update is non-fatal — the
        original stop remains at the broker.

        MONEY-SAFETY: single request, no retry (consistent with
        place_market_order / close_trade).
        """


class PaperBroker(Broker):
    """
    Deterministic in-memory broker. Prices are pushed in via set_price so a
    backtest / shadow run is fully reproducible. P&L is computed from the
    pip_value you provide per instrument.
    """
    def __init__(self, pip_value: dict[str, float]):
        self._prices: dict[str, tuple[float, float]] = {}
        self._pip_value = pip_value
        self._trades: dict[str, OpenTrade] = {}
        self._ids = itertools.count(1)

    def set_price(self, instrument: str, bid: float, ask: float) -> None:
        self._prices[instrument] = (bid, ask)

    def get_price(self, instrument: str) -> tuple[float, float]:
        return self._prices[instrument]

    def place_market_order(
        self, instrument: str, units: int, stop: float, take_profit: float, setup: str = ""
    ) -> OpenTrade | None:
        bid, ask = self._prices[instrument]
        fill = ask if units > 0 else bid       # pay the spread, like real life
        tid = f"P{next(self._ids)}"
        t = OpenTrade(tid, instrument, units, fill, stop, take_profit,
                      datetime.now(UTC), setup)
        self._trades[tid] = t
        logger.info("PAPER fill %s %s %+d @ %.5f (sl %.5f tp %.5f)",
                    tid, instrument, units, fill, stop, take_profit)
        return t

    def close_trade(self, trade_id: str) -> float | None:
        t = self._trades.pop(trade_id, None)
        if not t:
            return None
        bid, ask = self._prices[t.instrument]
        exit_price = bid if t.units > 0 else ask
        pip = pip_size(t.instrument)
        pips = (exit_price - t.entry_price) / pip * (1 if t.units > 0 else -1)
        pnl = pips * abs(t.units) * self._pip_value.get(t.instrument, 0.0)
        logger.info("PAPER close %s pnl=%.2f (%.1f pips)", trade_id, pnl, pips)
        return pnl

    def open_trades(self) -> list[OpenTrade]:
        return list(self._trades.values())

    def modify_stop(
        self, trade_id: str, new_stop: float, *, instrument: str | None = None
    ) -> bool:
        t = self._trades.get(trade_id)
        if t is None:
            return False
        t.stop_price = new_stop
        return True


class OandaBroker(Broker):
    """
    Live OANDA v20 broker adapter.

    Inject a `client` in tests to avoid any real network/truststore setup.
    In production, leave `client=None` and the real oandapyV20.API is created.

    MONEY-SAFETY: place_market_order and close_trade are single-request
    (no retry) because a lost response after a real fill + retry = double
    position. Only reads (get_price, open_trades) use _request_with_retry.
    """

    def __init__(
        self,
        account_id: str,
        token: str,
        practice: bool = True,
        *,
        client: Any = None,
    ) -> None:
        self._account_id = account_id
        self._sleeps: tuple[float, ...] = (1.0, 2.0, 4.0)

        if client is not None:
            # Test seam: use provided fake/mock client directly.
            self._client = client
        else:
            # Production: inject OS cert store, then build real API client.
            try:
                import truststore
                truststore.inject_into_ssl()
            except ImportError:
                pass  # optional dependency not installed
            except Exception as exc:
                logger.warning(
                    "truststore.inject_into_ssl() failed; using default ssl cert store: %r", exc
                )

            import oandapyV20  # type: ignore[import-untyped]
            env = "practice" if practice else "live"
            self._client = oandapyV20.API(access_token=token, environment=env)

    # ------------------------------------------------------------------
    # Internal: retrying wrapper (reads only)
    # ------------------------------------------------------------------

    def _request_with_retry(self, req: Any) -> dict[str, Any]:
        max_attempts = len(self._sleeps) + 1
        for attempt in range(1, max_attempts + 1):
            try:
                result: dict[str, Any] = self._client.request(req)
                return result
            except Exception as exc:
                if not _is_retryable(exc) or attempt == max_attempts:
                    raise
                sleep_s = self._sleeps[attempt - 1]
                logger.warning(
                    "broker.retry attempt=%d next_sleep=%.1f error=%r",
                    attempt, sleep_s, exc,
                )
                time.sleep(sleep_s)
        raise AssertionError("unreachable")  # for type checker

    # ------------------------------------------------------------------
    # Broker interface
    # ------------------------------------------------------------------

    def get_price(self, instrument: str) -> tuple[float, float]:
        """Return (bid, ask) — retryable read."""
        import oandapyV20.endpoints.pricing as v20_pricing  # type: ignore[import-untyped]
        req = v20_pricing.PricingInfo(
            accountID=self._account_id, params={"instruments": instrument}
        )
        resp = self._request_with_retry(req)
        prices = resp.get("prices", [])
        if not prices:
            raise ValueError(f"get_price: no price data for {instrument!r}")
        p = prices[0]
        return (float(p["bids"][0]["price"]), float(p["asks"][0]["price"]))

    def place_market_order(
        self,
        instrument: str,
        units: int,
        stop: float,
        take_profit: float,
        setup: str = "",
    ) -> OpenTrade | None:
        """
        Place a MARKET FOK order with attached SL/TP.

        MONEY-SAFETY: single request, no retry.
        Returns None on rejection (logs reason at WARNING).
        """
        import oandapyV20.endpoints.orders as v20_orders  # type: ignore[import-untyped]

        idempotency_id = uuid.uuid4().hex
        body: dict[str, Any] = {
            "order": {
                "instrument": instrument,
                "units": str(int(units)),
                "type": "MARKET",
                "timeInForce": "FOK",
                "positionFill": "DEFAULT",
                "stopLossOnFill": {"price": _fmt_price(instrument, stop)},
                "takeProfitOnFill": {"price": _fmt_price(instrument, take_profit)},
                "clientExtensions": {"id": idempotency_id},
            }
        }
        req = v20_orders.OrderCreate(accountID=self._account_id, data=body)
        resp = self._client.request(req)  # single call — no retry

        fill = resp.get("orderFillTransaction")
        if fill is None:
            reject = resp.get("orderRejectTransaction", {})
            reason = reject.get("rejectReason", "unknown")
            logger.warning("broker: order rejected reason=%s", reason)
            return None

        trade_opened = fill.get("tradeOpened")
        if trade_opened is None:
            logger.error(
                "orderFillTransaction missing tradeOpened: instrument=%s units=%s fill=%r",
                instrument, units, fill,
            )
            raise ValueError(f"unexpected fill response for {instrument}: no tradeOpened")
        opened_at = datetime.fromisoformat(
            fill["time"].replace("Z", "+00:00")
        ).astimezone(UTC)
        return OpenTrade(
            trade_id=str(trade_opened["tradeID"]),
            instrument=instrument,
            units=int(units),
            entry_price=float(fill["price"]),
            stop_price=float(stop),
            take_profit=float(take_profit),
            opened_at=opened_at,
            setup=setup,
        )

    def close_trade(self, trade_id: str) -> float | None:
        """
        Close a trade by ID.

        MONEY-SAFETY: single request, no retry.
        Returns realised P&L (float) or None if fill not found / trade gone.
        """
        import oandapyV20.endpoints.trades as v20_trades  # type: ignore[import-untyped]

        req = v20_trades.TradeClose(accountID=self._account_id, tradeID=trade_id)
        try:
            resp = self._client.request(req)  # single call — no retry
        except V20Error as exc:
            if getattr(exc, "code", None) == 404:
                logger.info("close_trade: trade already gone trade_id=%s", trade_id)
                return None
            logger.error(
                "close_trade FAILED for live trade trade_id=%s error=%r — position may still be open",
                trade_id, exc,
            )
            raise

        fill = resp.get("orderFillTransaction")
        if fill is None:
            logger.warning("broker: close_trade no fill in response trade_id=%s", trade_id)
            return None

        trades_closed = fill.get("tradesClosed", [])
        if not trades_closed:
            logger.error("close_trade: empty tradesClosed trade_id=%s fill=%r", trade_id, fill)
            return None
        return float(trades_closed[0]["realizedPL"])

    def open_trades(self) -> list[OpenTrade]:
        """Return all open trades — retryable read."""
        import oandapyV20.endpoints.trades as v20_trades

        req = v20_trades.OpenTrades(accountID=self._account_id)
        resp = self._request_with_retry(req)
        out: list[OpenTrade] = []
        for t in resp.get("trades", []):
            opened_at = datetime.fromisoformat(
                t["openTime"].replace("Z", "+00:00")
            ).astimezone(UTC)
            stop_price = (
                float(t["stopLossOrder"]["price"]) if "stopLossOrder" in t else 0.0
            )
            take_profit = (
                float(t["takeProfitOrder"]["price"]) if "takeProfitOrder" in t else 0.0
            )
            out.append(
                OpenTrade(
                    trade_id=str(t["id"]),
                    instrument=t["instrument"],
                    units=int(t["currentUnits"]),
                    entry_price=float(t["price"]),
                    stop_price=stop_price,
                    take_profit=take_profit,
                    opened_at=opened_at,
                    setup="",
                )
            )
        return out

    def fetch_candles(
        self, instrument: str, timeframe: str, count: int = 200
    ) -> list[Candle]:
        """Return completed candles oldest→newest — retryable read."""
        gran = _TF_TO_GRAN.get(timeframe)
        if gran is None:
            raise ValueError(
                f"fetch_candles: unknown timeframe {timeframe!r}; "
                f"supported: {list(_TF_TO_GRAN)}"
            )
        import oandapyV20.endpoints.instruments as v20_instruments  # type: ignore[import-untyped]

        req = v20_instruments.InstrumentsCandles(
            instrument=instrument,
            params={"granularity": gran, "count": count, "price": "M"},
        )
        resp = self._request_with_retry(req)
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

    def account_summary(self) -> AccountSummary:
        """Return live account summary — retryable read."""
        import oandapyV20.endpoints.accounts as v20_accounts  # type: ignore[import-untyped]

        req = v20_accounts.AccountSummary(accountID=self._account_id)
        resp = self._request_with_retry(req)
        a = resp["account"]
        return AccountSummary(
            balance=float(a["balance"]),
            nav=float(a["NAV"]),
            currency=a["currency"],
            open_trade_count=int(a.get("openTradeCount", 0)),
        )

    def modify_stop(
        self, trade_id: str, new_stop: float, *, instrument: str | None = None
    ) -> bool:
        """
        Update the stop-loss on a live OANDA position via TradeCRCDO.

        MONEY-SAFETY: single request, no retry. A failed trailing-stop update
        is non-fatal — the original stop remains at the broker.
        """
        import oandapyV20.endpoints.trades as v20_trades

        fmt_instrument = instrument or ""
        data: dict[str, Any] = {
            "stopLoss": {
                "price": _fmt_price(fmt_instrument, new_stop),
                "timeInForce": "GTC",
            }
        }
        req = v20_trades.TradeCRCDO(
            accountID=self._account_id, tradeID=trade_id, data=data
        )
        try:
            resp = self._client.request(req)  # single call — no retry
        except V20Error as exc:
            logger.error(
                "modify_stop FAILED trade_id=%s new_stop=%s error=%r — original stop unchanged at broker",
                trade_id, new_stop, exc,
            )
            return False

        if "stopLossOrderTransaction" not in resp:
            logger.warning(
                "modify_stop: unexpected response (no stopLossOrderTransaction) trade_id=%s resp=%r",
                trade_id, resp,
            )
            return False
        return True
