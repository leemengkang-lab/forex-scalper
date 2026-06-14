# Forex Scalper — Live Build-Out Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the paper-only scalping scaffold (`C:\Users\User\Downloads\files (2)`) into a deployable, live-capable OANDA trading bot that replaces the losing `Forexbot`, by fusing the scaffold's multi-timeframe / multi-pair *trading brain* onto the proven *live machinery* already battle-tested in `Forexbot` (OANDA adapter, stop-loss watchdog, startup reconciliation, persistence, Telegram, systemd).

**Architecture:** A `src/forex_scalper` package. The scaffold modules (`models, indicators, data, bias, regime, setups, session, risk_manager, trade_manager, journal, notifier, execution, bot, config`) become the brain. The live loop, OANDA REST/stream adapters, SQLite persistence, reconciliation, watchdog, and Telegram control are ported and adapted from `Forexbot/src/forex_bot` to work against the scaffold's `Broker` interface and `MarketState`, extended from single-instrument to the scaffold's multi-instrument design. Nothing trades real money until a real-data walk-forward validation gate passes AND `config.live` is explicitly flipped.

**Tech Stack:** Python 3.13, `oandapyV20` 0.7.2 (already installed), `pydantic` (typed config), `structlog`, `python-telegram-bot`, `pytest` + `pytest-asyncio`, `ruff`, `mypy`, `uv` (lockfile), Docker + systemd on the existing GCP e2-micro VM.

---

## Assumptions — CONFIRM BEFORE EXECUTING PHASE 2+

These are sensible defaults taken from the scaffold, the old bot, and memory. Confirm before wiring anything that touches the broker:

