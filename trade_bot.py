#!/usr/bin/env python3
"""24/7 AI trading alert bot.

This bot discovers high-momentum tickers from Yahoo endpoints, pulls market data and
recent headlines with yfinance, asks a Copilot-backed LLM to turn that context into
a structured trade call, and sends the result to a Telegram chat.

The implementation is intentionally lightweight for Raspberry Pi deployment:
- synchronous market calls are offloaded with asyncio.to_thread
- the bot sleeps between runs to minimize CPU and network usage
- secrets are loaded from .env and never hardcoded
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

import openai
from dotenv import load_dotenv
from telegram import Bot
import yfinance as yf


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

FALLBACK_TICKERS: list[str] = [
    "AAPL",
    "MSFT",
    "NVDA",
    "TSLA",
    "AMZN",
    "META",
    "GOOGL",
    "AMD",
    "SPY",
    "QQQ",
    "PLTR",
    "SOFI",
    "GME",
    "AMC",
    "RIVN",
    "COIN",
    "MSTR",
    "SMCI",
]


@dataclasses.dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    telegram_chat_id: str
    copilot_model: str
    check_interval_minutes: int
    day_gainers_limit: int
    most_active_limit: int
    trending_news_limit: int
    discovery_region: str
    request_timeout_seconds: float
    max_parallel_tickers: int
    news_lookback_days: int
    max_news_items: int
    log_level: str
    state_file: Path
    timezone: str


@dataclasses.dataclass
class MarketSnapshot:
    ticker: str
    last_price: float | None
    previous_close: float | None
    price_change_pct: float | None
    trend: str
    news: list[str]
    option_context: dict[str, Any]


class TradeBotError(RuntimeError):
    pass


def load_config() -> Config:
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    github_token = os.getenv("GITHUB_TOKEN", "").strip()
    copilot_model = os.getenv("COPILOT_MODEL", "claude-3.5-sonnet").strip()

    if not telegram_bot_token:
        raise TradeBotError("TELEGRAM_BOT_TOKEN is missing")
    if not telegram_chat_id:
        raise TradeBotError("TELEGRAM_CHAT_ID is missing")
    if not github_token:
        raise TradeBotError("GITHUB_TOKEN is missing")

    check_interval_minutes = max(5, int(os.getenv("CHECK_INTERVAL_MINUTES", "60")))
    day_gainers_limit = max(1, int(os.getenv("DAY_GAINERS_LIMIT", "10")))
    most_active_limit = max(1, int(os.getenv("MOST_ACTIVE_LIMIT", "10")))
    trending_news_limit = max(1, int(os.getenv("TRENDING_NEWS_LIMIT", "25")))
    discovery_region = os.getenv("DISCOVERY_REGION", "US").strip().upper() or "US"
    request_timeout_seconds = max(2.0, float(os.getenv("REQUEST_TIMEOUT_SECONDS", "12")))
    max_parallel_tickers = max(1, int(os.getenv("MAX_PARALLEL_TICKERS", "1")))
    news_lookback_days = max(1, int(os.getenv("NEWS_LOOKBACK_DAYS", "7")))
    max_news_items = max(1, int(os.getenv("MAX_NEWS_ITEMS", "5")))
    log_level = os.getenv("LOG_LEVEL", "INFO").upper().strip()
    timezone = os.getenv("TIMEZONE", "UTC").strip()
    state_file = Path(os.getenv("STATE_FILE", str(ROOT / "state.json"))).expanduser()

    return Config(
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        copilot_model=copilot_model,
        check_interval_minutes=check_interval_minutes,
        day_gainers_limit=day_gainers_limit,
        most_active_limit=most_active_limit,
        trending_news_limit=trending_news_limit,
        discovery_region=discovery_region,
        request_timeout_seconds=request_timeout_seconds,
        max_parallel_tickers=max_parallel_tickers,
        news_lookback_days=news_lookback_days,
        max_news_items=max_news_items,
        log_level=log_level,
        state_file=state_file,
        timezone=timezone,
    )


def setup_logging(level_name: str) -> None:
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"sent_hashes": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"sent_hashes": {}}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _fetch_json(url: str, params: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    query = urlparse.urlencode({k: str(v) for k, v in params.items() if v is not None})
    target = f"{url}?{query}" if query else url
    req = urlrequest.Request(
        target,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json",
        },
    )
    with urlrequest.urlopen(req, timeout=timeout_seconds) as response:
        body = response.read().decode("utf-8")
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise TradeBotError(f"Expected JSON object from endpoint: {url}")
    return parsed


def _normalize_symbol(raw_symbol: Any) -> str | None:
    if raw_symbol is None:
        return None
    symbol = str(raw_symbol).strip().upper()
    if not symbol:
        return None
    if not all(char.isalnum() or char in ".-^" for char in symbol):
        return None
    return symbol


def _extract_symbols(quotes: Any, limit: int | None = None) -> list[str]:
    symbols: list[str] = []
    if not isinstance(quotes, list):
        return symbols
    for item in quotes:
        if not isinstance(item, dict):
            continue
        symbol = _normalize_symbol(item.get("symbol"))
        if not symbol:
            continue
        symbols.append(symbol)
        if limit is not None and len(symbols) >= limit:
            break
    return symbols


def _extract_screener_symbols(payload: dict[str, Any], limit: int) -> list[str]:
    finance = payload.get("finance")
    if not isinstance(finance, dict):
        return []
    result = finance.get("result")
    if not isinstance(result, list) or not result:
        return []
    first = result[0]
    if not isinstance(first, dict):
        return []
    quotes = first.get("quotes")
    return _extract_symbols(quotes, limit=limit)


async def discover_tickers(config: Config) -> list[str]:
    return await asyncio.to_thread(_discover_tickers_sync, config)


def _discover_tickers_sync(config: Config) -> list[str]:
    logger = logging.getLogger(__name__)
    discovered: list[str] = []
    seen: set[str] = set()

    def add_symbols(symbols: list[str]) -> None:
        for symbol in symbols:
            if symbol in seen:
                continue
            seen.add(symbol)
            discovered.append(symbol)

    screener_url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
    trending_url = f"https://query1.finance.yahoo.com/v1/finance/trending/{config.discovery_region}"
    search_url = "https://query1.finance.yahoo.com/v1/finance/search"

    try:
        gainers_payload = _fetch_json(
            screener_url,
            {
                "formatted": "false",
                "scrIds": "day_gainers",
                "count": config.day_gainers_limit,
                "start": 0,
            },
            timeout_seconds=config.request_timeout_seconds,
        )
        add_symbols(_extract_screener_symbols(gainers_payload, limit=config.day_gainers_limit))
    except (urlerror.URLError, TimeoutError, ValueError, TradeBotError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load day_gainers list: %s", exc)

    # Space screener calls to reduce HTTP 429 responses from Yahoo Finance.
    time.sleep(2.0)

    try:
        most_active_payload = _fetch_json(
            screener_url,
            {
                "formatted": "false",
                "scrIds": "most_actives",
                "count": config.most_active_limit,
                "start": 0,
            },
            timeout_seconds=config.request_timeout_seconds,
        )
        add_symbols(_extract_screener_symbols(most_active_payload, limit=config.most_active_limit))
    except (urlerror.URLError, TimeoutError, ValueError, TradeBotError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load most_actives list: %s", exc)

    try:
        trending_payload = _fetch_json(trending_url, {}, timeout_seconds=config.request_timeout_seconds)
        finance = trending_payload.get("finance")
        result = finance.get("result") if isinstance(finance, dict) else None
        quotes = result[0].get("quotes") if isinstance(result, list) and result and isinstance(result[0], dict) else []
        add_symbols(_extract_symbols(quotes))
    except (urlerror.URLError, TimeoutError, ValueError, TradeBotError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load trending ticker list: %s", exc)

    try:
        search_payload = _fetch_json(
            search_url,
            {
                "q": "stock market",
                "quotesCount": 0,
                "newsCount": config.trending_news_limit,
                "enableFuzzyQuery": "false",
                "region": config.discovery_region,
                "lang": "en-US",
            },
            timeout_seconds=config.request_timeout_seconds,
        )
        news_items = search_payload.get("news")
        if isinstance(news_items, list):
            for item in news_items:
                if not isinstance(item, dict):
                    continue
                related = item.get("relatedTickers")
                if isinstance(related, list):
                    add_symbols([s for s in (_normalize_symbol(raw) for raw in related) if s])
    except (urlerror.URLError, TimeoutError, ValueError, TradeBotError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load trending-news ticker list: %s", exc)

    if not discovered:
        logger.warning("Using fallback ticker list due to discovery failure")
        add_symbols(FALLBACK_TICKERS)

    return discovered


async def fetch_snapshot(ticker: str, news_lookback_days: int, max_news_items: int) -> MarketSnapshot:
    return await asyncio.to_thread(_fetch_snapshot_sync, ticker, news_lookback_days, max_news_items)


def _fetch_snapshot_sync(ticker: str, news_lookback_days: int, max_news_items: int) -> MarketSnapshot:
    symbol = yf.Ticker(ticker)

    history = symbol.history(period="1mo", interval="1d", auto_adjust=False)
    last_price = None
    previous_close = None
    price_change_pct = None
    trend = "unknown"

    if not history.empty:
        close_series = history["Close"].dropna()
        if not close_series.empty:
            last_price = float(close_series.iloc[-1])
        if len(close_series) >= 2:
            previous_close = float(close_series.iloc[-2])
        elif "Close" in history and not history["Close"].isna().all():
            previous_close = float(history["Close"].dropna().iloc[0])
        if last_price is not None and previous_close not in (None, 0):
            price_change_pct = ((last_price - previous_close) / previous_close) * 100.0
            if price_change_pct > 1.5:
                trend = "uptrend"
            elif price_change_pct < -1.5:
                trend = "downtrend"
            else:
                trend = "sideways"

    news_items: list[str] = []
    try:
        raw_news = getattr(symbol, "news", []) or []
        cutoff = dt.datetime.utcnow() - dt.timedelta(days=news_lookback_days)
        for item in raw_news:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            publisher = str(item.get("publisher") or "").strip()
            link = str(item.get("link") or "").strip()
            published_ts = item.get("providerPublishTime")
            published_text = ""
            if isinstance(published_ts, (int, float)):
                published_dt = dt.datetime.utcfromtimestamp(float(published_ts))
                if published_dt < cutoff:
                    continue
                published_text = published_dt.strftime("%Y-%m-%d %H:%M UTC")
            parts = [title]
            if publisher:
                parts.append(f"source={publisher}")
            if published_text:
                parts.append(f"published={published_text}")
            if link:
                parts.append(f"url={link}")
            news_items.append(" | ".join(parts))
            if len(news_items) >= max_news_items:
                break
    except Exception:
        logging.getLogger(__name__).exception("Failed to fetch news for %s", ticker)

    option_context: dict[str, Any] = {"available": False}
    try:
        expirations = getattr(symbol, "options", []) or []
        if expirations:
            expiration = expirations[0]
            chain = symbol.option_chain(expiration)
            calls = getattr(chain, "calls", None)
            selected_iv = None
            selected_strike = None
            if calls is not None and not calls.empty:
                if "impliedVolatility" in calls.columns:
                    calls_sorted = calls.sort_values(by=["impliedVolatility"], ascending=False)
                    selected_row = calls_sorted.iloc[0]
                    selected_iv = float(selected_row.get("impliedVolatility") or 0.0)
                    selected_strike = float(selected_row.get("strike") or 0.0)
            option_context = {
                "available": True,
                "expiration": expiration,
                "selected_call_iv": selected_iv,
                "selected_call_strike": selected_strike,
                "delta": None,
                "note": "yfinance does not reliably expose delta; ask the model to infer conservatively and prefer credit spreads when IV is elevated.",
            }
    except Exception:
        logging.getLogger(__name__).exception("Failed to fetch options context for %s", ticker)

    return MarketSnapshot(
        ticker=ticker,
        last_price=last_price,
        previous_close=previous_close,
        price_change_pct=price_change_pct,
        trend=trend,
        news=news_items,
        option_context=option_context,
    )


def build_prompt(snapshot: MarketSnapshot) -> str:
    price_line = "unknown"
    if snapshot.last_price is not None:
        if snapshot.price_change_pct is not None:
            price_line = f"last price={snapshot.last_price:.2f}, previous close={snapshot.previous_close:.2f}, change={snapshot.price_change_pct:.2f}%"
        else:
            price_line = f"last price={snapshot.last_price:.2f}"

    news_block = "\n".join(f"- {item}" for item in snapshot.news) if snapshot.news else "- No recent headlines were returned by yfinance."

    options_block = json.dumps(snapshot.option_context, indent=2, sort_keys=True)

    return f"""You are a disciplined market analyst generating one trading alert for {snapshot.ticker}.

