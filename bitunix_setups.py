#!/usr/bin/env python3
"""Failed-move setup alerts. First rejection is enough. Not an order."""

import logging
import os
import time
from collections import defaultdict, deque

import requests

from market_filters import allowed, chart_link, ta_snapshot

POLL_INTERVAL = 10
WINDOW_SECONDS = 30 * 60
FRESH_SECONDS = 5 * 60
MOVE_PCT = 5.0
BTC_MOVE_PCT = 2.0
REJECT_PCT = 0.8
TOUCH_PCT = 0.25
HELD_PCT = 1.0
MAX_SPREAD_PCT = 0.3
COOLDOWN_SECONDS = 1800
MIN_VOLUME_USDT = 200000
TICKERS_URL = "https://fapi.bitunix.com/api/v1/futures/market/tickers"
DEPTH_URL = "https://fapi.bitunix.com/api/v1/futures/market/depth"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bitunix-setup")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
history = defaultdict(lambda: deque(maxlen=400))
touches = defaultdict(int)
last_touch = defaultdict(float)
last_alert = defaultdict(float)
last_candidate = defaultdict(float)
open_setups = defaultdict(dict)
scorecard = {"held": 0, "dead": 0}


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


def spread_ok(symbol: str) -> bool:
    try:
        book = requests.get(
            DEPTH_URL, params={"symbol": symbol, "limit": "1"}, timeout=8
        ).json().get("data") or {}
        ask = float(book["asks"][0][0])
        bid = float(book["bids"][0][0])
    except (KeyError, IndexError, TypeError, ValueError, requests.RequestException):
        return False
    if bid <= 0 or ask < bid:
        return False
    return (ask - bid) / bid * 100 <= MAX_SPREAD_PCT


def record(symbol: str, result: str) -> None:
    scorecard[result] += 1
    log.warning("SCORE %s %s held=%s dead=%s", result.upper(), symbol, scorecard["held"], scorecard["dead"])


def follow_up(symbol: str, price: float) -> None:
    setup = open_setups.get(symbol)
    if not setup:
        return
    entry = setup["entry"]
    invalid = setup["invalid"]
    if setup["side"] == "short":
        dead = price > invalid
        held = price <= entry * (1 - HELD_PCT / 100)
    else:
        dead = price < invalid
        held = price >= entry * (1 + HELD_PCT / 100)
    if not dead and not held:
        return
    result = "dead" if dead else "held"
    record(symbol, result)
    title = "SETUP DEAD" if dead else "SETUP HELD"
    send_telegram(
        f"<b>{title}</b> {symbol}\n"
        f"{'Invalidation hit' if dead else f'Moved {HELD_PCT:.0f}% before invalidation'}\n"
        f"Price: {price:.6g}\n"
        f"Scorecard: held {scorecard['held']} / dead {scorecard['dead']}\n"
        f"{chart_link(symbol)}"
    )
    open_setups.pop(symbol, None)


def note_touch(symbol: str, extreme: float, price: float, now: float) -> None:
    near = abs(price - extreme) / extreme * 100 <= TOUCH_PCT
    if near:
        last_touch[symbol] = now
        return
    touched = last_touch.get(symbol)
    if touched and abs(price - extreme) / extreme * 100 >= REJECT_PCT / 2:
        touches[symbol] += 1
        last_touch.pop(symbol, None)


def move_needed(symbol: str) -> float:
    return BTC_MOVE_PCT if symbol == "BTCUSDT" else MOVE_PCT


