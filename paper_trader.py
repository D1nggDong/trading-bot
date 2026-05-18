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


TRADING_MODE = os.getenv("TRADING_MODE", "paper").strip().lower()
if TRADING_MODE not in {"paper", "live"}:
    TRADING_MODE = "paper"
IS_LIVE = TRADING_MODE == "live"
_PREFIX = "LIVE" if IS_LIVE else "PAPER"

BASE_URL = os.getenv(
    "LIVE_ALPACA_BASE_URL" if IS_LIVE else "ALPACA_BASE_URL",
    "https://api.alpaca.markets" if IS_LIVE else "https://paper-api.alpaca.markets",
).rstrip("/")
KEY_ID = os.getenv("LIVE_ALPACA_API_KEY_ID" if IS_LIVE else "ALPACA_API_KEY_ID", os.getenv("ALPACA_API_KEY", "")).strip()
SECRET = os.getenv("LIVE_ALPACA_API_SECRET_KEY" if IS_LIVE else "ALPACA_API_SECRET_KEY", os.getenv("ALPACA_SECRET_KEY", "")).strip()
DRY_RUN = _bool_env(f"{_PREFIX}_TRADING_DRY_RUN", True)
ENABLED = _bool_env(f"{_PREFIX}_TRADING_ENABLED", False)
MIN_CONFIDENCE = _int_env(f"{_PREFIX}_MIN_CONFIDENCE", _int_env("PAPER_MIN_CONFIDENCE", 75))
MAX_OPEN_POSITIONS = _int_env(f"{_PREFIX}_MAX_OPEN_POSITIONS", _int_env(f"{_PREFIX}_MAX_POSITIONS", 3))
MAX_POSITION_PCT = _float_env(f"{_PREFIX}_MAX_POSITION_PCT", 10.0 if not IS_LIVE else 100.0)
MAX_POSITION_DOLLARS = _float_env(f"{_PREFIX}_MAX_POSITION_DOLLARS", 0.0)
CASH_RESERVE_DOLLARS = _float_env(f"{_PREFIX}_CASH_RESERVE_DOLLARS", 0.0)
MAX_TRADES_PER_DAY = _int_env(f"{_PREFIX}_MAX_TRADES_PER_DAY", 3 if not IS_LIVE else 5)
MAX_TRADES_PER_TICKER_PER_DAY = _int_env(f"{_PREFIX}_MAX_TRADES_PER_TICKER_PER_DAY", 1)
ALLOW_SHORTS = _bool_env(f"{_PREFIX}_ALLOW_SHORTS", False)
ALLOW_OPTIONS = _bool_env(f"{_PREFIX}_ALLOW_OPTIONS", False)
OPTION_MAX_CONTRACTS = _int_env(f"{_PREFIX}_OPTION_MAX_CONTRACTS", 10 if not IS_LIVE else 999)
OPTION_MAX_PREMIUM_PCT = _float_env(f"{_PREFIX}_OPTION_MAX_PREMIUM_PCT", 20.0 if not IS_LIVE else 100.0)
OPTION_MAX_PREMIUM_DOLLARS = _float_env(f"{_PREFIX}_OPTION_MAX_PREMIUM_DOLLARS", 0.0)
OPTION_MIN_VOLUME = _int_env(f"{_PREFIX}_OPTION_MIN_VOLUME", 0)
OPTION_MIN_OPEN_INTEREST = _int_env(f"{_PREFIX}_OPTION_MIN_OPEN_INTEREST", 0)
OPTION_MAX_SPREAD_PCT = _float_env(f"{_PREFIX}_OPTION_MAX_SPREAD_PCT", 100.0)
TRADE_LOG = ROOT / os.getenv(f"{_PREFIX}_TRADE_LOG", os.getenv("PAPER_TRADE_LOG", "paper_trades.jsonl"))

class PaperTradeError(RuntimeError):
    pass


def now_utc() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _headers() -> dict[str, str]:
    if not KEY_ID or not SECRET:
        raise PaperTradeError(f"Alpaca {TRADING_MODE} API credentials are missing")
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


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        result = float(value)
        if result != result:
            return None
        return result
    except (TypeError, ValueError):
        return None


