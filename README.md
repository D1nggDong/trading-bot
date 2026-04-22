# Trading Bot

An always-on Python bot that discovers active stock tickers, asks an AI model for structured trade analysis, and sends only actionable alerts to Telegram.

## What it does

- Discovers symbols from Yahoo Finance (day gainers, most active, trending/news-related).
- Pulls recent price action + headlines with `yfinance`.
- Prompts the model to return a strict 10-line trading alert format (no extra text).
- Filters low-value noise and only sends high-value alerts.
- Prevents duplicate alerts using a hash cache in `state.json`.

## Alert format (enforced)

The model is instructed to return exactly this structure:

```text
📊 TICKER: [SYMBOL]
💰 CURRENT PRICE: $[price]
🎯 SIGNAL: [BUY / SELL / HOLD]
📊 CONFIDENCE: [HIGH / MEDIUM / LOW]
📝 REASON: [2-3 sentences explaining WHY based on news and price action]
⏰ WHEN: [Specific timing, e.g. "Enter tomorrow at market open if price holds above $X"]
🎯 ENTRY PRICE: $[specific price or range to enter]
🛑 STOP LOSS: $[specific stop loss price]
✅ TARGET PRICE: $[specific price target]
📈 OPTIONS PLAY: [If applicable - specific strategy, strike, expiry]
```

## High-value filtering logic

A Telegram alert is sent if **any** of these are true:

- `SIGNAL` is `BUY` or `SELL`
- `CONFIDENCE` is `HIGH` or `MEDIUM`
- `REASON` mentions a catalyst: earnings, news, breakout, unusual volume, or options activity

If a ticker is `HOLD` + `LOW` confidence + no catalyst, it is skipped and logged:

```text
Skipping [TICKER] - no actionable signal
```

## Requirements

- Python 3.10+
- Telegram bot token and chat ID
- GitHub token with model access for inference API

## Setup

1. Clone the repo and enter it:
   ```bash
   git clone https://github.com/D1nggDong/trading-bot.git
   cd trading-bot
   ```
2. Create and activate a virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Create your environment file:
   ```bash
   cp .env.example .env
   ```
5. Edit `.env` with your credentials and settings.

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | - | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Yes | - | Destination chat/channel ID |
| `COPILOT_API_KEY` | Yes | - | Startup validation key in current code path |
| `COPILOT_MODEL` | No | `claude-3.5-sonnet` | Preserved config model field |
| `GITHUB_TOKEN` | Yes | - | Token used by model inference client |
| `MODEL_NAME` | No | `gpt-4o` | Model name used for chat completion |
| `CHECK_INTERVAL_MINUTES` | No | `60` | Run interval (minimum 5) |
| `NEWS_LOOKBACK_DAYS` | No | `7` | News freshness window |
| `MAX_NEWS_ITEMS` | No | `5` | Max headlines per ticker |
| `DAY_GAINERS_LIMIT` | No | `10` | Symbols from day gainers screener |
| `MOST_ACTIVE_LIMIT` | No | `10` | Symbols from most active screener |
| `TRENDING_NEWS_LIMIT` | No | `25` | News items scanned for related tickers |
| `DISCOVERY_REGION` | No | `US` | Yahoo trending region |
| `REQUEST_TIMEOUT_SECONDS` | No | `12` | HTTP request timeout |
| `MAX_PARALLEL_TICKERS` | No | `1` | Reserved for processing concurrency tuning |
| `LOG_LEVEL` | No | `INFO` | Logging verbosity |
| `STATE_FILE` | No | `state.json` | Persistent state + dedupe cache |
| `TIMEZONE` | No | `UTC` | Runtime timezone setting |

## Run locally

```bash
python trade_bot.py
```

The bot runs continuously and sleeps between cycles based on `CHECK_INTERVAL_MINUTES`.

## Run as a systemd service (Linux)

Use the included helper script:

```bash
chmod +x setup_service.sh
./setup_service.sh
```

It creates and starts `ai-trading-alert-bot.service`, using `.env` from the project directory.

## State and deduplication

- `state.json` stores:
  - `sent_hashes` to prevent duplicate Telegram messages for the same ticker
  - `last_run_utc`
  - `last_discovered_tickers` fallback set

## Notes

- This project is for informational/automation use and is **not financial advice**.
- Always validate trade ideas independently and apply your own risk management.
