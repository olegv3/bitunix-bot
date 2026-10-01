#!/usr/bin/env python3
"""
Bitunix Futures Sudden Price Move Alert Bot
Monitors ALL futures pairs for sudden spikes/drops.
"""

import time
import requests
import logging
import os
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Dict, Deque, Tuple

# ====================== CONFIG ======================
POLL_INTERVAL = 5          # seconds between full ticker fetches
LOOKBACK_SECONDS = 10      # how far back to measure the move
THRESHOLD_PCT = 1.0        # alert if |change| >= this % within LOOKBACK
MIN_VOLUME_USDT = 50000    # ignore low-volume pairs
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# ====================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bitunix-alert")

BASE_URL = "https://fapi.bitunix.com"

price_history: Dict[str, Deque[Tuple[float, float]]] = defaultdict(
    lambda: deque(maxlen=200)
)

def get_all_tickers() -> list:
    url = f"{BASE_URL}/api/v1/futures/market/tickers"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            log.error("API error: %s", data.get("msg"))
            return []
        return data.get("data", [])
    except Exception as e:
        log.error("Failed to fetch tickers: %s", e)
        return []

def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
    except Exception as e:
        log.error("Telegram send failed: %s", e)

def check_and_alert(symbol: str, price: float, now: float):
    history = price_history[symbol]
    history.append((now, price))

    cutoff = now - LOOKBACK_SECONDS
    while history and history[0][0] < cutoff:
        history.popleft()

    if len(history) < 2:
        return

    old_ts, old_price = history[0]
    if old_price <= 0:
        return

    change_pct = ((price - old_price) / old_price) * 100
    elapsed = now - old_ts

    if abs(change_pct) >= THRESHOLD_PCT:
        direction = "🚀 SPIKE" if change_pct > 0 else "📉 DROP"
        msg = (
            f"<b>{direction}</b> on <b>{symbol}</b>\n"
            f"Change: <b>{change_pct:+.2f}%</b> in {elapsed:.0f}s\n"
            f"From {old_price:.6g} → {price:.6g}\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        log.warning("%s %+.2f%% in %.0fs  %s → %s",
                    symbol, change_pct, elapsed, old_price, price)
        print(msg.replace("<b>", "").replace("</b>", ""))
        send_telegram(msg)

def main():
    log.info("Starting Bitunix Futures alert bot")
    log.info("Threshold: ±%.1f%% within %ds | Poll every %ds",
             THRESHOLD_PCT, LOOKBACK_SECONDS, POLL_INTERVAL)

    while True:
        start = time.time()
        tickers = get_all_tickers()
        now = time.time()

        for t in tickers:
            symbol = t.get("symbol")
            last = t.get("lastPrice") or t.get("last")
            vol = float(t.get("quoteVol") or 0)

            if not symbol or not last:
                continue
            if vol < MIN_VOLUME_USDT:
                continue

            try:
                price = float(last)
                check_and_alert(symbol, price, now)
            except (ValueError, TypeError):
                continue

        elapsed = time.time() - start
        sleep_time = max(0.5, POLL_INTERVAL - elapsed)
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()