def option_contract_for_signal(signal: dict[str, Any], action: str) -> tuple[dict[str, Any] | None, str]:
    if not ALLOW_OPTIONS:
        return None, "options disabled"
    context = signal.get("option_context")
    if not isinstance(context, dict) or not context.get("available"):
        return None, "no option context available"
    play = str(signal.get("options_play", "")).lower()
    if "no options" in play or "equity/watchlist only" in play:
        return None, "AI did not recommend an options play"
    key = "nearest_call" if action == "BUY" else "nearest_put"
    contract = context.get(key)
    if not isinstance(contract, dict):
        return None, f"{key} unavailable"
    contract_symbol = str(contract.get("contractSymbol") or "").strip()
    if not contract_symbol:
        return None, "option contract symbol unavailable"
    bid = _num(contract.get("bid")) or 0.0
    ask = _num(contract.get("ask")) or 0.0
    last = _num(contract.get("lastPrice")) or 0.0
    premium = ask or last or bid
    if premium <= 0:
        return None, "option premium unavailable"
    volume = _num(contract.get("volume")) or 0.0
    open_interest = _num(contract.get("openInterest")) or 0.0
    if volume < OPTION_MIN_VOLUME:
        return None, f"option volume {volume:g} below minimum {OPTION_MIN_VOLUME}"
    if open_interest < OPTION_MIN_OPEN_INTEREST:
        return None, f"option open interest {open_interest:g} below minimum {OPTION_MIN_OPEN_INTEREST}"
    if bid > 0 and ask > 0:
        mid = (bid + ask) / 2.0
        spread_pct = ((ask - bid) / mid) * 100.0 if mid else 999.0
        if spread_pct > OPTION_MAX_SPREAD_PCT:
            return None, f"option spread {spread_pct:.1f}% above max {OPTION_MAX_SPREAD_PCT:.1f}%"
    return {**contract, "contractSymbol": contract_symbol, "premium": premium, "option_side": "call" if action == "BUY" else "put"}, "selected"


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
        return False, f"{TRADING_MODE} trading disabled", facts
    if not symbol or not re.match(r"^[A-Z][A-Z0-9.\-]{0,9}$", symbol):
        return False, "invalid symbol", facts
    if action not in {"BUY", "SELL"}:
        return False, "only BUY/SELL signals are executable", facts
    if IS_LIVE and action == "SELL" and not ALLOW_OPTIONS:
        return False, "live short/sell execution disabled; cash-only BUY mode", facts
    if action == "SELL" and not ALLOW_SHORTS and not ALLOW_OPTIONS:
        return False, "short/sell execution disabled; BUY-only mode", facts
    if confidence < MIN_CONFIDENCE:
        return False, f"confidence {confidence} below threshold {MIN_CONFIDENCE}", facts
    if entry is None or stop is None or target is None:
        return False, "entry/stop/target must be numeric", facts
    if action == "BUY" and not (stop < entry < target):
        return False, "BUY requires stop < entry < target", facts
    return True, "validated", facts


