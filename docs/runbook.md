# Forex Scalper — Runbook

## Migration safety net (2026-06-14)
- Old bot `Forexbot` committed to git and tagged `pre-scalper-migration-2026-06-14`.
- Full archive: `C:\Users\User\Forexbot-archive-2026-06-14.zip`.
- The old bot is NOT deleted. It is retired only after the new bot is proven live (plan Phase 9.5).

## Account facts (detected 2026-06-14, read-only AccountSummary)
- Reused creds: Forexbot/.env (copied to forex-scalper/.env, gitignored).
- `OANDA_ENVIRONMENT=practice`. Account `101-003-35599767-001`.
- **Account currency: SGD** — pip-value math (Phase 3) uses `*_SGD` crosses.
- Balance ~939.41 SGD at detection (down ~6% from ~1000 — incumbent is losing).
- **1 open position** on this practice account at detection (the incumbent's). Plan flattens it at cutover (Phase 9.4), NOT during the build, so we don't go dark while the replacement is built+validated.

## Platform note
- This Windows dev box fails OANDA TLS verification (AV/proxy intercept). Fix: `truststore.inject_into_ssl()` at startup (uses the OS cert store). `truststore` is a project dep; the live loop / broker bootstrap must call it on Windows. The GCP VM (Linux) does not need it but the call is harmless there.

## Open action items
- [ ] SSH to the GCP e2-micro VM and record whether `forex-bot.service` is currently trading (`systemctl status forex-bot`), and whether the live deployment uses a separate (live) account vs this practice one. Note the answer here.

---

## Deployment (demo account)

> **WARNING — ONE BOT PER ACCOUNT**
>
> forex-scalper and the old forex-bot **both target the same OANDA practice account
> (101-003-35599767-001)**. Running both simultaneously means two bots fighting over
> one account: the scalper's reconciler/watchdog will adopt and act on the old bot's
> open trades, leading to unpredictable behaviour.
>
> **Stop the incumbent BEFORE starting forex-scalper:**
> ```bash
> sudo systemctl disable --now forex-bot   # or stop its Docker container
> ```

> **Note on validation:** The strategy has **not** passed the real-data validation
> gate (Phase 8). This demo deployment is for infrastructure verification and
> observation only — the edge is not yet proven.

---

### Option A — Systemd venv deploy (recommended for GCP e2-micro)

```bash
# 1. Copy / clone the repo to the VM
sudo git clone https://github.com/<your-org>/forex-scalper.git /opt/forex-scalper
# — or rsync from local:
# rsync -av --exclude='.venv' --exclude='data/' . user@<vm-ip>:/opt/forex-scalper/

# 2. Create a dedicated service user (skip if already exists)
sudo useradd --no-create-home --shell /bin/false botuser

# 3. Create the Python venv and install the package
sudo python3 -m venv /opt/forex-scalper/.venv
sudo /opt/forex-scalper/.venv/bin/pip install /opt/forex-scalper

# 4. Create the secrets file. SIMPLEST: copy your existing demo .env verbatim
#    (it already has the correct variable names) instead of hand-editing:
#       scp your-dev-box:/c/Users/User/forex-scalper/.env user@vm:/tmp/fx.env
#       sudo mv /tmp/fx.env /etc/forex-scalper.env
sudo cp /opt/forex-scalper/.env.example /etc/forex-scalper.env   # or start from the template
sudo chmod 600 /etc/forex-scalper.env
sudo nano /etc/forex-scalper.env
#   OANDA_TOKEN=<your-practice-token>
#   OANDA_ACCOUNT_ID=101-003-35599767-001
#   OANDA_ENVIRONMENT=practice
#   TELEGRAM_BOT_TOKEN=<reuse your bot token>
#   TELEGRAM_ALLOWED_CHAT_IDS=<your chat id(s), comma-separated>   # NOT "TELEGRAM_CHAT_ID"

# 5. Set ownership so the service user can write the SQLite DB
sudo mkdir -p /opt/forex-scalper/data
sudo chown -R botuser:botuser /opt/forex-scalper/data

# 6. Install and enable the systemd unit
sudo cp /opt/forex-scalper/systemd/forex-scalper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now forex-scalper

# 7. Tail the logs
journalctl -u forex-scalper -f
```

---

### Option B — Docker

```bash
# Build the image (run from the repo root)
docker build -t forex-scalper:latest .

# Ensure the secrets file exists on the host (same format as Option A step 4)
sudo nano /etc/forex-scalper.env   # fill in creds, chmod 600

# Run the container
docker run -d \
  --name forex-scalper \
  --env-file /etc/forex-scalper.env \
  --restart unless-stopped \
  -v /opt/forex-scalper-data:/app/data \
  forex-scalper:latest

# Tail the logs
docker logs -f forex-scalper
```

---

## Bias-aligned baseline (added 2026-06-18)

Inverse mode was removed. The bot now runs Setup A only, strictly bias-aligned,
with a corrected reward:risk and a minimum-volatility filter:

- Stop = `max(1.5 * 1M-ATR, floor)` (floor 10 pips non-JPY / 15 JPY).
- Take-profit = `1.3 * stop` — always paid more than risked.
- No trade when 1M ATR < 2.5 pips (`RiskConfig.min_atr_pips`).
- Setup B (round-number fade) is disabled (`SetupConfig.enable_setup_b=False`).
- Every close is journalled with pnl + exit_reason
  (time_stop / watchdog_no_stop / broker_close).

Knobs live in `RiskConfig` (`stop_atr_mult`, `tp_r_multiple`, `min_atr_pips`) and
`SetupConfig.enable_setup_b`. Deploy: pull `buildout`, `sudo bash
/opt/forex-scalper/scripts/deploy_vm.sh` (now restarts on redeploy). Verify:
`journalctl -u forex-scalper -n 60 --no-pager | grep -iE "live runner started|reconciled"`.
Let it run 2–3 weeks, then analyse `journal.csv` (opens + closes with pnl).

**Note — synthetic backtest produces no trades (expected):** With Setup B disabled
and Setup A inert on the built-in `synth()` data, running `python -m
forex_scalper.backtest` produces zero trades. This is not a bug — the synthetic
candles never satisfy the bias + volatility filters. Real validation comes from
(a) the live demo soak on the VM and (b) walk-forward on real OANDA CSVs fetched
via `scripts/fetch_history.py`.
