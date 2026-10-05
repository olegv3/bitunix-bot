#!/usr/bin/env python3
"""Daily technical watch. Not a buy signal."""

import logging
import os
import time

import requests

from market_filters import allowed, chart_link, ta_snapshot

SCAN_SECONDS = 6 * 60 * 60
MIN_VOLUME_USDT = 500000
TICKERS_URL = "https://fapi.bitunix.com/api/v1/futures/market/tickers"
KLINE_URL = "https://fapi.bitunix.com/api/v1/futures/market/kline"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bitunix-technical")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
sent = {}


def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(text)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception as exc:
        log.error("Telegram send failed: %s", exc)


def ema(values: list, length: int) -> float:
    k = 2 / (length + 1)
    value = values[0]
    for price in values[1:]:
        value = price * k + value * (1 - k)
    return value


def rsi(values: list) -> float:
    gain = loss = 0.0
    for older, newer in zip(values[-15:-1], values[-14:]):
        diff = newer - older
        gain += max(diff, 0)
        loss += max(-diff, 0)
    return 100 if loss == 0 else 100 - 100 / (1 + gain / loss)


def scan() -> None:
    rows = requests.get(TICKERS_URL, timeout=20).json().get("data") or []
    for row in rows:
        symbol = row.get("symbol")
        try:
            volume = float(row.get("quoteVol") or 0)
            live = float(row.get("lastPrice") or row.get("last") or 0)
        except (TypeError, ValueError):
            continue
        if not symbol or not allowed(symbol) or volume < MIN_VOLUME_USDT or live <= 0:
            continue
        try:
            candles = requests.get(
                KLINE_URL, params={"symbol": symbol, "interval": "1d", "limit": "220"}, timeout=12
            ).json().get("data") or []
            candles = sorted(candles, key=lambda item: int(item.get("time") or 0))
            closes = [float(item["close"]) for item in candles]
            lows = [float(item["low"]) for item in candles]
        except (TypeError, ValueError, requests.RequestException):
            continue
        if len(closes) < 55 or closes[-1] <= 0:
            continue
        if abs(live - closes[-1]) / closes[-1] > 0.08:
            log.info("Skip %s, daily close %.6g vs live %.6g", symbol, closes[-1], live)
            continue
        price = live
        closes[-1] = live
        fast20, slow50 = ema(closes, 20), ema(closes, 50)
        value = rsi(closes)
        macd = ema(closes, 12) - ema(closes, 26)
        mid = sum(closes[-20:]) / 20
        band = (sum((item - mid) ** 2 for item in closes[-20:]) / 20) ** 0.5
        checks = []
        if fast20 > slow50 and price > slow50:
            checks.append("uptrend, 20 above 50")
        if 40 <= value <= 65:
            checks.append(f"RSI {value:.0f}")
        if macd > 0:
            checks.append("MACD positive")
        if price <= mid + band:
            checks.append("not above the upper band")
        if len(closes) >= 200 and ema(closes, 50) > ema(closes, 200):
            checks.append("50 above 200")
        if len(checks) < 4 or time.time() - sent.get(symbol, 0) < 3 * 24 * 60 * 60:
            continue
        buy = max(slow50, min(lows[-20:]))
        if buy >= price:
            buy = price * 0.99
        sent[symbol] = time.time()
        change = (price - closes[-20]) / closes[-20] * 100
        send_telegram(
            f"📘 <b>TECH WATCH</b> {symbol}\n"
            f"{len(checks)} of 5 daily checks agree\n"
            + "\n".join(f"• {item}" for item in checks)
            + f"\nPrice {price:.6g}, 20d {change:+.1f}%\n"
            f"Buy level <b>{buy:.6g}</b>. Still a watch, not an order\n"
            f"{chart_link(symbol)}\n"
            f"{ta_snapshot(symbol, price)}"
        )
        log.warning("TECH WATCH %s %s checks", symbol, len(checks))


def main() -> None:
    log.info("Starting daily technical watch")
    send_telegram("Technical watch is running. Needs 4 daily checks to agree. Not an order.")
    while True:
        try:
            scan()
        except Exception as exc:
            log.error("Scan failed: %s", exc)
        time.sleep(SCAN_SECONDS)


if __name__ == "__main__":
    main()
