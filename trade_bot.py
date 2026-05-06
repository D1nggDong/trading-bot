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
    discovery_cache_minutes: int
    max_ai_candidates: int
    min_signal_price_change_pct: float
    min_signal_relative_volume: float
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
    sma20: float | None
    sma50: float | None
    support_20d: float | None
    resistance_20d: float | None
    avg_volume_20d: float | None
    last_volume: float | None
    relative_volume: float | None
    news: list[str]
    option_context: dict[str, Any]


class TradeBotError(RuntimeError):
    pass


def load_config() -> Config:
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    groq_api_key = os.getenv("GROQ_API_KEY", "").strip()
    copilot_model = os.getenv("COPILOT_MODEL", "claude-3.5-sonnet").strip()

    if not telegram_bot_token:
        raise TradeBotError("TELEGRAM_BOT_TOKEN is missing")
    if not telegram_chat_id:
        raise TradeBotError("TELEGRAM_CHAT_ID is missing")
    if not groq_api_key:
        raise TradeBotError("GROQ_API_KEY is missing")

    check_interval_minutes = max(5, int(os.getenv("CHECK_INTERVAL_MINUTES", "60")))
    day_gainers_limit = max(1, int(os.getenv("DAY_GAINERS_LIMIT", "10")))
    most_active_limit = max(1, int(os.getenv("MOST_ACTIVE_LIMIT", "10")))
    trending_news_limit = max(1, int(os.getenv("TRENDING_NEWS_LIMIT", "25")))
    discovery_region = os.getenv("DISCOVERY_REGION", "US").strip().upper() or "US"
    request_timeout_seconds = max(2.0, float(os.getenv("REQUEST_TIMEOUT_SECONDS", "12")))
    max_parallel_tickers = max(1, int(os.getenv("MAX_PARALLEL_TICKERS", "1")))
    discovery_cache_minutes = max(15, int(os.getenv("DISCOVERY_CACHE_MINUTES", "240")))
    max_ai_candidates = max(1, int(os.getenv("MAX_AI_CANDIDATES", "3")))
    min_signal_price_change_pct = max(0.0, float(os.getenv("MIN_SIGNAL_PRICE_CHANGE_PCT", "1.0")))
    min_signal_relative_volume = max(0.0, float(os.getenv("MIN_SIGNAL_RELATIVE_VOLUME", "1.2")))
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
        discovery_cache_minutes=discovery_cache_minutes,
        max_ai_candidates=max_ai_candidates,
        min_signal_price_change_pct=min_signal_price_change_pct,
        min_signal_relative_volume=min_signal_relative_volume,
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
    time.sleep(8.0)

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

    time.sleep(4.0)

    try:
        trending_payload = _fetch_json(trending_url, {}, timeout_seconds=config.request_timeout_seconds)
        finance = trending_payload.get("finance")
        result = finance.get("result") if isinstance(finance, dict) else None
        quotes = result[0].get("quotes") if isinstance(result, list) and result and isinstance(result[0], dict) else []
        add_symbols(_extract_symbols(quotes))
    except (urlerror.URLError, TimeoutError, ValueError, TradeBotError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load trending ticker list: %s", exc)

    time.sleep(4.0)

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


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        result = float(value)
        if result != result:
            return None
        return result
    except (TypeError, ValueError):
        return None


def _fetch_snapshot_sync(ticker: str, news_lookback_days: int, max_news_items: int) -> MarketSnapshot:
    symbol = yf.Ticker(ticker)

    history = symbol.history(period="3mo", interval="1d", auto_adjust=False)
    last_price = None
    previous_close = None
    price_change_pct = None
    trend = "unknown"
    sma20 = None
    sma50 = None
    support_20d = None
    resistance_20d = None
    avg_volume_20d = None
    last_volume = None
    relative_volume = None

    if not history.empty:
        close_series = history["Close"].dropna()
        volume_series = history["Volume"].dropna() if "Volume" in history else None
        if not close_series.empty:
            last_price = float(close_series.iloc[-1])
        if len(close_series) >= 2:
            previous_close = float(close_series.iloc[-2])
        elif "Close" in history and not history["Close"].isna().all():
            previous_close = float(history["Close"].dropna().iloc[0])
        if len(close_series) >= 20:
            sma20 = float(close_series.tail(20).mean())
            support_20d = float(close_series.tail(20).min())
            resistance_20d = float(close_series.tail(20).max())
        if len(close_series) >= 50:
            sma50 = float(close_series.tail(50).mean())
        if volume_series is not None and not volume_series.empty:
            last_volume = float(volume_series.iloc[-1])
            if len(volume_series) >= 20:
                avg_volume_20d = float(volume_series.tail(20).mean())
                if avg_volume_20d:
                    relative_volume = last_volume / avg_volume_20d
        if last_price is not None and previous_close not in (None, 0):
            price_change_pct = ((last_price - previous_close) / previous_close) * 100.0

        above_sma20 = last_price is not None and sma20 is not None and last_price > sma20
        above_sma50 = last_price is not None and sma50 is not None and last_price > sma50
        below_sma20 = last_price is not None and sma20 is not None and last_price < sma20
        below_sma50 = last_price is not None and sma50 is not None and last_price < sma50
        high_volume = relative_volume is not None and relative_volume >= 1.4
        if price_change_pct is not None and price_change_pct > 1.5 and above_sma20 and (above_sma50 or high_volume):
            trend = "confirmed uptrend"
        elif price_change_pct is not None and price_change_pct < -1.5 and below_sma20 and (below_sma50 or high_volume):
            trend = "confirmed downtrend"
        elif price_change_pct is not None and abs(price_change_pct) <= 1.5:
            trend = "sideways / no clear daily impulse"
        else:
            trend = "mixed / unconfirmed"

    news_items: list[str] = []
    try:
        raw_news = getattr(symbol, "news", []) or []
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=news_lookback_days)
        for item in raw_news:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            publisher = str(item.get("publisher") or "").strip()
            link = str(item.get("link") or "").strip()
            published_ts = item.get("providerPublishTime")
            published_text = ""
            if isinstance(published_ts, (int, float)):
                published_dt = dt.datetime.fromtimestamp(float(published_ts), tz=dt.UTC)
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

    option_context: dict[str, Any] = {"available": False, "reason": "No option chain returned by yfinance."}
    try:
        expirations = getattr(symbol, "options", []) or []
        if expirations:
            expiration = expirations[0]
            chain = symbol.option_chain(expiration)
            calls = getattr(chain, "calls", None)
            puts = getattr(chain, "puts", None)
            def summarize_chain(df: Any) -> dict[str, Any] | None:
                if df is None or df.empty:
                    return None
                rows = df.copy()
                if last_price is not None and "strike" in rows.columns:
                    rows["distance"] = (rows["strike"] - last_price).abs()
                    rows = rows.sort_values(by=["distance"])
                row = rows.iloc[0]
                return {
                    "strike": _safe_float(row.get("strike")),
                    "lastPrice": _safe_float(row.get("lastPrice")),
                    "bid": _safe_float(row.get("bid")),
                    "ask": _safe_float(row.get("ask")),
                    "volume": _safe_float(row.get("volume")),
                    "openInterest": _safe_float(row.get("openInterest")),
                    "impliedVolatility": _safe_float(row.get("impliedVolatility")),
                }
            option_context = {
                "available": True,
                "nearest_expiration": expiration,
                "nearest_call": summarize_chain(calls),
                "nearest_put": summarize_chain(puts),
                "delta": None,
                "rule": "Only suggest options when the equity setup is actionable. Prefer defined-risk spreads; avoid naked premium when IV is elevated or liquidity is weak.",
            }
    except Exception:
        logging.getLogger(__name__).exception("Failed to fetch options context for %s", ticker)

    return MarketSnapshot(
        ticker=ticker,
        last_price=last_price,
        previous_close=previous_close,
        price_change_pct=price_change_pct,
        trend=trend,
        sma20=sma20,
        sma50=sma50,
        support_20d=support_20d,
        resistance_20d=resistance_20d,
        avg_volume_20d=avg_volume_20d,
        last_volume=last_volume,
        relative_volume=relative_volume,
        news=news_items,
        option_context=option_context,
    )


