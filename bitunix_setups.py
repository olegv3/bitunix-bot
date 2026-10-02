#!/usr/bin/env python3
"""Failed-move setup alerts. These are not orders."""

import logging
import os
import time
from collections import defaultdict, deque

import requests

from market_filters import allowed, chart_link

POLL_INTERVAL = 10
WINDOW_SECONDS = 15 * 60
MOVE_PCT = 2.0
REJECT_PCT = 0.8
COOLDOWN_SECONDS = 600
MIN_VOLUME_USDT = 200000
TICKERS_URL = "https://fapi.bitunix.com/api/v1/futures/market/tickers"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bitunix-setup")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
history = defaultdict(lambda: deque(maxlen=200))
last_alert = {}


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
    window = [item for item in rows if now - item[0] <= WINDOW_SECONDS]
    if len(window) < 6 or now - last_alert.get(symbol, 0) < COOLDOWN_SECONDS:
        return

    high_i = max(range(len(window)), key=lambda i: window[i][1])
    low_i = min(range(len(window)), key=lambda i: window[i][1])
    high_time, high = window[high_i]
    low_time, low = window[low_i]
    if high <= 0 or low <= 0:
        return

    prior_low = min((item[1] for item in window[: high_i + 1]), default=None)
    prior_high = max((item[1] for item in window[: low_i + 1]), default=None)

    short_ready = (
        prior_low
        and high_time >= low_time
        and (high - prior_low) / prior_low * 100 >= MOVE_PCT
        and (high - price) / high * 100 >= REJECT_PCT
        and price < high
    )
    long_ready = (
        prior_high
        and low_time >= high_time
        and (prior_high - low) / prior_high * 100 >= MOVE_PCT
        and (price - low) / low * 100 >= REJECT_PCT
        and price > low
    )
    if not short_ready and not long_ready:
        return

    last_alert[symbol] = now
    if short_ready:
        msg = (
            f"🟠 <b>SHORT WATCH</b> {symbol}\n"
            f"Price: <b>{price:.6g}</b>\n"
            f"Invalid if it trades back above <b>{high:.6g}</b>\n"
            f"Reason: {((high - prior_low) / prior_low * 100):.1f}% spike failed\n"
            f"Not an order\n"
            f"{chart_link(symbol)}"
        )
    else:
        msg = (
            f"🟢 <b>LONG WATCH</b> {symbol}\n"
            f"Price: <b>{price:.6g}</b>\n"
            f"Invalid if it trades back below <b>{low:.6g}</b>\n"
            f"Reason: {((prior_high - low) / prior_high * 100):.1f}% drop failed\n"
            f"Not an order\n"
            f"{chart_link(symbol)}"
        )
    log.warning(msg.replace("<b>", "").replace("</b>", ""))
    send_telegram(msg)


def main() -> None:
    log.info("Starting setup alerts")
    while True:
        try:
            rows = requests.get(TICKERS_URL, timeout=20).json().get("data") or []
            now = time.time()
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
                    check(symbol, price, now)
        except Exception as exc:
            log.error("Ticker fetch failed: %s", exc)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
