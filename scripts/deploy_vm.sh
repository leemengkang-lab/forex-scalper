#!/usr/bin/env bash
#
# deploy_vm.sh — one-shot demo deployment on the GCP VM.
#
# PREREQUISITES (do these once before running):
#   1. The repo is present at /opt/forex-scalper  (git clone or rsync).
#   2. /etc/forex-scalper.env exists (chmod 600) with your PRACTICE creds:
#        OANDA_TOKEN=...           OANDA_ACCOUNT_ID=101-003-35599767-001
#        OANDA_ENVIRONMENT=practice
#        TELEGRAM_BOT_TOKEN=...    TELEGRAM_ALLOWED_CHAT_IDS=<your chat id>
#      (Simplest: scp your existing forex-scalper/.env up to /etc/forex-scalper.env.)
#
# Then:  sudo bash /opt/forex-scalper/scripts/deploy_vm.sh
#
# This runs the new bot on the PRACTICE account (zero real-money risk). The
# strategy did NOT pass the real-data validation gate — this is for observation.
set -euo pipefail
APP=/opt/forex-scalper

echo "[1/5] Stopping the incumbent (one bot per account/token)..."
sudo systemctl disable --now forex-bot 2>/dev/null && echo "  forex-bot stopped." \
  || echo "  forex-bot not found / already stopped (continuing)."

echo "[2/5] Building venv + installing the package..."
sudo python3 -m venv "$APP/.venv"
sudo "$APP/.venv/bin/pip" install --upgrade pip
sudo "$APP/.venv/bin/pip" install "$APP"

echo "[3/5] Ensuring data dir (SQLite ledger lives here)..."
sudo mkdir -p "$APP/data"

echo "[4/5] Installing + starting the systemd service..."
sudo cp "$APP/systemd/forex-scalper.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now forex-scalper

echo "[5/5] Status:"
sudo systemctl --no-pager status forex-scalper | head -n 15
echo
echo "Done. Watch it live with:  journalctl -u forex-scalper -f"
echo "Monitor from Telegram:     /status   /positions   (alerts fire on every fill)"
echo "Note: entries only occur during the 12:00-16:00 UTC session window."