def check(symbol: str, price: float, now: float, day_high: float, day_low: float, day_open: float) -> None:
    follow_up(symbol, price)
    rows = history[symbol]
    rows.append((now, price))
    window = [item for item in rows if now - item[0] <= WINDOW_SECONDS]
    if symbol in open_setups:
        return
    if len(window) < 2:
        window = [(now, price)]
    high_i = max(range(len(window)), key=lambda i: window[i][1])
    low_i = min(range(len(window)), key=lambda i: window[i][1])
    high_time, high = window[high_i]
    low_time, low = window[low_i]
    if high <= 0 or low <= 0:
        return

    needed = move_needed(symbol)
    day_change = (price - day_open) / day_open * 100 if day_open else 0
    near_high = day_high and (day_high - price) / day_high * 100 <= 1.5
    near_low = day_low and (price - day_low) / day_low * 100 <= 1.5

    if now - last_candidate.get(symbol, 0) >= COOLDOWN_SECONDS:
        if near_high and day_change >= 8:
            last_candidate[symbol] = now
            send_telegram(
                f"\U0001f7e0 <b>WATCHING SHORT</b> {symbol}\n"
                f"Up <b>{day_change:.1f}%</b> today, price {price:.6g}\n"
                f"Day high {day_high:.6g}. Still near the high\n"
                f"Not an order\n"
                f"{chart_link(symbol)}\n"
                f"{ta_snapshot(symbol, price)}"
            )
        elif near_low and day_change <= -8:
            last_candidate[symbol] = now
            send_telegram(
                f"\U0001f7e2 <b>WATCHING LONG</b> {symbol}\n"
                f"Down <b>{abs(day_change):.1f}%</b> today, price {price:.6g}\n"
                f"Day low {day_low:.6g}. Still near the low\n"
                f"Not an order\n"
                f"{chart_link(symbol)}\n"
                f"{ta_snapshot(symbol, price)}"
            )

    if now - last_alert.get(symbol, 0) < COOLDOWN_SECONDS:
        return
    prior_low = min((item[1] for item in window[: high_i + 1]), default=None)
    prior_high = max((item[1] for item in window[: low_i + 1]), default=None)
    short_ready = (
        prior_low
        and now - high_time <= FRESH_SECONDS
        and (high - prior_low) / prior_low * 100 >= needed
        and (high - price) / high * 100 >= REJECT_PCT
    )
    long_ready = (
        prior_high
        and now - low_time <= FRESH_SECONDS
        and (prior_high - low) / prior_high * 100 >= needed
        and (price - low) / low * 100 >= REJECT_PCT
    )
    if short_ready:
        note_touch(symbol, high, price, now)
    elif long_ready:
        note_touch(symbol, low, price, now)
    else:
        return
    if touches[symbol] < 1 or not spread_ok(symbol):
        return

    last_alert[symbol] = now
    touches[symbol] = 0
    if short_ready:
        open_setups[symbol] = {"side": "short", "entry": price, "invalid": high}
        msg = (
            f"\U0001f7e0 <b>SHORT WATCH</b> {symbol}\n"
            f"Price: <b>{price:.6g}</b>\n"
            f"Invalid if it trades back above <b>{high:.6g}</b>\n"
            f"Reason: first rejection after a {((high - prior_low) / prior_low * 100):.1f}% spike\n"
            f"Not an order\n"
            f"{chart_link(symbol)}"
        )
    else:
        open_setups[symbol] = {"side": "long", "entry": price, "invalid": low}
        msg = (
            f"\U0001f7e2 <b>LONG WATCH</b> {symbol}\n"
            f"Price: <b>{price:.6g}</b>\n"
            f"Invalid if it trades back below <b>{low:.6g}</b>\n"
            f"Reason: first hold after a {((prior_high - low) / prior_high * 100):.1f}% drop\n"
            f"Not an order\n"
            f"{chart_link(symbol)}"
        )
    log.warning(msg.replace("<b>", "").replace("</b>", ""))
    send_telegram(msg)


def main() -> None:
    log.info("Starting setup alerts")
    send_telegram(
        "Setup bot loosened. An 8% day move can watch, and the first rejection can open a setup. Not an order."
    )
    last_beat = time.time()
    while True:
        try:
            rows = requests.get(TICKERS_URL, timeout=20).json().get("data") or []
            now = time.time()
            if now - last_beat >= 1800:
                log.info("Setup heartbeat open=%s held=%s dead=%s", len(open_setups), scorecard["held"], scorecard["dead"])
                last_beat = now
            for row in rows:
                symbol = row.get("symbol")
                if not symbol or not allowed(symbol):
                    continue
                try:
                    price = float(row.get("lastPrice") or row.get("last") or 0)
                    volume = float(row.get("quoteVol") or 0)
                    day_high = float(row.get("high") or 0)
                    day_low = float(row.get("low") or 0)
                    day_open = float(row.get("open") or 0)
                except (TypeError, ValueError):
                    continue
                if price > 0 and volume >= MIN_VOLUME_USDT:
                    check(symbol, price, now, day_high, day_low, day_open)
        except Exception as exc:
            log.error("Ticker fetch failed: %s", exc)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
