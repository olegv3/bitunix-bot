#!/usr/bin/env python3
"""
Early-watch bot for all liquid Bitunix futures pairs.

Bitunix allows 300 channel subscriptions per connection. This script uses
trade + depth (2 channels per pair), so it splits pairs across connections.
"""

import json
import logging
import os
import threading
import time
from collections import defaultdict, deque

import requests
from websocket import WebSocketApp

# ====================== CONFIG ======================
MIN_VOLUME_USDT = 200000      # skip dead pairs
WINDOW_SECONDS = 8
MIN_NOTIONAL = 8000           # burst size required before a WATCH alert
IMBALANCE = 0.72
BOOK_IMBALANCE = 0.65
COOLDOWN_SECONDS = 60
DEPTH_CHANNEL = "depth_book5"
PAIRS_PER_CONNECTION = 120    # 120 x 2 channels = 240, under the 300 cap
WS_URL = "wss://fapi.bitunix.com/public/"
TICKERS_URL = "https://fapi.bitunix.com/api/v1/futures/market/tickers"
# ====================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bitunix-watch")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

trades = defaultdict(lambda: deque(maxlen=400))
books = {}
last_alert = {}
lock = threading.Lock()


def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram env vars missing")
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


def load_symbols() -> list:
    resp = requests.get(TICKERS_URL, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    rows = payload.get("data") or payload.get("result") or []
    symbols = []
    for row in rows:
        symbol = row.get("symbol") or row.get("s")
        volume = row.get("quoteVol") or row.get("q") or row.get("amount") or 0
        try:
            volume = float(volume)
        except (TypeError, ValueError):
            volume = 0
        if symbol and volume >= MIN_VOLUME_USDT:
            symbols.append(symbol)
    symbols = sorted(set(symbols))
    log.info("Watching %s pairs with 24h volume >= %s", len(symbols), MIN_VOLUME_USDT)
    return symbols


def book_ratio(symbol: str):
    book = books.get(symbol) or {}
    bid_notional = ask_notional = 0.0
    for level in (book.get("b") or [])[:5]:
        try:
            bid_notional += float(level[0]) * float(level[1])
        except (TypeError, ValueError, IndexError):
            continue
    for level in (book.get("a") or [])[:5]:
        try:
            ask_notional += float(level[0]) * float(level[1])
        except (TypeError, ValueError, IndexError):
            continue
    total = bid_notional + ask_notional
    if total <= 0:
        return None, 0.0, 0.0
    return bid_notional / total, bid_notional, ask_notional


def check_symbol(symbol: str, now: float) -> None:
    with lock:
        recent = [row for row in trades[symbol] if now - row[0] <= WINDOW_SECONDS]
    if len(recent) < 4:
        return
    buy = sum(n for _, side, n in recent if side == "buy")
    sell = sum(n for _, side, n in recent if side == "sell")
    total = buy + sell
    if total < MIN_NOTIONAL:
        return

    buy_share = buy / total
    sell_share = sell / total
    bid_share, bid_notional, ask_notional = book_ratio(symbol)

    side = None
    if buy_share >= IMBALANCE and (bid_share is None or bid_share >= BOOK_IMBALANCE):
        side = "spike"
    elif sell_share >= IMBALANCE and (bid_share is None or bid_share <= 1 - BOOK_IMBALANCE):
        side = "drop"
    if not side or now - last_alert.get(symbol, 0) < COOLDOWN_SECONDS:
        return
    last_alert[symbol] = now

    if side == "spike":
        msg = (
            f"🟡 <b>WATCH SPIKE</b> {symbol}\n"
            f"Buy flow: <b>{buy_share:.0%}</b> of ${total:,.0f} in {WINDOW_SECONDS}s\n"
            + (
                f"Top book bids: <b>{bid_share:.0%}</b>\n"
                if bid_share is not None
                else ""
            )
            + "Early guess, not a confirmed move"
        )
    else:
        ask_share = None if bid_share is None else 1 - bid_share
        msg = (
            f"🟠 <b>WATCH DROP</b> {symbol}\n"
            f"Sell flow: <b>{sell_share:.0%}</b> of ${total:,.0f} in {WINDOW_SECONDS}s\n"
            + (
                f"Top book asks: <b>{ask_share:.0%}</b>\n"
                if ask_share is not None
                else ""
            )
            + "Early guess, not a confirmed move"
        )
    log.warning(msg.replace("<b>", "").replace("</b>", ""))
    send_telegram(msg)


def on_message(_ws, raw: str) -> None:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return
    ch = msg.get("ch")
    symbol = msg.get("symbol")
    data = msg.get("data")
    now = time.time()
    if ch == "trade" and symbol and isinstance(data, list):
        with lock:
            for trade in data:
                try:
                    price = float(trade.get("p"))
                    qty = float(trade.get("v"))
                    side = str(trade.get("s", "")).lower()
                except (TypeError, ValueError):
                    continue
                if side in ("buy", "sell") and price > 0 and qty > 0:
                    trades[symbol].append((now, side, price * qty))
        check_symbol(symbol, now)
    elif ch == DEPTH_CHANNEL and symbol and isinstance(data, dict):
        books[symbol] = data


def run_connection(symbols: list, index: int) -> None:
    def on_open(ws) -> None:
        args = []
        for symbol in symbols:
            args.append({"symbol": symbol, "ch": "trade"})
            args.append({"symbol": symbol, "ch": DEPTH_CHANNEL})
        # Bitunix allows 5 messages/second. Send in chunks.
        for i in range(0, len(args), 40):
            ws.send(json.dumps({"op": "subscribe", "args": args[i:i + 40]}))
            time.sleep(0.3)
        log.info("Connection %s subscribed to %s pairs", index, len(symbols))

    while True:
        ws = WebSocketApp(
            WS_URL,
            on_open=on_open,
            on_message=on_message,
            on_error=lambda _ws, err: log.error("WS %s error: %s", index, err),
            on_close=lambda _ws, status, reason: log.warning(
                "WS %s closed: %s %s", index, status, reason
            ),
        )
        ws.run_forever(ping_interval=20, ping_timeout=10)
        time.sleep(5)


def main() -> None:
    symbols = load_symbols()
    if not symbols:
        raise SystemExit("No symbols returned. Check the tickers endpoint.")
    chunks = [
        symbols[i:i + PAIRS_PER_CONNECTION]
        for i in range(0, len(symbols), PAIRS_PER_CONNECTION)
    ]
    log.info("Opening %s websocket connections", len(chunks))
    threads = []
    for index, chunk in enumerate(chunks, start=1):
        thread = threading.Thread(target=run_connection, args=(chunk, index), daemon=True)
        thread.start()
        threads.append(thread)
        time.sleep(1)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