def ensure_option_exit_orders() -> list[dict[str, Any]]:
    """Create persistent exits for open long option positions.

    Options are buy-to-open only. For live cash accounts, default to a GTC stop
    sell order so the contract has downside protection without opening debt or a
    naked position. Only existing long option positions are closed here.
    """
    if not ENABLED or DRY_RUN:
        return []
    tp_mult = _float_env(f"{_PREFIX}_OPTION_TAKE_PROFIT_MULT", 2.0)
    stop_mult = _float_env(f"{_PREFIX}_OPTION_STOP_MULT", 0.5)
    exit_mode = os.getenv(f"{_PREFIX}_OPTION_EXIT_MODE", "stop" if IS_LIVE else "take_profit").strip().lower()
    current_positions = positions()
    current_orders = alpaca_request("GET", "/v2/orders?status=open&limit=100&nested=true")
    open_closing = {
        str(o.get("symbol", "")).upper()
        for o in current_orders
        if str(o.get("side", "")).lower() == "sell"
    }
    created: list[dict[str, Any]] = []
    for pos in current_positions:
        if pos.get("asset_class") != "us_option" or pos.get("side") != "long":
            continue
        symbol = str(pos.get("symbol", "")).upper()
        if not symbol or symbol in open_closing:
            continue
        qty = str(pos.get("qty") or "0")
        avg = _num(pos.get("avg_entry_price")) or _num(pos.get("current_price")) or 0.0
        if avg <= 0:
            continue
        limit_price = max(0.01, round(avg * tp_mult, 2))
        stop_price = max(0.01, round(avg * stop_mult, 2))
        payload = {
            "symbol": symbol,
            "qty": qty,
            "side": "sell",
            "time_in_force": os.getenv(f"{_PREFIX}_OPTION_EXIT_TIME_IN_FORCE", "gtc"),
        }
        if exit_mode == "stop":
            payload.update({"type": "stop", "stop_price": str(stop_price)})
        else:
            payload.update({"type": "limit", "limit_price": str(limit_price)})
        try:
            result = alpaca_request("POST", "/v2/orders", payload)
            record = {"decision": "option_exit_submitted", "mode": TRADING_MODE, "exit_mode": exit_mode, "symbol": symbol, "qty": qty, "take_profit": limit_price, "stop_price": stop_price, "order": result}
        except Exception as exc:
            record = {"decision": "option_exit_failed", "mode": TRADING_MODE, "exit_mode": exit_mode, "symbol": symbol, "qty": qty, "take_profit": limit_price, "stop_price": stop_price, "reason": str(exc)}
        log_decision(record)
        created.append(record)
    return created


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

    option_contract, option_reason = option_contract_for_signal(signal, str(facts["action"]))
    if facts["action"] == "SELL" and not ALLOW_SHORTS and option_contract is None:
        record = {"decision": "skipped", "reason": f"SELL requires a buy-to-open put option when shorts are disabled: {option_reason}", "symbol": symbol}
        log_decision(record)
        return record
    execution_mode = "option" if option_contract is not None else "equity"
    execution_symbol = option_contract["contractSymbol"] if option_contract else symbol

    if execution_symbol in held_symbols or execution_symbol in pending_symbols:
        record = {"decision": "skipped", "reason": "symbol already held or pending", "symbol": symbol, "execution_symbol": execution_symbol}
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
    cash = float(acct.get("cash") or acct.get("buying_power") or 0)

    if execution_mode == "option":
        if IS_LIVE and not ALLOW_OPTIONS:
            record = {"decision": "skipped", "reason": "live options disabled", "symbol": symbol}
            log_decision(record)
            return record
        max_premium = equity * (OPTION_MAX_PREMIUM_PCT / 100.0)
        if OPTION_MAX_PREMIUM_DOLLARS > 0:
            max_premium = min(max_premium, OPTION_MAX_PREMIUM_DOLLARS)
        if IS_LIVE:
            max_premium = min(max_premium, max(0.0, cash - CASH_RESERVE_DOLLARS))
        premium = float(option_contract["premium"])
        qty = min(OPTION_MAX_CONTRACTS, math.floor(max_premium / (premium * 100.0)))
        if qty <= 0:
            record = {"decision": "skipped", "reason": "option premium budget too small", "symbol": symbol, "max_premium": max_premium, "premium": premium}
            log_decision(record)
            return record
        payload = {
            "symbol": execution_symbol,
            "qty": str(qty),
            "side": "buy",
            "type": "limit",
            "limit_price": str(round(premium, 2)),
            "time_in_force": os.getenv(f"{_PREFIX}_OPTION_TIME_IN_FORCE", "gtc"),
        }
        order_meta = {"mode": "option", "underlying": symbol, "option_side": option_contract.get("option_side"), "premium": premium, "max_premium": max_premium, "option_reason": option_reason}
    else:
        max_notional = equity * (MAX_POSITION_PCT / 100.0)
        if MAX_POSITION_DOLLARS > 0:
            max_notional = min(max_notional, MAX_POSITION_DOLLARS)
        if IS_LIVE:
            max_notional = min(max_notional, max(0.0, cash - CASH_RESERVE_DOLLARS))
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
            "time_in_force": os.getenv(f"{_PREFIX}_EQUITY_TIME_IN_FORCE", "gtc"),
            "order_class": "bracket",
            "take_profit": {"limit_price": str(round(float(facts["target"]), 2))},
            "stop_loss": {"stop_price": str(round(float(facts["stop"]), 2))},
        }
        order_meta = {"mode": "equity"}

    if DRY_RUN:
        record = {"decision": "dry_run", "reason": "validated but dry-run enabled", "symbol": symbol, "execution_symbol": execution_symbol, "order": payload, "meta": order_meta}
        log_decision(record)
        return record

    result = alpaca_request("POST", "/v2/orders", payload)
    record = {"decision": "submitted", "mode": TRADING_MODE, "symbol": symbol, "execution_symbol": execution_symbol, "order": payload, "meta": order_meta, "alpaca_order": result}
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
        print(json.dumps({"status": acct.get("status"), "mode": TRADING_MODE, "base_url": BASE_URL, "trading_blocked": acct.get("trading_blocked"), "equity": acct.get("equity"), "cash": acct.get("cash"), "buying_power": acct.get("buying_power")}, indent=2))
    elif args.signal_json:
        print(json.dumps(process_signal(json.loads(args.signal_json)), indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