def _fmt_money(value: float | None) -> str:
    return "unknown" if value is None else f"${value:.2f}"


def _fmt_number(value: float | None, suffix: str = "") -> str:
    return "unknown" if value is None else f"{value:.2f}{suffix}"


def build_prompt(snapshot: MarketSnapshot) -> str:
    price_line = "unknown"
    if snapshot.last_price is not None:
        if snapshot.price_change_pct is not None and snapshot.previous_close is not None:
            price_line = f"last price={snapshot.last_price:.2f}, previous close={snapshot.previous_close:.2f}, daily change={snapshot.price_change_pct:.2f}%"
        else:
            price_line = f"last price={snapshot.last_price:.2f}"

    news_block = "\n".join(f"- {item}" for item in snapshot.news) if snapshot.news else "- No recent headlines were returned by yfinance."
    options_block = json.dumps(snapshot.option_context, indent=2, sort_keys=True)

    return f"""You are a disciplined trading analyst. Your job is to produce one conservative, evidence-based trade assessment for {snapshot.ticker}.

Return JSON only. No markdown, no prose outside JSON. Use exactly this schema:
{{
  "ticker": "SYMBOL",
  "action": "BUY|SELL|HOLD",
  "confidence": 0,
  "timeframe": "intraday|swing|watchlist",
  "entry": "specific trigger or price range",
  "stop_loss": "specific invalidation price",
  "target_price": "specific target price or range",
  "risk_reward": "estimated reward:risk, e.g. 2.1:1, or unavailable",
  "options_play": "defined-risk options idea or 'No options play; equity/watchlist only'",
  "summary": "one concise sentence",
  "rationale": ["reason 1", "reason 2", "reason 3"],
  "risk_notes": ["risk 1", "risk 2"],
  "telegram_message": "ready-to-send Telegram alert"
}}

Decision rules:
- Default to HOLD unless price action, volume, and news/catalyst are aligned.
- BUY requires bullish price action above/near support, constructive trend, and either a catalyst/headline or relative volume confirmation.
- SELL requires bearish price action below key averages/support or clearly negative catalyst.
- Confidence must be 70+ only when there is strong confluence. Use 40-69 for watchlist/mixed setups. Use below 40 for weak/no setup.
- Do not force a trade. HOLD/watchlist is a valid and preferred result when evidence is thin.
- Entry, stop, and target must be numeric when action is BUY or SELL.
- Target should offer at least ~1.5:1 reward:risk. If it does not, action should be HOLD.
- Options play must be defined-risk and only suggested when options data is available and the equity setup is actionable. Otherwise say no options play.
- Telegram message must include: ticker, action, confidence, current price, entry trigger, stop, target, reason, risk, and a short "not financial advice" line.

Market snapshot:
- Ticker: {snapshot.ticker}
- Price context: {price_line}
- Trend classification: {snapshot.trend}
- SMA20: {_fmt_money(snapshot.sma20)}
- SMA50: {_fmt_money(snapshot.sma50)}
- 20-day support: {_fmt_money(snapshot.support_20d)}
- 20-day resistance: {_fmt_money(snapshot.resistance_20d)}
- Last volume: {_fmt_number(snapshot.last_volume)}
- 20-day average volume: {_fmt_number(snapshot.avg_volume_20d)}
- Relative volume: {_fmt_number(snapshot.relative_volume, "x")}
- Recent headlines:
{news_block}
- Option context:
{options_block}
"""


