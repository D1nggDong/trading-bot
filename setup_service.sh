#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="ai-trading-alert-bot.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
RUN_USER="${SUDO_USER:-${USER}}"

if [[ ! -f "${SCRIPT_DIR}/trade_bot.py" ]]; then
  echo "trade_bot.py not found in ${SCRIPT_DIR}"
  exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  else
    echo "python3 was not found"
    exit 1
  fi
fi

cat <<EOF | sudo tee "${SERVICE_PATH}" >/dev/null
[Unit]
Description=AI Trading Alert Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${SCRIPT_DIR}
EnvironmentFile=${SCRIPT_DIR}/.env
ExecStart=${PYTHON_BIN} ${SCRIPT_DIR}/trade_bot.py
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