Return plain text only, exactly in this format and order (10 lines), with no extra text before or after:
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

Hard requirements:
- Use this exact template and labels verbatim.
- Fill every field. Do not skip any field.
- Never use placeholders or vague wording.
- Every price-related field must include concrete numeric values tied to the provided price context.
- REASON must be exactly 2-3 sentences and reference both headlines and price action.
- WHEN must include a specific time/trigger and a numeric price level.
- OPTIONS PLAY must still be filled with a concrete, numeric setup tied to current price context.
- If trend and news conflict, be conservative; HOLD is allowed.
- If implied volatility is high, prefer credit spreads over naked long premium.
- If suggesting a naked option, target Delta between 0.30 and 0.40.
- If delta is unavailable, state that clearly in REASON or OPTIONS PLAY and provide your proxy assumption.
- Do not output JSON, markdown, bullets, or commentary.
- Do not mention that you are an AI.

Market snapshot:
- Ticker: {snapshot.ticker}
- Price context: {price_line}
- Trend classification: {snapshot.trend}
- Recent headlines:
{news_block}
- Option context:
{options_block}
"""


async def generate_analysis(config: Config, prompt: str) -> str:
    return await asyncio.to_thread(_generate_analysis_sync, config, prompt)


def _generate_analysis_sync(config: Config, prompt: str) -> str:
    _ = config
    client = openai.OpenAI(
        base_url="https://models.inference.ai.azure.com",
        api_key=os.getenv("GITHUB_TOKEN"),
    )
    response = client.chat.completions.create(
        model=os.getenv("MODEL_NAME", "gpt-4o"),
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1000,
    )
    content = response.choices[0].message.content
    if content is None:
        raise TradeBotError("AI response content was empty.")
    return str(content)


def normalize_analysis(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
    return {
        "ticker": "unknown",
        "action": "HOLD",
        "confidence": 0,
        "summary": text[:3000],
        "rationale": ["Model response could not be parsed as JSON."],
        "risk_notes": ["Review the raw response before acting."],
        "equity_view": "Unavailable",
        "options_view": "Unavailable",
        "telegram_message": text[:3500],
    }


def render_alert(analysis: dict[str, Any]) -> str:
    ticker = analysis.get("ticker", "UNKNOWN")
    action = analysis.get("action", "HOLD")
    confidence = analysis.get("confidence", 0)
    summary = analysis.get("summary", "No summary provided.")
    equity_view = analysis.get("equity_view", "")
    options_view = analysis.get("options_view", "")
    rationale = analysis.get("rationale", [])
    risk_notes = analysis.get("risk_notes", [])

    lines = [
        f"{ticker} | {action} | confidence {confidence}%",
        summary,
    ]
    if equity_view:
        lines.append(f"Equity view: {equity_view}")
    if options_view:
        lines.append(f"Options view: {options_view}")
    if rationale:
        lines.append("Reasons:")
        lines.extend(f"- {item}" for item in rationale[:3])
    if risk_notes:
        lines.append("Risks:")
        lines.extend(f"- {item}" for item in risk_notes[:3])
    return "\n".join(lines)


def extract_alert_field(message: str, field_name: str) -> str:
    token = f"{field_name.upper()}:"
    for line in message.splitlines():
        uppercase_line = line.upper()
        index = uppercase_line.find(token)
        if index != -1:
            return line[index + len(token) :].strip()
    return ""


def has_actionable_signal(analysis: dict[str, Any], telegram_message: str) -> bool:
    signal = extract_alert_field(telegram_message, "SIGNAL").upper()
    if not signal:
        signal = str(analysis.get("action", "")).strip().upper()

    confidence = extract_alert_field(telegram_message, "CONFIDENCE").upper()
    if not confidence:
        raw_confidence = analysis.get("confidence")
        if isinstance(raw_confidence, (int, float)):
            if raw_confidence >= 70:
                confidence = "HIGH"
            elif raw_confidence >= 40:
                confidence = "MEDIUM"
            else:
                confidence = "LOW"
        else:
            confidence = str(raw_confidence or "").strip().upper()

    reason = extract_alert_field(telegram_message, "REASON")
    if not reason:
        rationale = analysis.get("rationale")
        if isinstance(rationale, list):
            reason = " ".join(str(item) for item in rationale if item)
        elif rationale:
            reason = str(rationale)
        else:
            reason = str(analysis.get("summary", ""))

    reason_lower = reason.lower()
    has_catalyst = any(
        re.search(pattern, reason_lower)
        for pattern in (
            r"\bearnings\b",
            r"\bnews\b",
            r"\bbreakout\b",
            r"\bunusual volume\b",
            r"\boptions activity\b",
        )
    )

    return signal in {"BUY", "SELL"} or confidence in {"HIGH", "MEDIUM"} or has_catalyst


def alert_hash(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


async def send_telegram_message(bot: Bot, chat_id: str, message: str) -> None:
    await bot.send_message(chat_id=chat_id, text=message)


async def process_ticker(config: Config, bot: Bot, state: dict[str, Any], ticker: str) -> None:
    logger = logging.getLogger(__name__)
    try:
        snapshot = await fetch_snapshot(ticker, config.news_lookback_days, config.max_news_items)
        prompt = build_prompt(snapshot)
        raw_analysis = await generate_analysis(config, prompt)
        analysis = normalize_analysis(raw_analysis)

        telegram_message = analysis.get("telegram_message") or render_alert(analysis)
        telegram_message = str(telegram_message).strip()
        if not telegram_message:
            telegram_message = render_alert(analysis)

        if not has_actionable_signal(analysis, telegram_message):
            logger.info("Skipping %s - no actionable signal", ticker)
            return

        current_hash = alert_hash(telegram_message)
        sent_hashes = state.setdefault("sent_hashes", {})
        if sent_hashes.get(ticker) == current_hash:
            logger.info("Skipping duplicate alert for %s", ticker)
            return

        await send_telegram_message(bot, config.telegram_chat_id, telegram_message)
        sent_hashes[ticker] = current_hash
        state["last_run_utc"] = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        save_state(config.state_file, state)
        logger.info("Sent alert for %s", ticker)
    except Exception:
        logger.exception("Failed processing ticker %s", ticker)


async def run_forever(config: Config) -> None:
    logger = logging.getLogger(__name__)
    bot = Bot(token=config.telegram_bot_token)
    state = load_state(config.state_file)

    logger.info("Starting trading bot with dynamic ticker discovery")
    logger.info(
        "Discovery limits: day_gainers=%d, most_actives=%d, trending_news=%d",
        config.day_gainers_limit,
        config.most_active_limit,
        config.trending_news_limit,
    )
    logger.info("Check interval: %s minutes", config.check_interval_minutes)

    while True:
        started_at = dt.datetime.utcnow()
        tickers = await discover_tickers(config)
        if tickers:
            state["last_discovered_tickers"] = tickers
            save_state(config.state_file, state)
        else:
            fallback = state.get("last_discovered_tickers", [])
            if isinstance(fallback, list) and fallback:
                tickers = [str(symbol).upper() for symbol in fallback if str(symbol).strip()]
                logger.warning("Discovery returned no symbols; using last discovered set of %d tickers", len(tickers))

        if tickers:
            logger.info("Discovered %d tickers this cycle: %s", len(tickers), ", ".join(tickers))
            for index, ticker in enumerate(tickers):
                await process_ticker(config, bot, state, ticker)
                if index < len(tickers) - 1:
                    await asyncio.sleep(5)
        else:
            logger.warning("Ticker discovery returned no symbols and no fallback was available.")

        elapsed = (dt.datetime.utcnow() - started_at).total_seconds()
        sleep_for = max(0, config.check_interval_minutes * 60 - elapsed)
        logger.info("Cycle complete; sleeping for %.0f seconds", sleep_for)
        await asyncio.sleep(sleep_for)


async def main() -> None:
    config = load_config()
    setup_logging(config.log_level)
    await run_forever(config)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except TradeBotError as exc:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        logging.getLogger(__name__).error(str(exc))
        raise SystemExit(1)