async def generate_analysis(config: Config, prompt: str) -> str:
    return await asyncio.to_thread(_generate_analysis_sync, config, prompt)


def _generate_analysis_sync(config: Config, prompt: str) -> str:
    _ = config
    logger = logging.getLogger(__name__)
    provider_order = [p.strip().lower() for p in os.getenv("AI_PROVIDER_ORDER", "groq,github").split(",") if p.strip()]
    if not provider_order:
        provider_order = ["groq", "github"]

    errors: list[str] = []
    for provider in provider_order:
        try:
            if provider == "groq":
                api_key = os.getenv("GROQ_API_KEY")
                if not api_key:
                    errors.append("groq: missing GROQ_API_KEY")
                    continue
                client = openai.OpenAI(
                    base_url="https://api.groq.com/openai/v1",
                    api_key=api_key,
                )
                model = os.getenv("MODEL_NAME", "llama-3.3-70b-versatile")
            elif provider == "github":
                api_key = os.getenv("GITHUB_MODELS_TOKEN")
                if not api_key:
                    errors.append("github: missing GITHUB_MODELS_TOKEN")
                    continue
                client = openai.OpenAI(
                    base_url=os.getenv("GITHUB_MODELS_BASE_URL", "https://models.github.ai/inference"),
                    api_key=api_key,
                )
                model = os.getenv("GITHUB_MODELS_MODEL", "openai/gpt-4.1-mini")
            else:
                errors.append(f"{provider}: unknown provider")
                continue

            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "Return valid JSON only. Be conservative. Never claim certainty or guaranteed profit."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=1200,
            )
            content = response.choices[0].message.content
            if content is None:
                raise TradeBotError(f"{provider} response content was empty.")
            logger.info("Generated analysis using %s model %s", provider, model)
            return str(content)
        except openai.RateLimitError as exc:
            errors.append(f"{provider}: rate limited: {str(exc)[:180]}")
            logger.warning("AI provider %s rate limited; trying fallback if available", provider)
            continue
        except Exception as exc:
            errors.append(f"{provider}: {type(exc).__name__}: {str(exc)[:180]}")
            logger.warning("AI provider %s failed; trying fallback if available: %s", provider, exc)
            continue

    raise TradeBotError("All AI providers failed: " + " | ".join(errors))


