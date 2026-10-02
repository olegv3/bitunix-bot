#!/usr/bin/env python3
"""Failed-move setup alerts, plus held/dead follow-ups. These are not orders."""

import logging
import os
import time
from collections import defaultdict, deque

import requests

from market_filters import allowed, chart_link

POLL_INTERVAL = 10
WINDOW_SECONDS = 15 * 60
FRESH_SECONDS = 3 * 60
MOVE_PCT = 2.0
REJECT_PCT = 0.8
TOUCH_PCT = 0.25
HELD_PCT = 1.0
MAX_SPREAD_PCT = 0.3
COOLDOWN_SECONDS = 600
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
history = defaultdict(lambda: deque(maxlen=200))
touches = defaultdict(int)
last_touch = {}
last_alert = {}
open_setups = {}
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
    log.warning(
        "SCORE %s %s held=%s dead=%s",
        result.upper(),
        symbol,
        scorecard["held"],
        scorecard["dead"],
    )


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
    msg = (
        f"<b>{title}</b> {symbol}\n"
        f"{'Invalidation hit' if dead else f'Moved {HELD_PCT:.0f}% before invalidation'}\n"
        f"Price: {price:.6g}\n"
        f"Scorecard: held {scorecard['held']} / dead {scorecard['dead']}\n"
        f"{chart_link(symbol)}"
    )
    send_telegram(msg)
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


def check(symbol: str, price: float, now: float) -> None:
    follow_up(symbol, price)
    rows = history[symbol]
    rows.append((now, price))
    window = [item for item in rows if now - item[0] <= WINDOW_SECONDS]
    if len(window) < 8 or symbol in open_setups:
        return
    if now - last_alert.get(symbol, 0) < COOLDOWN_SECONDS:
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
        and now - high_time <= FRESH_SECONDS
        and (high - prior_low) / prior_low * 100 >= MOVE_PCT
        and (high - price) / high * 100 >= REJECT_PCT
    )
    long_ready = (
        prior_high
        and now - low_time <= FRESH_SECONDS
        and (prior_high - low) / prior_high * 100 >= MOVE_PCT
        and (price - low) / low * 100 >= REJECT_PCT
    )
    if short_ready:
        note_touch(symbol, high, price, now)
    elif long_ready:
        note_touch(symbol, low, price, now)
    else:
        return
    if touches[symbol] < 2 or not spread_ok(symbol):
        return

    last_alert[symbol] = now
    touches[symbol] = 0
    if short_ready:
        open_setups[symbol] = {"side": "short", "entry": price, "invalid": high}
        msg = (
            f"🟠 <b>SHORT WATCH</b> {symbol}\n"
            f"Price: <b>{price:.6g}</b>\n"
            f"Invalid if it trades back above <b>{high:.6g}</b>\n"
            f"Reason: second rejection after a {((high - prior_low) / prior_low * 100):.1f}% spike\n"
            f"Not an order\n"
            f"{chart_link(symbol)}"
        )
    else:
        open_setups[symbol] = {"side": "long", "entry": price, "invalid": low}
        msg = (
            f"🟢 <b>LONG WATCH</b> {symbol}\n"
            f"Price: <b>{price:.6g}</b>\n"
            f"Invalid if it trades back below <b>{low:.6g}</b>\n"
            f"Reason: second hold after a {((prior_high - low) / prior_high * 100):.1f}% drop\n"
            f"Not an order\n"
            f"{chart_link(symbol)}"
        )
    log.warning(msg.replace("<b>", "").replace("</b>", ""))
    send_telegram(msg)


def main() -> None:
    log.info("Starting setup alerts")
    send_telegram("Setup bot is running. Watching for failed spikes and drops.")
    last_beat = time.time()
    while True:
        try:
            rows = requests.get(TICKERS_URL, timeout=20).json().get("data") or []
            now = time.time()
            if now - last_beat >= 1800:
                log.info(
                    "Setup heartbeat open=%s held=%s dead=%s",
                    len(open_setups),
                    scorecard["held"],
                    scorecard["dead"],
                )
                send_telegram(
                    f"Setup bot alive. Open {len(open_setups)}. "
                    f"Held {scorecard['held']} / dead {scorecard['dead']}."
                )
                last_beat = now
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
