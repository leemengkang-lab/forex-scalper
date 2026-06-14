"""
backtest.py
===========
Replays historical 1-minute candles through the SAME pipeline the live bot uses
(bot.on_candle_close), so what you validate is exactly what you'll run.

It does three things the live path doesn't need:
  1. aggregates 1M -> 15M and 1H (so you only feed one clean 1M file),
  2. resolves stop-loss / take-profit intrabar against each candle's high/low,
  3. routes every close back to the risk manager + a stats collector.

Walk-forward: split the data by date into IN-SAMPLE (older) and OUT-OF-SAMPLE
(newer) and report both side by side. If a setting shines in-sample but dies
out-of-sample, it was memorised noise — exactly the per-pair-override / 7% trap.

Run:  python backtest.py            # uses built-in synthetic data
      python backtest.py mydata.csv 2025-04-01   # your CSV, split date
CSV columns: time,open,high,low,close   (time = ISO 8601, UTC)
"""

from __future__ import annotations

import csv
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from forex_scalper import bias as bias_mod
from forex_scalper import regime as regime_mod
from forex_scalper import setups as setups_mod
from forex_scalper.bot import ScalpBot
from forex_scalper.config import BotConfig
from forex_scalper.data import MarketState
from forex_scalper.execution import Broker, OpenTrade
from forex_scalper.models import Candle, pip_size

logging.basicConfig(level=logging.WARNING)  # quiet during replay


# --------------------------------------------------------------------------- #
# Backtest broker: deterministic fills + intrabar SL/TP resolution
# --------------------------------------------------------------------------- #
class BacktestBroker(Broker):
    def __init__(self, pip_value: Dict[str, float], spread_pips: float = 0.6):
        self._pip_value = pip_value
        self._spread = spread_pips
        self._trades: Dict[str, OpenTrade] = {}
        self._n = 0
        self.now: datetime = datetime.now(timezone.utc)
        self.on_close = lambda trade, pnl, reason: None  # set by Backtester

    def set_price_from_close(self, instrument: str, close: float) -> None:
        half = self._spread / 2 * pip_size(instrument)
        self._bid = getattr(self, "_bid", {})
        self._ask = getattr(self, "_ask", {})
        self._bid[instrument] = close - half
        self._ask[instrument] = close + half

    def get_price(self, instrument: str) -> Tuple[float, float]:
        return self._bid[instrument], self._ask[instrument]

    def place_market_order(self, instrument, units, stop, take_profit, setup="") -> Optional[OpenTrade]:
        bid, ask = self.get_price(instrument)
        fill = ask if units > 0 else bid
        self._n += 1
        t = OpenTrade(f"B{self._n}", instrument, units, fill, stop, take_profit, self.now, setup)
        self._trades[t.trade_id] = t
        return t

    def close_trade(self, trade_id: str, reason: str = "time_stop") -> Optional[float]:
        t = self._trades.pop(trade_id, None)
        if not t:
            return None
        bid, ask = self.get_price(t.instrument)
        exit_price = bid if t.units > 0 else ask
        pnl = self._pnl(t, exit_price)
        self.on_close(t, pnl, reason)
        return pnl

    def open_trades(self) -> List[OpenTrade]:
        return list(self._trades.values())

    # called by the engine for every new candle, before new-entry detection
    def resolve_candle(self, instrument: str, c: Candle) -> None:
        for tid, t in list(self._trades.items()):
            if t.instrument != instrument:
                continue
            if t.units > 0:   # long: stop below, tp above. Stop checked first (pessimistic).
                if c.low <= t.stop_price:
                    self._settle(tid, t, t.stop_price, "stop")
                elif c.high >= t.take_profit:
                    self._settle(tid, t, t.take_profit, "target")
            else:             # short
                if c.high >= t.stop_price:
                    self._settle(tid, t, t.stop_price, "stop")
                elif c.low <= t.take_profit:
                    self._settle(tid, t, t.take_profit, "target")

    def _settle(self, tid, t, price, reason):
        self._trades.pop(tid, None)
        self.on_close(t, self._pnl(t, price), reason)

    def _pnl(self, t: OpenTrade, exit_price: float) -> float:
        pip = pip_size(t.instrument)
        pips = (exit_price - t.entry_price) / pip * (1 if t.units > 0 else -1)
        return pips * abs(t.units) * self._pip_value.get(t.instrument, 0.0)


