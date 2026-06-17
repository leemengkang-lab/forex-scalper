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

## Inverse (reversal) mode — EUR/USD (added 2026-06-18)

Inverse mode takes the **opposite** side of every detected setup (buy↔sell) for
the listed instruments. It is controlled by the `INVERT_INSTRUMENTS` env var
(comma-separated, e.g. `EUR_USD`). Empty/unset = normal trading. It is independent
of `OANDA_ENVIRONMENT`; it does **not** turn on real-money trading.

Design/plan: `docs/superpowers/specs/2026-06-18-eurusd-inverse-mode-design.md`,
`docs/superpowers/plans/2026-06-18-eurusd-inverse-mode.md`.

> **Caveat:** flipping a strategy does not guarantee profit — the spread is paid
> on the inverted side too. Treat the demo soak as the test of whether the flip
> has edge before any live cutover.

### Deploy / update inverse mode on the VM (DEMO / practice, $0 risk)

```bash
# 1. Pull the latest code (the VM deploys from /opt/forex-scalper)
cd /opt/forex-scalper
sudo git fetch origin && sudo git checkout buildout && sudo git pull

# 2. Enable inverse mode for EUR/USD. The deploy script does NOT overwrite an
#    existing /etc/forex-scalper.env, so add the line yourself:
echo 'INVERT_INSTRUMENTS=EUR_USD' | sudo tee -a /etc/forex-scalper.env

# 3. Re-run the deploy (reinstalls the package + restarts the service)
sudo bash /opt/forex-scalper/scripts/deploy_vm.sh

# 4. Confirm it is active
journalctl -u forex-scalper -n 50 --no-pager | grep -i "INVERSE MODE"
#   -> "INVERSE MODE active for: EUR_USD"
```

### Turn it off

Remove (or blank) the `INVERT_INSTRUMENTS` line in `/etc/forex-scalper.env` and
restart: `sudo systemctl restart forex-scalper`. No code change or redeploy needed.

> Trades only fire 12:00–16:00 UTC (London/NY overlap); a quiet log outside that
> window is normal. Monitor via Telegram `/status` and `/positions`.
