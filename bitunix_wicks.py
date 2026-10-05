#!/usr/bin/env python3
"""
1-minute wick alerts for liquid Bitunix futures pairs.

Upper wick = rejection after a push up (possible short watch).
Lower wick = rejection after a push down (possible bounce watch).
This does not replace the confirmed spike/drop bot.

Fetches klines concurrently across symbols instead of one-at-a-time,
which cuts a full pass from minutes to seconds and avoids rate limits.
"""

import logging
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from market_context import score as context_score
from market_filters import allowed, chart_link, strength, ta_snapshot
from utils import send_telegram, strip_html

MIN_VOLUME_USDT = 200000
MIN_RANGE_PCT = 1.5          # ignore small candles
WICK_TO_BODY = 2.5           # wick must be at least 2.5x the body
WICK_SHARE = 0.60            # wick must be at least 60% of the whole candle
COOLDOWN_SECONDS = 60
MAX_WORKERS = 20             # concurrent kline requests
TICKERS_URL = "https://fapi.bitunix.com/api/v1/futures/market/tickers"
KLINE_URL = "https://fapi.bitunix.com/api/v1/futures/market/kline"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bitunix-wick")

last_alert = defaultdict(float)


def load_symbols() -> list:
    rows = requests.get(TICKERS_URL, timeout=20).json().get("data") or []
    symbols = []
    for row in rows:
        try:
            volume = float(row.get("quoteVol") or 0)
        except (TypeError, ValueError):
            volume = 0
        if row.get("symbol") and volume >= MIN_VOLUME_USDT and allowed(row["symbol"]):
            symbols.append(row["symbol"])
    return sorted(set(symbols))


def latest_candle(symbol: str):
    resp = requests.get(
        KLINE_URL,
        params={"symbol": symbol, "interval": "1m", "limit": 2},
        timeout=10,
    )
    rows = resp.json().get("data") or []
    return rows[-1] if rows else None


def check_wick(symbol: str, candle: dict, now: float) -> None:
    try:
        open_ = float(candle["open"])
        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])
    except (KeyError, TypeError, ValueError):
        return
    if min(open_, high, low, close) <= 0:
        return

    full_range = high - low
    range_pct = full_range / open_ * 100
    if range_pct < MIN_RANGE_PCT:
        return

    body = abs(close - open_)
    upper = high - max(open_, close)
    lower = min(open_, close) - low
    body_floor = max(body, full_range * 0.05)

    side = None
    wick_pct = 0.0
    if upper >= body_floor * WICK_TO_BODY and upper / full_range >= WICK_SHARE:
        side = "upper"
        wick_pct = upper / open_ * 100
    elif lower >= body_floor * WICK_TO_BODY and lower / full_range >= WICK_SHARE:
        side = "lower"
        wick_pct = lower / open_ * 100
    if not side or now - last_alert.get(symbol, 0) < COOLDOWN_SECONDS:
        return
    context_side = "drop" if side == "upper" else "spike"
    points, reasons, with_btc = context_score(symbol, context_side, window_seconds=60)
    if points < 1:
        return
    last_alert[symbol] = now
    extra = "\n".join(f"• {item}" for item in reasons)
    if with_btc:
        extra = (extra + "\n" if extra else "") + "WITH BTC"

    label = strength(range_pct)
    if side == "upper":
        msg = (
            f"🗓️ <b>UPPER WICK</b> {symbol}  {label}\n"
            f"Rejected high. Wick <b>{wick_pct:.2f}%</b>, candle range {range_pct:.2f}%\n"
            f"O {open_:.6g}  H {high:.6g}  L {low:.6g}  C {close:.6g}\n"
            f"{extra}\nConfidence checks: {points}\n"
            f"{chart_link(symbol)}"
        )
    else:
        msg = (
            f"🔺 <b>LOWER WICK</b> {symbol}  {label}\n"
            f"Rejected low. Wick <b>{wick_pct:.2f}%</b>, candle range {range_pct:.2f}%\n"
            f"O {open_:.6g}  H {high:.6g}  L {low:.6g}  C {close:.6g}\n"
            f"{extra}\nConfidence checks: {points}\n"
            f"{chart_link(symbol)}"
        )
    note = ta_snapshot(symbol, close)
    if note:
        msg += "\n" + note
    log.warning(strip_html(msg))
    send_telegram(msg)


def scan_pass(symbols: list) -> None:
    now = time.time()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(latest_candle, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                candle = future.result()
                if candle:
                    check_wick(symbol, candle, now)
            except Exception as exc:
                log.error("%s kline failed: %s", symbol, exc)


def main() -> None:
    symbols = load_symbols()
    log.info("Scanning %s pairs for 1m wicks with %s workers", len(symbols), MAX_WORKERS)
    send_telegram(
        f"Wick bot is running. Scanning {len(symbols)} pairs concurrently "
        f"with {MAX_WORKERS} workers."
    )
    while True:
        t0 = time.time()
        try:
            scan_pass(symbols)
        except Exception as exc:
            log.error("Wick pass failed: %s", exc)
        elapsed = time.time() - t0
        log.info("Finished a wick pass in %.1fs", elapsed)
        # Sleep just enough to land near the top of the next minute candle.
        sleep_for = max(1.0, 60 - (time.time() % 60))
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
