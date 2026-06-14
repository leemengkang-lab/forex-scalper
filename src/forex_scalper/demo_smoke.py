"""
demo_smoke.py
=============
Proves the whole package wires together: builds synthetic uptrend data that
satisfies bias (LONG_ONLY) + regime (TREND) + Setup A (pullback), then runs one
candle through the orchestrator with a PaperBroker and shows the result.

Run from inside the package folder:  python demo_smoke.py
"""

import logging
from datetime import UTC, datetime, timedelta

from forex_scalper import bias as bias_mod
from forex_scalper import regime as regime_mod
from forex_scalper import setups as setups_mod
from forex_scalper.bot import ScalpBot
from forex_scalper.config import BotConfig
from forex_scalper.data import MarketState
from forex_scalper.execution import PaperBroker
from forex_scalper.models import Candle

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")

INSTR = "EUR_USD"
PIP = 0.0001
PV = 0.00013  # SGD per pip per unit (illustrative)


def candle(t, o, h, l, c):
    return Candle(t, o, h, l, c, complete=True)


def build_market():
    m = MarketState()
    t0 = datetime(2025, 1, 6, 8, 0, tzinfo=UTC)

    # --- 1H uptrend (drives bias LONG_ONLY) ---
    p = 1.0700
    for i in range(60):
        o = p
        c = p + 0.0008
        h = c + 0.0002
        l = o - 0.0002
        m.add_candle(INSTR, "1H", candle(t0 + timedelta(hours=i), o, h, l, c))
        p = c

    # --- 15M trend with EXPANDING range + clean bodies (regime TREND) ---
    p = 1.0750
    for i in range(70):
        rng = 0.0005 + i * 0.00002          # range grows over time -> ATR expands
        o = p
        c = p + rng * 0.8                   # strong up body
        l = o - rng * 0.1
        h = c + rng * 0.1
        m.add_candle(INSTR, "15M", candle(t0 + timedelta(minutes=15 * i), o, h, l, c))
        p = c

    # --- 1M: uptrend, then a pullback into the 20 EMA + bullish rejection ---
    p = 1.0840
    for i in range(40):
        o = p; c = p + 0.0003; h = c + 0.0001; l = o - 0.0001
        m.add_candle(INSTR, "1M", candle(t0 + timedelta(minutes=i), o, h, l, c))
        p = c
    # three small down candles pulling back toward the rising EMA
    for i in range(3):
        o = p; c = p - 0.0004; h = o + 0.0001; l = c - 0.0001
        m.add_candle(INSTR, "1M", candle(t0 + timedelta(minutes=40 + i), o, h, l, c))
        p = c
    # the trigger candle: dips below EMA then closes back up (long lower wick)
    ema = m.ema(INSTR, "1M", 20)
    o = p
    l = ema - 0.0006                         # spike down through the EMA
    c = o + 0.0005                            # close back up
    h = c + 0.0001
    m.add_candle(INSTR, "1M", candle(t0 + timedelta(minutes=43), o, h, l, c))

    # live price + pip value
    m.set_price(INSTR, c - 0.00004, c + 0.00004)  # ~0.8 pip spread
    m.set_pip_value(INSTR, PV)
    return m


def main():
    cfg = BotConfig(starting_balance=10_000.0)
    market = build_market()

    bias = bias_mod.get_bias(market, INSTR, cfg.bias)
    regime = regime_mod.get_regime(market, INSTR, cfg.regime)
    sig = setups_mod.detect(market, INSTR, bias, regime, cfg.setup)
    print(f"\nbias={bias.value}  regime={regime.value}  spread={market.spread_pips(INSTR):.2f}p")
    print(f"signal={sig}")

    broker = PaperBroker(pip_value={INSTR: PV})
    bid, ask = market._bid[INSTR], market._ask[INSTR]
    broker.set_price(INSTR, bid, ask)

    bot = ScalpBot(cfg, market, broker)
    # force a time inside the London/NY overlap so the session gate passes
    now = datetime(2025, 1, 6, 13, 30, tzinfo=UTC)
    bot.on_candle_close(INSTR, now)

    print(f"\nopen trades: {[(t.trade_id, t.units, round(t.entry_price,5)) for t in broker.open_trades()]}")
    print(f"risk state: heat={bot.risk.gross_heat:.0f}  netUSD={bot.risk.net_usd_exposure:+.0f}")
    print("\n--- journal.csv tail ---")
    with open(cfg.journal_path) as f:
        print("".join(f.readlines()[-4:]))


if __name__ == "__main__":
    main()
