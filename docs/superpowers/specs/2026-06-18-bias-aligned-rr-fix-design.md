# Bias-Aligned, Correct-R:R Baseline — Design

**Date:** 2026-06-18
**Status:** Approved (design); pending implementation plan.
**Supersedes:** the EUR/USD inverse mode
(`2026-06-18-eurusd-inverse-mode-design.md`) — that feature is removed in full.

## Goal

Establish the bot's first *correct* configuration so a clean multi-week demo run
can actually measure the strategy's edge. Five changes:

1. **Remove all inversion logic.** Every trade is bias-aligned: bias `LONG_ONLY`
   → longs only, `SHORT_ONLY` → shorts only, `FLAT` → no trade. No inverted mode,
   no exceptions.
2. **Fix reward:risk structurally** so the target can never be smaller than the
   stop.
3. **Add a minimum-volatility filter** — don't trade when the market is too dead
   to scalp.
4. **Disable Setup B** (round-number fade) for now; run Setup A alone.
5. **Journal every trade close** with pnl and an accurate exit_reason.

Motivation: the prior month ran a broken config (R:R that could pay less than it
risked; ~half the entries fired at 1M ATR < 2 pips; Setup B repeatedly fading the
same round number at `bias=FLAT`; closes never written to `journal.csv`). One
month of a broken config tells us nothing; two weeks of a correct one tells us
almost everything.

## 1. Remove inversion

- Delete `src/forex_scalper/inversion.py`, `tests/test_inversion.py`,
  `tests/test_inverse_mode_bot.py`.
- Remove `BotConfig.invert_instruments`.
- Remove the `maybe_invert` import and call in `bot.py`.
- Remove `INVERT_INSTRUMENTS` parsing in `live.py` and the line in `.env.example`.
- Remove the inverse-mode section from `docs/runbook.md`.
- **Defensive bias guard:** in `bot._try_enter` (or as a risk veto), reject any
  signal whose direction disagrees with a non-FLAT bias. With inversion gone and
  Setup B disabled this should never trigger, but it guarantees the invariant.

## 2. Reward:risk — single source of truth in `risk_manager`

The take-profit currently lives in `bot._try_enter` (`entry + dir*tp_atr_mult*ATR`),
decoupled from the stop that `risk_manager` computes. Move both into
`risk_manager.evaluate` so they can never diverge.

- `TradeSignal` gains `atr_pips: float` (1M ATR in pips; the caller already
  computes it).
- `RiskConfig` gains `stop_atr_mult: float = 1.5`, `tp_r_multiple: float = 1.3`.
- `evaluate` computes:
  ```
  stop_pips = max(cfg.stop_atr_mult * atr_pips, floor)   # floor 10 non-JPY / 15 JPY
  tp_pips   = cfg.tp_r_multiple * stop_pips              # TP >= stop, always
  stop_price = entry - direction * stop_pips * pip
  take_profit = entry + direction * tp_pips * pip
  ```
- `RiskDecision` gains `take_profit: float`.
- `bot._try_enter` passes `atr_pips`, drops its own TP math, and uses
  `decision.take_profit` in `broker.place_market_order`.
- This **replaces the structural swing-based stop for sizing** with a 1.5×ATR
  stop. Setups keep computing their structural stop, but only to confirm a valid
  signal exists (rejection with stop on the correct side); the number handed to
  the broker/risk is the ATR-based one. `tp_atr_mult` is removed from `BotConfig`.

## 3. Minimum-volatility filter

- `RiskConfig.min_atr_pips: float = 2.5`.
- `evaluate` vetoes with a new `Reject.LOW_VOLATILITY` when `atr_pips` is missing
  or `< min_atr_pips`. Logged as a `reject` journal row so the count is visible.

## 4. Disable Setup B

- `SetupConfig.enable_setup_b: bool = False`.
- `setups.detect()` skips the `RANGE` (level-rejection) branch when the flag is
  false. Nothing is deleted; flip to re-enable after it's independently proven.

## 5. Journal every close (accurate per-site reason)

`close_trade` returns realized pnl, so each close site journals directly:

- **Time stop** (`trade_manager._manage_one`): capture `pnl = broker.close_trade(id)`,
  write `JournalRow(event="close", instrument, pnl, exit=price, exit_reason="time_stop")`,
  and record the close in the repo so the reconciler won't re-handle it.
- **Watchdog no-stop close** (`live._watchdog`): same, `exit_reason="watchdog_no_stop"`.
- **Broker-side SL/TP** (`reconcile.PositionReconciler._close_one`): already
  computes pnl and `record_close` to the DB; additionally write a close
  `JournalRow` with `exit_reason="broker_close"` (startup variant:
  `"broker_close_startup"`).

Wiring: inject the bot's `Journal` into `TradeManager` and `PositionReconciler`;
the watchdog uses `bot.journal` (available in `live.py`). Each trade is journalled
exactly once (the site that closes it also marks it closed).

## Data flow (per candle, unchanged order)

`reconciler.sync` (journals broker closes) → `bot.on_candle_close` →
`trade_manager.manage` (journals time-stop closes) → session/bias/regime →
`detect` (Setup A only) → bias guard → `risk.evaluate` (min-vol veto; 1.5×ATR
stop; 1.3× TP) → `broker.place_market_order(stop, take_profit)` → journal open.

## Error handling

- `atr_pips` missing/None → treated as below the min-vol floor → `LOW_VOLATILITY`
  reject (no trade). No new crash paths.
- Close journaling failures must never abort trading: wrap each close-journal
  write so a logging error can't prevent the actual close/risk update.

## Testing (TDD)

- **risk_manager:** stop = `max(1.5*atr, floor)`; tp = `1.3*stop` (both sides);
  `tp_pips >= stop_pips` always; `LOW_VOLATILITY` reject when `atr_pips < 2.5`;
  `take_profit` present and on the correct side.
- **setups:** Setup B suppressed when `enable_setup_b=False`; Setup A unaffected.
- **bias guard:** a short signal under `LONG_ONLY` bias is rejected.
- **close journaling:** time-stop close writes one close row with pnl +
  `time_stop`; reconciler broker-close writes one close row with pnl +
  `broker_close`; no double-journaling.
- **removal:** inversion module/flag/env var gone; suite green without them.
- **integration:** an in-sample backtest run completes with the new R:R and
  produces close rows in the journal.

## Out of scope

- Any live-money cutover (`OANDA_ENVIRONMENT` stays practice).
- The London-open liquidity-sweep strategy (the wildcard candidate) — only
  considered *after* this baseline produces evidence.
- Economic-calendar automation, Setup B improvements.

## Rollout

Deploy to the demo VM, restart, confirm bias-aligned Setup-A trades and close
rows accumulating. Let it run 2–3 weeks, then run the analyzer:
profitable → keep/tune; flat → tune exits with evidence; genuinely bleeding with
correct R:R and no inversion → only then evaluate a different strategy.