# --------------------------------------------------------------------------- #
# Aggregation 1M -> higher timeframes
# --------------------------------------------------------------------------- #
def aggregate(candles_1m: List[Candle], minutes: int) -> List[Tuple[datetime, Candle]]:
    """Returns (bucket_end_time, aggregated_candle). bucket_end is when it completes."""
    buckets: Dict[datetime, List[Candle]] = defaultdict(list)
    for c in candles_1m:
        start = c.time.replace(second=0, microsecond=0)
        floored = start - timedelta(minutes=start.minute % minutes,
                                    hours=0) if minutes < 60 else start.replace(minute=0)
        buckets[floored].append(c)
    out = []
    for start in sorted(buckets):
        grp = buckets[start]
        agg = Candle(start, grp[0].open, max(x.high for x in grp),
                     min(x.low for x in grp), grp[-1].close, complete=True)
        out.append((start + timedelta(minutes=minutes), agg))
    return out


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #
@dataclass
class TradeRecord:
    time: datetime
    instrument: str
    setup: str
    pnl: float
    r: float
    reason: str


def report(name: str, trades: List[TradeRecord], start_bal: float) -> None:
    if not trades:
        print(f"\n[{name}]  no trades")
        return
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_p = sum(t.pnl for t in wins)
    gross_l = -sum(t.pnl for t in losses)
    net = sum(t.pnl for t in trades)
    avg_r = sum(t.r for t in trades) / len(trades)
    pf = (gross_p / gross_l) if gross_l > 0 else float("inf")
    # equity + max drawdown
    eq = start_bal; peak = eq; mdd = 0.0
    for t in trades:
        eq += t.pnl; peak = max(peak, eq); mdd = min(mdd, eq / peak - 1)
    by_setup = defaultdict(int)
    for t in trades:
        by_setup[t.setup] += 1

    print(f"\n[{name}]")
    print(f"  trades         {len(trades)}   (A:{by_setup.get('A',0)} B:{by_setup.get('B',0)})")
    print(f"  win rate       {len(wins)/len(trades)*100:.1f}%")
    print(f"  avg R / trade  {avg_r:+.3f}R")
    print(f"  profit factor  {pf:.2f}")
    print(f"  net P&L        {net:+.2f}  ({net/start_bal*100:+.1f}%)")
    print(f"  max drawdown   {mdd*100:.1f}%")


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class Backtester:
    def __init__(self, cfg: BotConfig, instrument: str, pip_value: float):
        self.cfg = cfg
        self.instrument = instrument
        self.pip_value = pip_value
        self.broker = BacktestBroker({instrument: pip_value})
        self.market = MarketState()
        self.market.set_pip_value(instrument, pip_value)
        self.bot = ScalpBot(cfg, self.market, self.broker)
        self.broker.on_close = self._on_close
        self.records: List[TradeRecord] = []

    def _on_close(self, t: OpenTrade, pnl: float, reason: str):
        pip = pip_size(t.instrument)
        one_r = abs(t.entry_price - t.stop_price) / pip * abs(t.units) * self.pip_value
        r = pnl / one_r if one_r > 0 else 0.0
        self.records.append(TradeRecord(self.broker.now, t.instrument, t.setup, pnl, r, reason))
        # route to risk manager: frees heat, updates daily P&L + kill switch
        self.bot.risk.close_position(t.instrument, pnl, now=self.broker.now)

    def run(self, candles_1m: List[Candle]):
        agg15 = aggregate(candles_1m, 15)
        agg60 = aggregate(candles_1m, 60)
        i15 = i60 = 0
        for c in candles_1m:
            t = c.time
            self.broker.now = t
            self.broker.set_price_from_close(self.instrument, c.close)
            self.market.set_price(self.instrument, *self.broker.get_price(self.instrument))

            # add any higher-TF candles that completed by now (no lookahead)
            while i15 < len(agg15) and agg15[i15][0] <= t:
                self.market.add_candle(self.instrument, "15M", agg15[i15][1]); i15 += 1
            while i60 < len(agg60) and agg60[i60][0] <= t:
                self.market.add_candle(self.instrument, "1H", agg60[i60][1]); i60 += 1

            # 1) resolve open trades against this candle, 2) add candle, 3) detect
            self.broker.resolve_candle(self.instrument, c)
            self.market.add_candle(self.instrument, "1M", c)
            self.bot.on_candle_close(self.instrument, t)