def normalize_analysis(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                parsed = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                parsed = None
        else:
            parsed = None
    if not isinstance(parsed, dict):
        return {
            "ticker": "UNKNOWN",
            "action": "HOLD",
            "confidence": 0,
            "summary": text[:500],
            "rationale": ["Model response could not be parsed as JSON."],
            "risk_notes": ["Skipped unless manually reviewed."],
            "telegram_message": text[:3000],
        }

    action = str(parsed.get("action", "HOLD")).strip().upper()
    if action not in {"BUY", "SELL", "HOLD"}:
        action = "HOLD"
    parsed["action"] = action
    try:
        parsed["confidence"] = max(0, min(100, int(float(parsed.get("confidence", 0)))))
    except (TypeError, ValueError):
        parsed["confidence"] = 0
    for key in ("rationale", "risk_notes"):
        value = parsed.get(key)
        if isinstance(value, str):
            parsed[key] = [value]
        elif not isinstance(value, list):
            parsed[key] = []
    return parsed


def render_alert(analysis: dict[str, Any]) -> str:
    message = analysis.get("telegram_message")
    if isinstance(message, str) and message.strip():
        return message.strip()[:3900]

    ticker = str(analysis.get("ticker", "UNKNOWN")).upper()
    action = str(analysis.get("action", "HOLD")).upper()
    confidence = analysis.get("confidence", 0)
    lines = [
        f"📊 {ticker} | {action} | confidence {confidence}%",
        f"Summary: {analysis.get('summary', 'No summary provided.')}",
        f"Entry: {analysis.get('entry', 'watch only')}",
        f"Stop: {analysis.get('stop_loss', 'n/a')}",
        f"Target: {analysis.get('target_price', 'n/a')}",
        f"Risk/reward: {analysis.get('risk_reward', 'unavailable')}",
        f"Options: {analysis.get('options_play', 'No options play; equity/watchlist only')}",
    ]
    rationale = analysis.get("rationale", [])
    if rationale:
        lines.append("Reasons: " + "; ".join(str(item) for item in rationale[:3]))
    risks = analysis.get("risk_notes", [])
    if risks:
        lines.append("Risks: " + "; ".join(str(item) for item in risks[:2]))
    lines.append("Not financial advice — verify levels and size risk before acting.")
    return "\n".join(lines)[:3900]


def extract_alert_field(message: str, field_name: str) -> str:
    token = f"{field_name.upper()}:"
    for line in message.splitlines():
        uppercase_line = line.upper()
        index = uppercase_line.find(token)
        if index != -1:
            return line[index + len(token) :].strip()
    return ""


def _contains_number(value: Any) -> bool:
    return bool(re.search(r"\d+(?:\.\d+)?", str(value or "")))


def has_actionable_signal(analysis: dict[str, Any], telegram_message: str) -> bool:
    """Only send high-confluence, actually actionable BUY/SELL alerts.

    This intentionally blocks HOLD/watchlist ideas and low-confidence model output so
    the bot does not spam half-baked calls just because a headline had exciting words.
    """
    action = str(analysis.get("action", "")).strip().upper()
    if action not in {"BUY", "SELL"}:
        return False

    try:
        confidence = int(float(analysis.get("confidence", 0)))
    except (TypeError, ValueError):
        confidence = 0
    if confidence < 70:
        return False

    entry = analysis.get("entry")
    stop_loss = analysis.get("stop_loss")
    target_price = analysis.get("target_price")
    if not (_contains_number(entry) and _contains_number(stop_loss) and _contains_number(target_price)):
        return False

    combined_reason = " ".join(str(item) for item in analysis.get("rationale", []))
    combined_reason += " " + str(analysis.get("summary", ""))
    combined_reason += " " + telegram_message
    reason_lower = combined_reason.lower()
    has_price_action = any(term in reason_lower for term in ("breakout", "support", "resistance", "sma", "volume", "trend", "relative volume"))
    has_catalyst_or_confirmation = any(
        term in reason_lower
        for term in ("earnings", "guidance", "upgrade", "downgrade", "contract", "approval", "news", "catalyst", "volume")
    )
    return has_price_action and has_catalyst_or_confirmation


def record_signal(config: Config, analysis: dict[str, Any], telegram_message: str) -> None:
    signal_path = ROOT / os.getenv("PAPER_SIGNAL_FILE", "signals.jsonl")
    record = {
        "timestamp": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "ticker": analysis.get("ticker"),
        "action": analysis.get("action"),
        "confidence": analysis.get("confidence"),
        "entry": analysis.get("entry"),
        "stop_loss": analysis.get("stop_loss"),
        "target_price": analysis.get("target_price"),
        "risk_reward": analysis.get("risk_reward"),
        "summary": analysis.get("summary"),
        "rationale": analysis.get("rationale", []),
        "risk_notes": analysis.get("risk_notes", []),
        "telegram_message": telegram_message,
    }
    signal_path.parent.mkdir(parents=True, exist_ok=True)
    with signal_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def maybe_paper_trade(analysis: dict[str, Any], telegram_message: str) -> dict[str, Any] | None:
    if os.getenv("PAPER_TRADING_ENABLED", "false").strip().lower() not in {"1", "true", "yes", "on"}:
        return None
    try:
        import paper_trader

        signal = dict(analysis)
        signal["telegram_message"] = telegram_message
        result = paper_trader.process_signal(signal)
        logging.getLogger(__name__).info(
            "Paper trader decision for %s: %s (%s)",
            result.get("symbol", analysis.get("ticker")),
            result.get("decision"),
            result.get("reason", "no reason"),
        )
        return result
    except Exception:
        logging.getLogger(__name__).exception("Paper trader failed")
        return {"decision": "error", "reason": "paper trader exception", "symbol": analysis.get("ticker")}


def render_trade_notification(analysis: dict[str, Any], trade_result: dict[str, Any]) -> str:
    symbol = str(trade_result.get("symbol") or analysis.get("ticker") or "UNKNOWN").upper()
    decision = str(trade_result.get("decision", "unknown"))
    order = trade_result.get("order") if isinstance(trade_result.get("order"), dict) else {}
    rationale = analysis.get("rationale", [])
    if isinstance(rationale, list):
        reason = "; ".join(str(item) for item in rationale[:3])
    else:
        reason = str(rationale or analysis.get("summary", "No rationale provided."))

    if decision == "submitted":
        lines = [
            f"✅ PAPER BUY EXECUTED: {symbol}",
            f"Qty: {order.get('qty', 'unknown')}",
            f"Order: {order.get('type', 'market')} {order.get('side', 'buy')} with bracket",
            f"Take profit: {order.get('take_profit', {}).get('limit_price', analysis.get('target_price', 'n/a'))}",
            f"Stop loss: {order.get('stop_loss', {}).get('stop_price', analysis.get('stop_loss', 'n/a'))}",
            f"Confidence: {analysis.get('confidence', 'n/a')}",
            f"Reason: {reason}",
            "Paper trade only — not financial advice.",
        ]
    elif decision == "dry_run":
        lines = [
            f"🧪 PAPER TRADE DRY-RUN: {symbol}",
            f"Would buy qty: {order.get('qty', 'unknown')}",
            f"Reason: {reason}",
        ]
    elif decision == "skipped":
        lines = [
            f"⏭️ PAPER TRADE SKIPPED: {symbol}",
            f"Reason: {trade_result.get('reason', 'unknown')}",
        ]
    else:
        lines = [
            f"⚠️ PAPER TRADER STATUS: {symbol}",
            f"Decision: {decision}",
            f"Reason: {trade_result.get('reason', 'unknown')}",
        ]
    return "\n".join(lines)[:3900]


def alert_hash(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


async def send_telegram_message(bot: Bot, chat_id: str, message: str) -> None:
    await bot.send_message(chat_id=chat_id, text=message)


def _parse_utc_timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.UTC)
        return parsed.astimezone(dt.UTC)
    except ValueError:
        return None


def get_cached_tickers(state: dict[str, Any], max_age_minutes: int) -> list[str]:
    tickers = state.get("last_discovered_tickers", [])
    if not isinstance(tickers, list) or not tickers:
        return []
    timestamp = _parse_utc_timestamp(state.get("last_discovered_tickers_utc"))
    if timestamp is None:
        return []
    age = dt.datetime.now(dt.UTC) - timestamp
    if age > dt.timedelta(minutes=max_age_minutes):
        return []
    return [str(symbol).upper() for symbol in tickers if str(symbol).strip()]


def ai_rate_limited_until(state: dict[str, Any]) -> dt.datetime | None:
    timestamp = _parse_utc_timestamp(state.get("ai_rate_limited_until_utc"))
    if timestamp is None or timestamp <= dt.datetime.now(dt.UTC):
        return None
    return timestamp


def mark_ai_rate_limited(state: dict[str, Any], config: Config, minutes: int = 15) -> None:
    until = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=minutes)
    state["ai_rate_limited_until_utc"] = until.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    save_state(config.state_file, state)


