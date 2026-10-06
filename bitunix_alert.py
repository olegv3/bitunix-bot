#!/usr/bin/env python3
"""Confirmed Bitunix futures spike/drop alerts."""

import logging
import os
import time
from collections import defaultdict, deque

import requests

from bot_common import emit_signal, file_logger
from market_context import note_btc, score as context_score
from market_filters import allowed, chart_link, strength, ta_snapshot
from thresholds import late_move_pct

POLL_INTERVAL = 5
LOOKBACK_SECONDS = 10
THRESHOLD_PCT = 2.0
MIN_VOLUME_USDT = 200000
COOLDOWN_SECONDS = 1800
TICKERS_URL = "https://fapi.bitunix.com/api/v1/futures/market/tickers"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bitunix-alert")
file_logger("bitunix-alert")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
history = defaultdict(lambda: deque(maxlen=200))
last_alert = {}
pending = {}
ENTRY_WAIT_SECONDS = 180


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
        title = "\U0001f7e2\U0001f7e2\U0001f7e2 <b>SPIKE</b> \U0001f7e2\U0001f7e2\U0001f7e2  " + label
    else:
        title = "\U0001f534\U0001f534\U0001f534 <b>DROP</b> \U0001f534\U0001f534\U0001f534  " + label
    extra = "\n".join(f"\u2022 {item}" for item in reasons)
    if with_btc:
        extra = (extra + "\n" if extra else "") + "WITH BTC"
    msg = (
        f"{title}\n"
        f"<b>{symbol}</b>  <b>{change:+.2f}%</b> in {elapsed:.0f}s\n"
        f"{old_price:.6g} \u2192 {price:.6g}\n"
        + (extra + "\n" if extra else "")
        + f"Confidence checks: {points}\n"
        f"{chart_link(symbol)}"
    )
    note = ta_snapshot(symbol, price)
    if note:
        msg += "\n" + note
    log.warning("%s %s %.2f%%", label, symbol, change)
    if change >= 0 or "HIGH" not in label or change > -4 or with_btc:
        return
    send_telegram(msg)
    pending[symbol] = {"price": price, "at": now, "change": change}


def follow_entries(now: float, prices: dict) -> None:
    bar = late_move_pct()
    for symbol, item in list(pending.items()):
        if now - item["at"] < ENTRY_WAIT_SECONDS:
            continue
        pending.pop(symbol, None)
        price = prices.get(symbol)
        if not price or item["price"] <= 0:
            continue
        move = (price - item["price"]) / item["price"] * 100
        if move > -bar:
            log.info("Skip late long %s, only %+.2f%% (bar %.2f)", symbol, move, bar)
            continue
        if not emit_signal("late", symbol, "long", price, 1.2, 2.0, f"after {item['change']:+.1f}%"):
            continue
        note = ta_snapshot(symbol, price)
        extra = f"\n{note}" if note else ""
        send_telegram(
            f"\U0001f7e2 <b>LATE LONG</b> {symbol}\n"
            f"Drop was {item['change']:+.1f}% at {item['price']:.6g}\n"
            f"3 minutes later, lower: <b>{price:.6g}</b> ({move:+.2f}%)\n"
            f"Not moving with BTC. Not an order\n"
            f"{chart_link(symbol)}{extra}"
        )
        send_telegram(
            f"\U0001f4c4 <b>PAPER OPEN</b> {symbol}\n"
            f"Long from <b>{price:.6g}</b> with $1\n"
            f"Adds at $1, $3, then $5 if it keeps falling. Stop at a $50 loss.\n"
            f"Not a live order."
        )
        log.warning("LATE LONG %s %+.2f%%", symbol, move)


def main() -> None:
    log.info("Starting confirmed alert bot, cooldown %ss", COOLDOWN_SECONDS)
    send_telegram("Alert bot is running. A late long now sends a paper-open message.")
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