1. **Project root:** `C:\Users\User\forex-scalper`, `src/`-layout package named `forex_scalper`.
2. **OANDA account currency:** the scaffold comments say **SGD**. `pip_value_per_unit` math depends on this. We compute it generically from the live account currency + cross rates, so it's correct whatever the account ccy actually is. Confirm the real account ccy.
3. **Credentials:** reuse the existing OANDA token/account and Telegram bot token/chat id from `Forexbot/.env` (env var names: `OANDA_TOKEN`, `OANDA_ACCOUNT_ID`, `OANDA_ENVIRONMENT`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_IDS`).
4. **Practice first:** all of demo + shadow runs on `OANDA_ENVIRONMENT=practice`. Live is a separate account/token, flipped only at cutover.
5. **Instruments:** the scaffold's 5 pairs (`EUR_USD, AUD_USD, USD_JPY, NZD_USD, USD_CHF`) for backtest/demo. **Live phase 1 starts on ONE pair at micro size** per the rollout, then scales.
6. **Deploy target:** the same GCP e2-micro free-tier VM the old bot uses.
7. **Old bot retirement is SEQUENCED, not immediate:** archive first, delete only after the new bot is proven live (see Phase 9). The old bot's OANDA code is our reference during the build.

---

## File Structure

New project `C:\Users\User\forex-scalper`:

```
forex-scalper/
  pyproject.toml                # deps, ruff, mypy, pytest config (adapt from Forexbot)
  uv.lock
  Makefile                      # install / test / lint / backtest / smoke (adapt from Forexbot)
  Dockerfile                    # adapt from Forexbot
  .env.example                  # same env var names as Forexbot
  .gitignore
  README.md
  config/
    demo.yaml                   # practice env, multi-pair, live=false
    live.yaml                   # live env, single pair micro, live=true (created at cutover)
    backtest.yaml
  systemd/
    forex-scalper.service       # adapt from Forexbot/systemd
  data/                         # sqlite db + backups (gitignored)
  docs/
    runbook.md
    superpowers/plans/2026-06-14-forex-scalper-live-buildout.md  # this file
  src/forex_scalper/
    __init__.py
    models.py                   # from scaffold (verbatim)
    indicators.py               # from scaffold (verbatim)
    data.py                     # from scaffold + pip_value computation (Phase 3)
    bias.py                     # from scaffold (verbatim)
    regime.py                   # from scaffold (verbatim)
    setups.py                   # from scaffold (verbatim)
    session.py                  # from scaffold + calendar loader (Phase 6)
    risk_manager.py             # from scaffold (verbatim; single source of risk truth)
    trade_manager.py            # from scaffold + push trailing stop to broker (Phase 2)
    journal.py                  # from scaffold (verbatim)
    notifier.py                 # from scaffold; Telegram impl reused from Forexbot (Phase 7)
    config.py                   # from scaffold, extended to load env/yaml like Forexbot
    bot.py                      # from scaffold orchestrator (on_candle_close kept)
    execution.py                # PaperBroker (verbatim) + OandaBroker IMPLEMENTED (Phase 2)
    backtest.py                 # from scaffold + real-CSV loader hardening (Phase 8)
    pip_value.py                # NEW: account-ccy pip value from quotes (Phase 3)
    stream.py                   # NEW: OANDA pricing stream -> per-instrument candles (Phase 4)
    persistence.py              # NEW: sqlite repo (open trades, halt flag, day snapshot, events) (Phase 5)
    reconcile.py                # NEW: broker<->db reconciliation at startup (Phase 5)
    live.py                     # NEW: async live loop, watchdog, rollover, telegram (Phase 4/7)
  tests/
    test_indicators.py          # Phase 1
    test_risk_manager.py        # Phase 1
    test_bias_regime_setups.py  # Phase 1
    test_oanda_broker.py        # Phase 2 (fake API client)
    test_pip_value.py           # Phase 3
    test_stream_aggregator.py   # Phase 4
    test_persistence.py         # Phase 5
    test_reconcile.py           # Phase 5
    test_session_calendar.py    # Phase 6
    test_live_smoke.py          # Phase 4 (paper end-to-end through live loop)
```

**Why a package, not flat files:** the scaffold uses flat imports (`import bias`). Converting to a package with explicit imports (`from forex_scalper import bias`) is mechanical and unlocks clean pytest, mypy, packaging, and systemd. Task 1.2 does this conversion once.

---

## Phase 0 — Safety & Baseline (do FIRST, before any build)

### Task 0.1: Archive the old bot (reversible safety net)

**Files:** none created in repo; produces an archive.

- [ ] **Step 1: Commit the old bot's current state to git (if not already clean)**

Run:
```bash
cd "C:/Users/User/Forexbot" && git add -A && git status
```
If it's a git repo with changes, commit: `git commit -m "snapshot before forex-scalper migration"`. If it is NOT a git repo, init one: `git init && git add -A && git commit -m "snapshot before migration"`.

- [ ] **Step 2: Tag the snapshot**

Run: `cd "C:/Users/User/Forexbot" && git tag pre-scalper-migration-2026-06-14`
Expected: tag created, `git tag` lists it.

- [ ] **Step 3: Make a zip archive outside the repo**

Run (PowerShell):
```powershell
Compress-Archive -Path "C:\Users\User\Forexbot\*" -DestinationPath "C:\Users\User\Forexbot-archive-2026-06-14.zip" -Force
```
Expected: `Forexbot-archive-2026-06-14.zip` exists and is > 100KB.

- [ ] **Step 4: Confirm the live bot's current run state**

Determine whether the live `forex-bot.service` on the GCP VM is currently trading (SSH: `systemctl status forex-bot`). Record the answer in `docs/runbook.md`. **Do not stop it yet** — it stays as the incumbent until the new bot is proven (Phase 9 handles flatten + cutover).

### Task 0.2: Create the new repo skeleton

**Files:**
- Create: `C:\Users\User\forex-scalper\.gitignore`, `pyproject.toml`, `Makefile`, `.env.example`, `README.md`

- [ ] **Step 1: git init the new project**

Run: `cd "C:/Users/User/forex-scalper" && git init`

- [ ] **Step 2: Write `.gitignore`** (copy from `Forexbot/.gitignore`, ensure it includes `.env`, `data/`, `__pycache__/`, `.venv/`, `*.pyc`, `journal.csv`).

- [ ] **Step 3: Write `pyproject.toml`** — adapt `Forexbot/pyproject.toml`: package name `forex-scalper`, src layout, deps `oandapyV20`, `pydantic`, `pyyaml`, `structlog`, `python-telegram-bot`, dev deps `pytest`, `pytest-asyncio`, `ruff`, `mypy`. (Read the old file and reproduce its tool config sections verbatim, changing only the name and adding `pytest-asyncio`.)

- [ ] **Step 4: Write `.env.example`** identical to `Forexbot/.env.example`.

- [ ] **Step 5: Create venv and install**

Run:
```bash
cd "C:/Users/User/forex-scalper" && python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"
```
Expected: install succeeds; `.venv/Scripts/python -c "import oandapyV20, pydantic, structlog, telegram"` prints nothing (no error).

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "chore: project skeleton"
```

---

## Phase 1 — Land the scaffold as a tested package (lock the baseline)

The scaffold has ZERO tests. Before changing any logic we pin its current behavior with characterization tests, so later wiring can't silently break the brain.

### Task 1.1: Copy scaffold modules into the package

**Files:**
- Create: `src/forex_scalper/{models,indicators,data,bias,regime,setups,session,risk_manager,trade_manager,journal,notifier,config,bot,execution,backtest}.py`, `src/forex_scalper/__init__.py`

- [ ] **Step 1: Copy the 16 scaffold modules**

Run (PowerShell):
```powershell
$src = "C:\Users\User\Downloads\files (2)"
$dst = "C:\Users\User\forex-scalper\src\forex_scalper"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item "$src\*.py" $dst
"" | Out-File "$dst\__init__.py" -Encoding utf8
```
Expected: 16 `.py` files + `__init__.py` in the package dir.

- [ ] **Step 2: Commit the verbatim copy** (so the import rewrite in 1.2 is a reviewable diff)

```bash
git add -A && git commit -m "vendor: scaffold modules verbatim"
```

### Task 1.2: Convert flat imports to package imports

**Files:** Modify every `src/forex_scalper/*.py` that does flat imports.

The scaffold imports look like `import bias as bias_mod`, `from data import MarketState`, `import indicators as ind`. Convert to package-relative: `from forex_scalper import bias as bias_mod`, `from forex_scalper.data import MarketState`, `from forex_scalper import indicators as ind`.

- [ ] **Step 1: Rewrite imports**

Edit each file. The cross-module imports to fix (grep for them): `bias`, `regime`, `setups`, `data`, `models`, `indicators`, `config`, `execution`, `journal`, `notifier`, `risk_manager`, `session`, `trade_manager`, `bot`. Also fix `bias._ema_at` which does `import indicators as ind` inside the function → `from forex_scalper import indicators as ind`.

- [ ] **Step 2: Verify the package imports cleanly**

Run:
```bash
cd "C:/Users/User/forex-scalper" && .venv/Scripts/python -c "from forex_scalper import bot, backtest, risk_manager; print('ok')"
```
Expected: prints `ok`.

- [ ] **Step 3: Verify the scaffold demos still run**

Run:
```bash
.venv/Scripts/python -m forex_scalper.risk_manager
.venv/Scripts/python -m forex_scalper.backtest
```
Expected: risk demo prints the gate sequence ending `halted=True`; backtest prints IN-SAMPLE / OUT-OF-SAMPLE reports. (`demo_smoke.py` writes `journal.csv`; adapt it later or skip.)

- [ ] **Step 4: Commit** — `git commit -am "refactor: package-relative imports"`

### Task 1.3: Characterization tests for the brain

**Files:**
- Test: `tests/test_indicators.py`, `tests/test_risk_manager.py`, `tests/test_bias_regime_setups.py`

- [ ] **Step 1: Write `tests/test_indicators.py`** — pin EMA seeding, Wilder ATR, swings, body ratio against hand-computed values.

```python
from forex_scalper.indicators import ema, atr, recent_swing_low, avg_body_ratio
from forex_scalper.models import Candle
from datetime import datetime, timezone

def _c(o, h, l, c):
    return Candle(datetime(2025, 1, 1, tzinfo=timezone.utc), o, h, l, c)

def test_ema_returns_none_when_too_short():
    assert ema([1.0, 2.0], 5) is None

def test_ema_seeds_with_sma_then_smooths():
    vals = [1.0] * 5 + [2.0]
    # seed SMA=1.0 over first 5, then one step toward 2.0 with k=2/6
    assert abs(ema(vals, 5) - (2.0 * (2/6) + 1.0 * (1 - 2/6))) < 1e-12

def test_atr_needs_period_plus_one():
    assert atr([_c(1,1,1,1)] * 5, 14) is None

def test_swing_low_picks_min_over_lookback():
    cs = [_c(1,2,0.9,1.5), _c(1,2,0.7,1.5), _c(1,2,1.1,1.5)]
    assert recent_swing_low(cs, 3) == 0.7
```

- [ ] **Step 2: Run it — expect PASS** (these pin existing behavior). Run: `.venv/Scripts/python -m pytest tests/test_indicators.py -v`

- [ ] **Step 3: Write `tests/test_risk_manager.py`** — pin the veto ladder: spread cap, duplicate, max positions, gross-heat cap, USD factor cap, daily kill switch, loss-streak de-risk, stop-floor widening, unit sizing. (Mirror the scenarios in `risk_manager.__main__`, but as asserts.)

```python
from forex_scalper.risk_manager import RiskManager, RiskConfig, TradeSignal, Reject

PV = 0.00013

def test_clean_long_is_approved_and_sized():
    rm = RiskManager(RiskConfig(), balance=10_000)
    d = rm.evaluate(TradeSignal("EUR_USD", +1, 1.0850, 1.0838, PV, spread_pips=0.4))
    assert d.approved and d.units > 0 and d.stop_pips >= 10.0

def test_wide_spread_rejected():
    rm = RiskManager(RiskConfig(), balance=10_000)
    d = rm.evaluate(TradeSignal("GBP_USD", +1, 1.2730, 1.2718, PV, spread_pips=2.1))
    assert not d.approved and d.reason is Reject.SPREAD

def test_factor_cap_blocks_third_correlated_usd_bet():
    rm = RiskManager(RiskConfig(), balance=10_000)
    for ins in ("EUR_USD", "AUD_USD"):
        d = rm.evaluate(TradeSignal(ins, +1, 1.0, 0.9988, PV, spread_pips=0.4))
        assert d.approved
        rm.register_fill(ins, +1, d.risk_amount)
    d = rm.evaluate(TradeSignal("NZD_USD", +1, 0.60, 0.5988, PV, spread_pips=0.4))
    assert not d.approved and d.reason is Reject.FACTOR_EXPOSURE

def test_daily_loss_limit_halts():
    rm = RiskManager(RiskConfig(), balance=10_000)
    rm.close_position("EUR_USD", pnl=-310)
    assert rm.halted
    d = rm.evaluate(TradeSignal("USD_CHF", +1, 0.89, 0.8888, PV, spread_pips=0.4))
    assert not d.approved and d.reason in (Reject.HALTED, Reject.DAILY_LIMIT)
```

- [ ] **Step 4: Run — expect PASS.** `.venv/Scripts/python -m pytest tests/test_risk_manager.py -v`

- [ ] **Step 5: Write `tests/test_bias_regime_setups.py`** — build the synthetic uptrend from `demo_smoke.build_market()` (reuse it) and assert `get_bias == LONG_ONLY`, `get_regime == TREND`, `detect` returns a Setup A long with a stop below entry.

- [ ] **Step 6: Run — expect PASS.** Then run the full suite: `.venv/Scripts/python -m pytest -v`

- [ ] **Step 7: Commit** — `git commit -am "test: characterization tests pin the trading brain"`

---

## Phase 2 — Implement `OandaBroker` (real fills, TDD with a fake client)

The scaffold's `execution.OandaBroker` is four `NotImplementedError` stubs. We implement them against the scaffold's `Broker` interface (`get_price`, `place_market_order`, `close_trade`, `open_trades`), porting the proven v20 calls from `Forexbot/src/forex_bot/broker/oanda.py` (OrderCreate with `stopLossOnFill`+`takeProfitOnFill`, TradeClose, OpenTrades, PricingInfo) and adding the retry/backoff wrapper. **Idempotency:** attach `clientExtensions.id` to each order so a retry never double-fires.

### Task 2.1: Fake OANDA client + broker contract tests

**Files:**
- Test: `tests/test_oanda_broker.py`

- [ ] **Step 1: Write a `FakeClient`** that records requests and returns canned v20 responses (fill transaction, reject transaction, open trades, pricing). Then write failing tests:

```python
from forex_scalper.execution import OandaBroker
from forex_scalper.models import pip_size

class FakeClient:
    def __init__(self, responses): self.responses, self.requests = list(responses), []
    def request(self, req):
        self.requests.append(req)
        return self.responses.pop(0)

def _fill_resp(trade_id="101", price=1.0850):
    return {"orderFillTransaction": {"price": str(price), "time": "2025-01-06T13:30:00.000000Z",
            "tradeOpened": {"tradeID": trade_id}}}

def test_place_market_order_sends_market_with_sl_tp_and_returns_trade(monkeypatch):
    b = OandaBroker.__new__(OandaBroker)        # bypass real API construction
    b._client = FakeClient([_fill_resp()]); b._account_id = "X"; b._sleeps = ()
    t = b.place_market_order("EUR_USD", units=1000, stop=1.0838, take_profit=1.0890, setup="A")
    sent = b._client.requests[0].data["order"]
    assert sent["type"] == "MARKET" and sent["units"] == "1000"
    assert sent["stopLossOnFill"]["price"] == "1.08380"
    assert sent["takeProfitOnFill"]["price"] == "1.08900"
    assert "id" in sent["clientExtensions"]          # idempotency key present
    assert t.trade_id == "101" and t.units == 1000

def test_rejected_order_returns_none():
    b = OandaBroker.__new__(OandaBroker)
    b._client = FakeClient([{"orderRejectTransaction": {"rejectReason": "MARKET_HALTED"}}])
    b._account_id = "X"; b._sleeps = ()
    assert b.place_market_order("EUR_USD", 1000, 1.0838, 1.0890) is None
```

- [ ] **Step 2: Run — expect FAIL** (`NotImplementedError`). `.venv/Scripts/python -m pytest tests/test_oanda_broker.py -v`

### Task 2.2: Implement the four methods

**Files:** Modify `src/forex_scalper/execution.py` (`OandaBroker`).

- [ ] **Step 1: Implement** `__init__` (lazy `oandapyV20.API`, store account/env, retry sleeps), `place_market_order` (OrderCreate MARKET/FOK + `stopLossOnFill` + `takeProfitOnFill` + `clientExtensions.id=uuid4`; parse `orderFillTransaction.tradeOpened.tradeID`; return `OpenTrade`; on `orderRejectTransaction` log + return `None`), `close_trade` (TradeClose → parse `realizedPL`), `open_trades` (OpenTrades → map to `OpenTrade`), `get_price` (PricingInfo → `(bid, ask)`). Port the `_request_with_retry` wrapper and `_is_retryable` from `Forexbot/src/forex_bot/broker/oanda.py:28-74`.

Reference signature mapping (old → scaffold): old `place_order(Order)->Fill` becomes scaffold `place_market_order(instrument, units, stop, take_profit, setup)->OpenTrade`; old `close_position(id)->CloseResult` becomes `close_trade(id)->float` (return `realized_pnl`); old `get_open_positions()->list[Position]` becomes `open_trades()->list[OpenTrade]`; old `current_price(instrument, side)` becomes `get_price(instrument)->(bid, ask)` (request both bids/asks).

- [ ] **Step 2: Run — expect PASS.** `.venv/Scripts/python -m pytest tests/test_oanda_broker.py -v`

- [ ] **Step 3: mypy + ruff clean.** `.venv/Scripts/python -m mypy src/forex_scalper/execution.py && .venv/Scripts/python -m ruff check src/forex_scalper/execution.py`

- [ ] **Step 4: Commit** — `git commit -am "feat: implement OandaBroker (idempotent market order + SL/TP, retry)"`

### Task 2.3: trade_manager pushes trailing stop to broker

**Files:** Modify `src/forex_scalper/trade_manager.py` (the `TODO(live)` at the trailing branch), `src/forex_scalper/execution.py` (add `modify_stop(trade_id, new_stop)` to `Broker` + both impls).

- [ ] **Step 1: Write failing test** in `tests/test_oanda_broker.py`: `modify_stop` issues a `TradeCRCDO`/`StopLossOrder` mutation with the new price.
- [ ] **Step 2: Add `modify_stop` to the `Broker` ABC, `PaperBroker` (mutate in-memory), and `OandaBroker` (v20 `trades.TradeCRCDO`).** Wire `trade_manager._manage_one` to call `self.broker.modify_stop(...)` after improving the stop.
- [ ] **Step 3: Run — PASS.** **Step 4: Commit.**

---

## Phase 3 — Pip value in account currency (`pip_value_per_unit`)

`data.MarketState.pip_value_per_unit` returns `0.0` (a hard TODO). With it zero, the risk manager rejects every trade (`loss_per_unit <= 0`). We compute the account-ccy value of 1 pip per 1 unit from live quotes, generically for any account ccy.

```
★ math: pip_value_per_unit = pip_size(instrument) * (units of ACCOUNT_CCY per 1 unit of QUOTE_CCY)
  - QUOTE ccy == account ccy            -> factor 1
  - else need QUOTE_CCY/ACCOUNT_CCY rate (from a cross quote, e.g. account=SGD,
    EUR_USD quote=USD -> multiply by USD_SGD)
```

### Task 3.1: pip_value module (TDD, pure)

**Files:** Create `src/forex_scalper/pip_value.py`; Test `tests/test_pip_value.py`.

- [ ] **Step 1: Failing tests**

```python
from forex_scalper.pip_value import pip_value_per_unit

def test_usd_account_usd_quoted_pair():
    # account USD, EUR_USD quote ccy = USD -> pip value per unit = 0.0001
    assert abs(pip_value_per_unit("EUR_USD", account_ccy="USD",
               rate_to_account=lambda ccy: 1.0) - 0.0001) < 1e-9

def test_sgd_account_usd_quoted_pair_uses_usd_sgd():
    # account SGD, quote USD, USD_SGD=1.35 -> 0.0001 * 1.35
    rates = {"USD": 1.35, "SGD": 1.0}
    pv = pip_value_per_unit("EUR_USD", account_ccy="SGD",
                            rate_to_account=lambda ccy: rates[ccy])
    assert abs(pv - 0.0001 * 1.35) < 1e-9

def test_jpy_pair_uses_001_pip_and_quote_jpy():
    rates = {"JPY": 1/110 * 1.35, "SGD": 1.0}   # JPY->SGD
    pv = pip_value_per_unit("USD_JPY", account_ccy="SGD",
                            rate_to_account=lambda ccy: rates["JPY"])
    assert abs(pv - 0.01 * rates["JPY"]) < 1e-12
```

- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** `pip_value_per_unit(instrument, account_ccy, rate_to_account)` = `pip_size(instrument) * rate_to_account(quote_ccy(instrument))`, where `quote_ccy` = chars after the `_`.
- [ ] **Step 4: Run — PASS. Step 5: Commit.**

### Task 3.2: Wire it into MarketState from live quotes

**Files:** Modify `src/forex_scalper/data.py` (`pip_value_per_unit`) + a small `RateBook` that the live loop refreshes from OANDA pricing.

- [ ] **Step 1: Add `MarketState.set_account_ccy(ccy)` and have `pip_value_per_unit(instrument)` use the cached cross rate** (refreshed by the live loop each candle via a pricing call; for `*_USD`/`USD_*` pairs the needed cross is usually already streaming). Keep the existing `set_pip_value` override path for backtests.
- [ ] **Step 2: Test** that `MarketState` returns the computed value when an account ccy + rate are set, and `0.0` only when unset (preserving the "wire it before live" guard).
- [ ] **Step 3: Run — PASS. Step 4: Commit.**

---

## Phase 4 — Live loop, streaming, multi-instrument orchestration

Port `Forexbot`'s async architecture (producer thread → `asyncio.Queue` → consumer; watchdog; rollover) and adapt the single-instrument design to the scaffold's instrument list. `bot.on_candle_close(instrument)` is the per-candle entrypoint and stays as-is.

### Task 4.1: Streaming source → per-instrument 1M candles

**Files:** Create `src/forex_scalper/stream.py`; Test `tests/test_stream_aggregator.py`.

- [ ] **Step 1: Failing test** for a `CandleAggregator` (port from `Forexbot/.../oanda_stream.py:50-101`) that buckets ticks into 1M candles and emits the prior candle on boundary crossing; reset() drops partial buckets. (Multi-instrument: one aggregator per instrument.)
- [ ] **Step 2: Implement** `MultiInstrumentStream` wrapping `oandapyV20` `PricingStream` with `params={"instruments": ",".join(instruments)}`, routing each PRICE msg to the right aggregator, yielding `(instrument, Candle)`; reuse the reconnect/backoff + `StreamReconnectExhausted` logic from the old file.
- [ ] **Step 3: Run — PASS. Step 4: Commit.**

### Task 4.2: Higher-timeframe aggregation (1M→15M, 1M→1H) live

**Files:** Modify `src/forex_scalper/stream.py` or add to `live.py`.

- [ ] **Step 1: Failing test:** feeding 1M candles, the 15M/1H aggregators emit completed higher-TF candles at the right boundaries (reuse `backtest.aggregate` logic, but online). MarketState then has 1M/15M/1H for bias+regime.
- [ ] **Step 2: Implement** an online HTF aggregator per instrument that pushes completed 15M/1H candles into `MarketState` before `on_candle_close` runs (no lookahead).
- [ ] **Step 3: Run — PASS. Step 4: Commit.**

### Task 4.3: Async live loop (`live.py`)

**Files:** Create `src/forex_scalper/live.py`; Test `tests/test_live_smoke.py`.

- [ ] **Step 1: Smoke test** that drives the whole loop with a fake stream (in-process) + `PaperBroker` for two instruments, asserting an order is placed and journalled when a setup fires — i.e., the live path produces the same result as `demo_smoke` but through `live.py`.
- [ ] **Step 2: Implement** `live.py` adapting `Forexbot/.../main_live.py`: producer thread from `MultiInstrumentStream`; consumer that, per `(instrument, candle)`, updates `MarketState` (candle + bid/ask + refreshed pip value), runs HTF aggregation, then `bot.on_candle_close(instrument)`; a `--max-runtime-seconds` smoke flag; clean shutdown. Reuse `bot.ScalpBot` as-is (it already owns the pipeline + risk).
- [ ] **Step 3: Run — PASS. Step 4: Commit.**

### Task 4.4: Stop-loss watchdog (multi-instrument)

**Files:** Modify `src/forex_scalper/live.py`; Test `tests/test_live_smoke.py`.

- [ ] **Step 1: Failing test:** an open paper trade with a missing/NaN stop is force-closed by the watchdog and a notification is sent.
- [ ] **Step 2: Implement** the 60s watchdog over `broker.open_trades()` (port from `main_live._watchdog_loop`). Stops live at OANDA via `stopLossOnFill`, but the watchdog is the belt-and-suspenders check.
- [ ] **Step 3: Run — PASS. Step 4: Commit.**

---

## Phase 5 — Persistence + startup reconciliation

So a restart never loses the halt flag, daily P&L baseline, or trade ledger, and never desyncs from the broker.

### Task 5.1: SQLite repository

**Files:** Create `src/forex_scalper/persistence.py`, schema; Test `tests/test_persistence.py`.

- [ ] **Step 1: Failing tests** for: record open/close trade, set/clear halt flag (survives reopen), upsert day snapshot (day_start_balance), append event, `get_last_close`. (Adapt `Forexbot/.../persistence/repository.py` + `schema.sql`.)
- [ ] **Step 2: Implement** repo + `apply_schema`. Route `journal.Journal` writes through it too (keep CSV as a secondary export).
- [ ] **Step 3: Run — PASS. Step 4: Commit.**

### Task 5.2: Reconciliation

**Files:** Create `src/forex_scalper/reconcile.py`; Test `tests/test_reconcile.py`.

- [ ] **Step 1: Failing test:** given broker open trades and DB open trades, detect `orphan_in_broker` (broker has it, DB doesn't) and `orphan_in_db`, and rebuild `RiskManager` heat/exposure from the broker's true open positions on startup.
- [ ] **Step 2: Implement** `reconcile_on_startup(broker, repo, risk)` (adapt `Forexbot/.../reconcile.py`), seeding `risk.register_fill(...)` for every live position so caps are correct after a restart.
- [ ] **Step 3: Run — PASS. Step 4: Commit.**

---

## Phase 6 — Session news calendar

`session.SessionFilter.set_events` exists but nothing feeds it. Wire a calendar source so the bot goes flat around red-folder events.

### Task 6.1: Calendar loader

**Files:** Create `src/forex_scalper/calendar_feed.py`; Test `tests/test_session_calendar.py`.

- [ ] **Step 1: Failing test:** a loader parses a simple CSV/JSON of `{instrument|*, iso_datetime}` into the `{instrument: [datetime]}` shape and `SessionFilter.news_blackout` returns True inside the buffer window.
- [ ] **Step 2: Implement** a file-based loader (manual red-folder CSV to start — no scraping). The live loop reloads it daily at rollover and calls `session.set_events(...)`.
- [ ] **Step 3: Run — PASS. Step 4: Commit.** (A live economic-calendar API can replace the file later; out of scope for first live.)

---

## Phase 7 — Telegram control + notifications

### Task 7.1: Reuse Forexbot's Telegram

**Files:** Modify `src/forex_scalper/notifier.py`; integrate into `live.py`.

- [ ] **Step 1:** Replace the scaffold's `TelegramNotifier` with the proven `Forexbot/.../telegram_bot/notifier.py` + `commands.py` (halt/resume/status/positions), adapted to the scalper's repo + risk objects. Reuse the existing bot token/chat id.
- [ ] **Step 2: Test** that `/halt` sets the persisted halt flag (risk manager then vetoes with `HALTED`) and `/status` reports equity + open trades.
- [ ] **Step 3: Run — PASS. Step 4: Commit.**

---

## Phase 8 — REAL-DATA VALIDATION GATE (go/no-go for live)

**This is the most important phase. Do not skip it. The current bot loses money; we will not replace it with an unvalidated strategy.**

### Task 8.1: Pull real OANDA history

**Files:** Create `scripts/fetch_history.py`.

- [ ] **Step 1:** Script using `oandapyV20.endpoints.instruments.InstrumentsCandles` to download ~12 months of 1M candles for each of the 5 pairs to `data/history/<pair>_1M.csv` (columns `time,open,high,low,close`). Respect OANDA's 5000-candle page limit (paginate).
- [ ] **Step 2:** Run it against the practice account; verify row counts and no gaps across London/NY sessions.

### Task 8.2: Harden the backtest for real data + realistic costs

**Files:** Modify `src/forex_scalper/backtest.py`.

- [ ] **Step 1:** Make `BacktestBroker` spread per-instrument and realistic (EUR/USD ~0.6–1.0p, JPY/crosses wider); add commission/slippage knobs. Confirm `load_csv` handles real timestamps + gaps.
- [ ] **Step 2:** Run walk-forward per pair: `python -m forex_scalper.backtest data/history/EUR_USD_1M.csv <cutoff>`. Record IS vs OOS win rate, avg R, profit factor, max DD for every pair.

### Task 8.3: The decision gate

- [ ] **Step 1: Evaluate.** Live cutover is permitted ONLY if, on REAL data, out-of-sample shows **positive avg R AND profit factor > 1.2 AND max DD within the daily/overall risk limits**, holding reasonably close to in-sample (no collapse = no curve-fit). Record the verdict in `docs/runbook.md`.
- [ ] **Step 2: If it fails the gate — STOP.** Do not go live. Options: re-tune within walk-forward discipline, reduce to the one pair that validates, or keep the incumbent. This is a real possible outcome and the correct one if the edge isn't there.

---

## Phase 9 — Deploy, shadow, cutover, retire old bot

Only proceed past 9.1 if Phase 8 passed the gate.

### Task 9.1: Containerize + config

**Files:** `Dockerfile`, `config/demo.yaml`, `config/live.yaml`, `systemd/forex-scalper.service`.

- [ ] Adapt `Forexbot`'s `Dockerfile` + systemd unit (`Restart=always`, `EnvironmentFile=/etc/forex-scalper.env`). `demo.yaml`: practice env, all 5 pairs, `live=false`. `live.yaml`: live env, ONE validated pair, micro units, `live=true`.

### Task 9.2: Demo soak (practice account) — ≥ the rollout's demo window

- [ ] Deploy the demo config to the VM **alongside** the incumbent (different service name, practice account). Log everything. Watch for ≥ the agreed soak window; compare live-demo stats to the Phase 8 backtest for edge decay.

### Task 9.3: Shadow mode

- [ ] Run the new bot logging-only next to the incumbent for the agreed window. Confirm its would-be trades and risk vetoes look sane on live ticks before it controls money.

### Task 9.4: Live micro cutover

- [ ] **Flatten the incumbent's positions** (via its own `/halt` + manual close at OANDA, or `close_position`), **stop** `forex-bot.service`. Start `forex-scalper.service` with `live.yaml`, single pair, micro units. Verify the first fills, SL/TP at OANDA, and Telegram alerts.

### Task 9.5: Retire the old bot (the deletion you asked for)

- [ ] Only after the new bot has run live cleanly for the agreed proving period: `systemctl disable --now forex-bot`, remove its unit, and delete `C:\Users\User\Forexbot` (the `pre-scalper-migration` git tag + `Forexbot-archive-2026-06-14.zip` remain as the safety net). Per memory: push the final new-bot state to GitHub at end of day.

---

## Self-Review notes

- **Spec coverage:** every scaffold `TODO` is mapped — OandaBroker (P2), `bot.run`/stream (P4), `pip_value_per_unit` (P3), `SessionFilter.set_events` (P6), `TelegramNotifier` (P7). Added what "fully live" actually requires that the scaffold omits: persistence + reconciliation (P5), watchdog (P4.4), real-data validation (P8), deploy/cutover (P9).
- **Risk:** `risk_manager.py` stays the single source of risk truth, untouched, pinned by tests in P1.3 before anything else moves.
- **Reversibility:** old bot archived (P0) and deleted only at the very end (P9.5) after live proof.
- **Honest gate:** P8 can legitimately say "don't go live." That's a feature, not a failure.
```
