#!/home/dingg/Tradingbot/venv/bin/python3
"""Alpaca paper-trading execution layer with hard guardrails.

Default mode is dry-run. It validates a Tradingbot signal, checks account/position
risk, logs the decision, and only places paper orders when PAPER_TRADING_DRY_RUN=false.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except ValueError:
        return default


BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
KEY_ID = os.getenv("ALPACA_API_KEY_ID", "").strip()
SECRET = os.getenv("ALPACA_API_SECRET_KEY", "").strip()
DRY_RUN = _bool_env("PAPER_TRADING_DRY_RUN", True)
ENABLED = _bool_env("PAPER_TRADING_ENABLED", False)
MIN_CONFIDENCE = _int_env("PAPER_MIN_CONFIDENCE", 75)
MAX_OPEN_POSITIONS = _int_env("PAPER_MAX_OPEN_POSITIONS", _int_env("PAPER_MAX_POSITIONS", 3))
MAX_POSITION_PCT = _float_env("PAPER_MAX_POSITION_PCT", 10.0)
MAX_TRADES_PER_DAY = _int_env("PAPER_MAX_TRADES_PER_DAY", 3)
MAX_TRADES_PER_TICKER_PER_DAY = _int_env("PAPER_MAX_TRADES_PER_TICKER_PER_DAY", 1)
ALLOW_SHORTS = _bool_env("PAPER_ALLOW_SHORTS", False)
ALLOW_OPTIONS = _bool_env("PAPER_ALLOW_OPTIONS", False)
TRADE_LOG = ROOT / os.getenv("PAPER_TRADE_LOG", "paper_trades.jsonl")


class PaperTradeError(RuntimeError):
    pass


def now_utc() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _headers() -> dict[str, str]:
    if not KEY_ID or not SECRET:
        raise PaperTradeError("Alpaca paper API credentials are missing")
    return {
        "APCA-API-KEY-ID": KEY_ID,
        "APCA-API-SECRET-KEY": SECRET,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def alpaca_request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(BASE_URL + path, data=data, headers=_headers(), method=method)
    try:
        with urlrequest.urlopen(req, timeout=15) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urlerror.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise PaperTradeError(f"Alpaca HTTP {exc.code}: {body[:500]}") from exc


def account() -> dict[str, Any]:
    return alpaca_request("GET", "/v2/account")


def positions() -> list[dict[str, Any]]:
    result = alpaca_request("GET", "/v2/positions")
    return result if isinstance(result, list) else []


def open_orders() -> list[dict[str, Any]]:
    result = alpaca_request("GET", "/v2/orders?status=open&limit=100")
    return result if isinstance(result, list) else []


def parse_price(value: Any) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    return float(match.group(0)) if match else None


def trade_counts_today(symbol: str) -> tuple[int, int]:
    today = dt.datetime.now(dt.UTC).date().isoformat()
    total = 0
    ticker_total = 0
    if not TRADE_LOG.exists():
        return total, ticker_total
    for line in TRADE_LOG.read_text().splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not str(item.get("timestamp", "")).startswith(today):
            continue
        if item.get("decision") != "submitted":
            continue
        total += 1
        if str(item.get("symbol", "")).upper() == symbol.upper():
            ticker_total += 1
    return total, ticker_total


def log_decision(record: dict[str, Any]) -> None:
    TRADE_LOG.parent.mkdir(parents=True, exist_ok=True)
    record.setdefault("timestamp", now_utc())
    with TRADE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def validate_signal(signal: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    symbol = str(signal.get("ticker", "")).upper().strip()
    action = str(signal.get("action", "")).upper().strip()
    try:
        confidence = int(float(signal.get("confidence", 0)))
    except (TypeError, ValueError):
        confidence = 0
    entry = parse_price(signal.get("entry"))
    stop = parse_price(signal.get("stop_loss"))
    target = parse_price(signal.get("target_price"))

    facts = {"symbol": symbol, "action": action, "confidence": confidence, "entry": entry, "stop": stop, "target": target}
    if not ENABLED:
        return False, "paper trading disabled", facts
    if not symbol or not re.match(r"^[A-Z][A-Z0-9.\-]{0,9}$", symbol):
        return False, "invalid symbol", facts
    if action not in {"BUY", "SELL"}:
        return False, "only BUY/SELL signals are executable", facts
    if action == "SELL" and not ALLOW_SHORTS:
        return False, "short/sell execution disabled; BUY-only mode", facts
    if confidence < MIN_CONFIDENCE:
        return False, f"confidence {confidence} below threshold {MIN_CONFIDENCE}", facts
    if entry is None or stop is None or target is None:
        return False, "entry/stop/target must be numeric", facts
    if action == "BUY" and not (stop < entry < target):
        return False, "BUY requires stop < entry < target", facts
    return True, "validated", facts


def process_signal(signal: dict[str, Any]) -> dict[str, Any]:
    ok, reason, facts = validate_signal(signal)
    symbol = facts.get("symbol") or str(signal.get("ticker", "")).upper()
    if not ok:
        record = {"decision": "skipped", "reason": reason, "symbol": symbol, "signal": signal}
        log_decision(record)
        return record

    acct = account()
    if str(acct.get("trading_blocked", "false")).lower() == "true":
        record = {"decision": "skipped", "reason": "account trading blocked", "symbol": symbol}
        log_decision(record)
        return record

    current_positions = positions()
    current_orders = open_orders()
    held_symbols = {str(p.get("symbol", "")).upper() for p in current_positions}
    pending_symbols = {str(o.get("symbol", "")).upper() for o in current_orders}

    if symbol in held_symbols or symbol in pending_symbols:
        record = {"decision": "skipped", "reason": "symbol already held or pending", "symbol": symbol}
        log_decision(record)
        return record
    if len(current_positions) >= MAX_OPEN_POSITIONS:
        record = {"decision": "skipped", "reason": "max open positions reached", "symbol": symbol}
        log_decision(record)
        return record

    total_trades, ticker_trades = trade_counts_today(symbol)
    if total_trades >= MAX_TRADES_PER_DAY:
        record = {"decision": "skipped", "reason": "max daily trades reached", "symbol": symbol}
        log_decision(record)
        return record
    if ticker_trades >= MAX_TRADES_PER_TICKER_PER_DAY:
        record = {"decision": "skipped", "reason": "max ticker trades reached", "symbol": symbol}
        log_decision(record)
        return record

    equity = float(acct.get("equity") or acct.get("cash") or 0)
    max_notional = equity * (MAX_POSITION_PCT / 100.0)
    entry = float(facts["entry"])
    qty = max(1, math.floor(max_notional / entry))
    if qty * entry > max_notional * 1.05:
        qty = max(0, math.floor(max_notional / entry))
    if qty <= 0:
        record = {"decision": "skipped", "reason": "position size too small", "symbol": symbol, "max_notional": max_notional}
        log_decision(record)
        return record

    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": "buy" if facts["action"] == "BUY" else "sell",
        "type": "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "take_profit": {"limit_price": str(round(float(facts["target"]), 2))},
        "stop_loss": {"stop_price": str(round(float(facts["stop"]), 2))},
    }

    if DRY_RUN:
        record = {"decision": "dry_run", "reason": "validated but dry-run enabled", "symbol": symbol, "order": payload}
        log_decision(record)
        return record

    result = alpaca_request("POST", "/v2/orders", payload)
    record = {"decision": "submitted", "symbol": symbol, "order": payload, "alpaca_order": result}
    log_decision(record)
    return record


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Validate or execute one paper-trading signal")
    parser.add_argument("--account", action="store_true", help="Check Alpaca account connectivity")
    parser.add_argument("--signal-json", help="Signal JSON object to process")
    args = parser.parse_args()
    if args.account:
        acct = account()
        print(json.dumps({"status": acct.get("status"), "paper": BASE_URL, "trading_blocked": acct.get("trading_blocked"), "equity": acct.get("equity")}, indent=2))
    elif args.signal_json:
        print(json.dumps(process_signal(json.loads(args.signal_json)), indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