def split_by_date(candles: List[Candle], cutoff: datetime):
    return ([c for c in candles if c.time < cutoff],
            [c for c in candles if c.time >= cutoff])


# --------------------------------------------------------------------------- #
# Synthetic data (so it runs with no file): trending session-days w/ pullbacks
# --------------------------------------------------------------------------- #
def synth(days: int = 30, instrument: str = "EUR_USD") -> List[Candle]:
    out: List[Candle] = []
    price = 1.0800
    day0 = datetime(2025, 1, 6, tzinfo=timezone.utc)
    for d in range(days):
        base = day0 + timedelta(days=d)
        drift = 0.00010 if (d % 4 != 3) else -0.00008   # mostly up, occasional down day
        for m in range(240):                            # 12:00-15:59 UTC session
            t = base + timedelta(hours=12, minutes=m)
            rng = 0.00035 + (m / 240) * 0.00060          # intraday vol expansion -> TREND
            if m % 28 in (0, 1, 2):                       # pullback dips
                o = price; c = price - rng * 0.7
                h = o + rng * 0.1; l = c - rng * 0.1
            elif m % 28 == 3:                             # rejection candle (bullish)
                o = price; l = price - rng * 1.1; c = o + rng * 0.6; h = c + rng * 0.1
            else:                                         # trend candle
                o = price; c = price + drift + rng * 0.5; h = c + rng * 0.1; l = o - rng * 0.1
            out.append(Candle(t, round(o, 5), round(h, 5), round(l, 5), round(c, 5)))
            price = c
    return out


def load_csv(path: str) -> List[Candle]:
    out = []
    with open(path) as f:
        for row in csv.DictReader(f):
            out.append(Candle(
                datetime.fromisoformat(row["time"]).replace(tzinfo=timezone.utc),
                float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])))
    return out


def main():
    instrument = "EUR_USD"
    pip_value = 0.00013
    cfg = BotConfig(starting_balance=10_000.0)

    if len(sys.argv) >= 2:
        candles = load_csv(sys.argv[1])
        cutoff = datetime.fromisoformat(sys.argv[2]).replace(tzinfo=timezone.utc) \
            if len(sys.argv) >= 3 else candles[len(candles) * 2 // 3].time
    else:
        candles = synth(30, instrument)
        cutoff = candles[len(candles) * 2 // 3].time   # 2/3 in-sample, 1/3 out

    is_c, oos_c = split_by_date(candles, cutoff)
    print(f"data: {len(candles)} 1M candles | split @ {cutoff.date()} "
          f"({len(is_c)} IS / {len(oos_c)} OOS)")

    bt_is = Backtester(cfg, instrument, pip_value); bt_is.run(is_c)
    bt_oos = Backtester(cfg, instrument, pip_value); bt_oos.run(oos_c)

    report("IN-SAMPLE", bt_is.records, cfg.starting_balance)
    report("OUT-OF-SAMPLE", bt_oos.records, cfg.starting_balance)

    print("\nRead it like a vet: if OOS win rate / avg R / profit factor hold up")
    print("close to IN-SAMPLE, the edge is real. If they collapse, you curve-fit.")


if __name__ == "__main__":
    main()
