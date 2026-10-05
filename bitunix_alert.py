#!/usr/bin/env python3
"""Confirmed Bitunix futures spike/drop alerts."""

import logging
import os
import time
from collections import defaultdict, deque

import requests

from market_context import note_btc, score as context_score
from market_filters import allowed, chart_link, strength

POLL_INTERVAL = 5
LOOKBACK_SECONDS = 10
THRESHOLD_PCT = 2.0
MIN_VOLUME_USDT = 200000
COOLDOWN_SECONDS = 180
TICKERS_URL = "https://fapi.bitunix.com/api/v1/futures/market/tickers"
KLINE_URL = "https://fapi.bitunix.com/api/v1/futures/market/kline"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bitunix-alert")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
history = defaultdict(lambda: deque(maxlen=200))
last_alert = {}
pending = {}
ENTRY_WAIT_SECONDS = 180


def ema(values: list, length: int) -> float:
    k = 2 / (length + 1)
    value = values[0]
    for price in values[1:]:
        value = price * k + value * (1 - k)
    return value


def ta_snapshot(symbol: str, price: float) -> str:
    try:
        rows = requests.get(
            KLINE_URL, params={"symbol": symbol, "interval": "15m", "limit": "80"}, timeout=8
        ).json().get("data") or []
        closes = [float(row["close"]) for row in rows]
        highs = [float(row["high"]) for row in rows]
        lows = [float(row["low"]) for row in rows]
    except (TypeError, ValueError, requests.RequestException):
        return ""
    if len(closes) < 30:
        return ""
    rsi_gain = rsi_loss = 0.0
    for older, newer in zip(closes[-15:-1], closes[-14:]):
        diff = newer - older
        rsi_gain += max(diff, 0)
        rsi_loss += max(-diff, 0)
    rsi = 100 if rsi_loss == 0 else 100 - 100 / (1 + rsi_gain / rsi_loss)
    macd = ema(closes, 12) - ema(closes, 26)
    mid = sum(closes[-20:]) / 20
    band = (sum((item - mid) ** 2 for item in closes[-20:]) / 20) ** 0.5
    above = min((item for item in highs[-40:] if item > price), default=0)
    below = max((item for item in lows[-40:] if item < price), default=0)
    return (
        f"15m RSI {rsi:.0f} · MACD {macd:+.4g}\n"
        f"EMA9 {ema(closes, 9):.6g} · EMA21 {ema(closes, 21):.6g}\n"
        f"Bollinger {mid - 2 * band:.6g} to {mid + 2 * band:.6g}\n"
        f"Nearby support {below:.6g} · resistance {above:.6g}"
    )


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


def check(symbol: str, price: float, now: float) -> None:
    rows = history[symbol]
    rows.append((now, price))
    old = next((item for item in rows if now - item[0] >= LOOKBACK_SECONDS), None)
    if not old:
        return
    old_time, old_price = old
    if old_price <= 0 or now - last_alert.get(symbol, 0) < COOLDOWN_SECONDS:
        return
    change = (price - old_price) / old_price * 100
    if abs(change) < THRESHOLD_PCT:
        return
    last_alert[symbol] = now
    elapsed = now - old_time
    side = "spike" if change > 0 else "drop"
    points, reasons, with_btc = context_score(symbol, side, window_seconds=elapsed)
    label = strength(change)
    if points >= 2 and not with_btc:
        label = f"{label}  HIGH"
    if change > 0:
        title = f"🟢🟢🟢 <b>SPIKE</b> 🟢🟢🟢  {label}"
    else:
        title = f"🔴🔴🔴 <b>DROP</b> 🔴🔴🔴  {label}"
    extra = "\n".join(f"• {item}" for item in reasons)
    if with_btc:
        extra = (extra + "\n" if extra else "") + "WITH BTC"
    msg = (
        f"{title}\n"
        f"<b>{symbol}</b>  <b>{change:+.2f}%</b> in {elapsed:.0f}s\n"
        f"{old_price:.6g} → {price:.6g}\n"
        + (extra + "\n" if extra else "")
        + f"Confidence checks: {points}\n"
        f"{chart_link(symbol)}"
    )
    note = ta_snapshot(symbol, price)
    if note:
        msg += "\n" + note
    log.warning("%s %s %.2f%%", label, symbol, change)
    if "HIGH" not in label or abs(change) < 4:
        return
    if change < 0:
        send_telegram(msg)
    pending[symbol] = {"side": "long" if change < 0 else "short", "price": price, "at": now, "change": change}


def follow_entries(now: float, prices: dict) -> None:
    for symbol, item in list(pending.items()):
        if now - item["at"] < ENTRY_WAIT_SECONDS:
            continue
        pending.pop(symbol, None)
        price = prices.get(symbol)
        if not price or item["price"] <= 0:
            continue
        move = (price - item["price"]) / item["price"] * 100
        if item["side"] == "long" and move > -0.3:
            log.info("Skip late long %s, only %+.2f%%", symbol, move)
            continue
        if item["side"] == "short" and move < 0.3:
            log.info("Skip late short %s, only %+.2f%%", symbol, move)
            continue
        note = ta_snapshot(symbol, price)
        extra = f"\n{note}" if note else ""
        if item["side"] == "long":
            send_telegram(
                f"🟢 <b>LATE LONG</b> {symbol}\n"
                f"Drop was {item['change']:+.1f}% at {item['price']:.6g}\n"
                f"3 minutes later, lower: <b>{price:.6g}</b> ({move:+.2f}%)\n"
                f"Not an order\n"
                f"{chart_link(symbol)}{extra}"
            )
        else:
            send_telegram(
                f"🔴 <b>LATE SHORT</b> {symbol}\n"
                f"Spike was {item['change']:+.1f}% at {item['price']:.6g}\n"
                f"3 minutes later, higher: <b>{price:.6g}</b> ({move:+.2f}%)\n"
                f"Not an order\n"
                f"{chart_link(symbol)}{extra}"
            )
        log.warning("LATE %s %s %+.2f%%", item["side"].upper(), symbol, move)


def main() -> None:
    log.info("Starting confirmed alert bot, cooldown %ss", COOLDOWN_SECONDS)
    while True:
        try:
            rows = requests.get(TICKERS_URL, timeout=20).json().get("data") or []
            now = time.time()
            prices = {}
            for row in rows:
                symbol = row.get("symbol")
                if not symbol or not allowed(symbol):
                    continue
                try:
                    price = float(row.get("lastPrice") or row.get("last") or 0)
                    volume = float(row.get("quoteVol") or 0)
                except (TypeError, ValueError):
                    continue
                if price > 0 and volume >= MIN_VOLUME_USDT:
                    prices[symbol] = price
                    if symbol == "BTCUSDT":
                        note_btc(price, now)
                    check(symbol, price, now)
            follow_entries(now, prices)
        except Exception as exc:
            log.error("Ticker fetch failed: %s", exc)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