def prequalifies_snapshot(snapshot: MarketSnapshot, config: Config) -> tuple[bool, str]:
    if snapshot.last_price is None:
        return False, "missing price data"
    abs_change = abs(snapshot.price_change_pct or 0.0)
    rel_volume = snapshot.relative_volume or 0.0
    has_recent_news = bool(snapshot.news)
    strong_price_move = abs_change >= config.min_signal_price_change_pct
    strong_volume = rel_volume >= config.min_signal_relative_volume
    constructive_trend = snapshot.trend in {"confirmed uptrend", "confirmed downtrend", "mixed / unconfirmed"}
    if not constructive_trend and not has_recent_news:
        return False, f"weak trend ({snapshot.trend}) and no recent news"
    if not (strong_price_move or strong_volume or has_recent_news):
        return False, f"no catalyst: change={abs_change:.2f}%, relvol={rel_volume:.2f}x"
    return True, "qualified"


async def process_ticker(config: Config, bot: Bot, state: dict[str, Any], ticker: str) -> None:
    logger = logging.getLogger(__name__)
    try:
        limited_until = ai_rate_limited_until(state)
        github_fallback_available = bool(os.getenv("GITHUB_MODELS_TOKEN", "").strip())
        if limited_until is not None and not github_fallback_available:
            logger.warning("Skipping %s - AI rate limited until %s", ticker, limited_until.isoformat())
            return

        snapshot = await fetch_snapshot(ticker, config.news_lookback_days, config.max_news_items)
        qualified, reason = prequalifies_snapshot(snapshot, config)
        if not qualified:
            logger.info("Skipping %s before AI - %s", ticker, reason)
            return

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

        record_signal(config, analysis, telegram_message)
        trade_result = maybe_paper_trade(analysis, telegram_message)

        await send_telegram_message(bot, config.telegram_chat_id, telegram_message)
        if trade_result and trade_result.get("decision") in {"submitted", "dry_run"}:
            await send_telegram_message(bot, config.telegram_chat_id, render_trade_notification(analysis, trade_result))
        sent_hashes[ticker] = current_hash
        state["last_run_utc"] = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        save_state(config.state_file, state)
        logger.info("Sent alert for %s", ticker)
    except openai.RateLimitError as exc:
        mark_ai_rate_limited(state, config, minutes=15)
        logger.warning("AI rate limited while processing %s; pausing AI calls for 15 minutes: %s", ticker, exc)
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
        started_at = dt.datetime.now(dt.UTC)
        tickers = get_cached_tickers(state, config.discovery_cache_minutes)
        if tickers:
            logger.info("Using cached ticker discovery (%d symbols, max age %d minutes)", len(tickers), config.discovery_cache_minutes)
        else:
            tickers = await discover_tickers(config)
            if tickers:
                state["last_discovered_tickers"] = tickers
                state["last_discovered_tickers_utc"] = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                save_state(config.state_file, state)
            else:
                fallback = state.get("last_discovered_tickers", [])
                if isinstance(fallback, list) and fallback:
                    tickers = [str(symbol).upper() for symbol in fallback if str(symbol).strip()]
                    logger.warning("Discovery returned no symbols; using last discovered set of %d tickers", len(tickers))

        if tickers:
            tickers = tickers[: config.max_ai_candidates]
            logger.info("Processing %d ticker candidates this cycle: %s", len(tickers), ", ".join(tickers))
            for index, ticker in enumerate(tickers):
                limited_until = ai_rate_limited_until(state)
                github_fallback_available = bool(os.getenv("GITHUB_MODELS_TOKEN", "").strip())
                if limited_until is not None and not github_fallback_available:
                    logger.warning("Stopping candidate processing until AI rate limit clears at %s", limited_until.isoformat())
                    break
                await process_ticker(config, bot, state, ticker)
                if index < len(tickers) - 1:
                    await asyncio.sleep(5)
        else:
            logger.warning("Ticker discovery returned no symbols and no fallback was available.")

        elapsed = (dt.datetime.now(dt.UTC) - started_at).total_seconds()
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
