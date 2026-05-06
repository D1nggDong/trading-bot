#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="/home/dingg/Tradingbot"
VENV_PYTHON="${BOT_DIR}/venv/bin/python3"
SERVICE_NAME="ai-trading-alert-bot.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"
RUN_USER="${SUDO_USER:-${USER}}"

if [[ ! -f "${BOT_DIR}/trade_bot.py" ]]; then
  echo "trade_bot.py not found in ${BOT_DIR}"
  exit 1
fi

if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo "venv python not found at ${VENV_PYTHON}"
  exit 1
fi

cat <<EOF | sudo tee "${SERVICE_PATH}" >/dev/null
[Unit]
Description=AI Trading Alert Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=/home/dingg/Tradingbot
EnvironmentFile=${BOT_DIR}/.env
ExecStart=/home/dingg/Tradingbot/venv/bin/python3 /home/dingg/Tradingbot/trade_bot.py
Restart=always
RestartSec=30
Nice=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl restart "${SERVICE_NAME}"
sudo systemctl --no-pager --full status "${SERVICE_NAME}"
