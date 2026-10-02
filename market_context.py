"""Shared context for Bitunix alerts. No secrets."""

import time
from collections import deque

import requests

TICKERS_URL = "https://fapi.bitunix.com/api/v1/futures/market/tickers"
FUNDING_URL = "https://fapi.bitunix.com/api/v1/futures/market/funding_rate/batch"
OI_URL = "https://fapi.binance.com/futures/data/openInterestHist"

_cache = {"tickers": {}, "funding": {}, "at": 0.0}
_btc = deque(maxlen=600)
_oi = {}


def note_btc(price: float, now: float) -> None:
    if price <= 0:
        return
    if _btc and now - _btc[-1][0] < 1:
        _btc[-1] = (now, price)
        return
    _btc.append((now, price))


def btc_move(now: float, seconds: float) -> float:
    if len(_btc) < 2:
        return 0.0
    old = None
    for ts, price in _btc:
        if now - ts >= seconds:
            old = price
        else:
            break
    if old is None:
        if now - _btc[0][0] < max(5, seconds / 2):
            return 0.0
        old = _btc[0][1]
    last = _btc[-1][1]
    if old <= 0:
        return 0.0
    return (last - old) / old * 100


def refresh(max_age: float = 30) -> None:
    now = time.time()
    if now - _cache["at"] < max_age and _cache["tickers"]:
        return
    try:
        rows = requests.get(TICKERS_URL, timeout=20).json().get("data") or []
        _cache["tickers"] = {row.get("symbol"): row for row in rows if row.get("symbol")}
        btc = _cache["tickers"].get("BTCUSDT") or {}
        price = float(btc.get("lastPrice") or btc.get("last") or 0)
        if price > 0:
            note_btc(price, now)
    except Exception:
        pass
    try:
        rows = requests.get(FUNDING_URL, timeout=20).json().get("data") or []
        _cache["funding"] = {row.get("symbol"): row for row in rows if row.get("symbol")}
    except Exception:
        pass
    _cache["at"] = now


def oi_change(symbol: str):
    """Binance 5-minute open-interest change. None if that pair is not listed there."""
    cached = _oi.get(symbol)
    now = time.time()
    if cached and now - cached[0] < 120:
        return cached[1]
    change = None
    try:
        resp = requests.get(
            OI_URL,
            params={"symbol": symbol, "period": "5m", "limit": 2},
            timeout=8,
        )
        if resp.status_code == 200:
            rows = resp.json()
            if isinstance(rows, list) and len(rows) >= 2:
                older = float(rows[0]["sumOpenInterest"])
                newer = float(rows[-1]["sumOpenInterest"])
                if older > 0:
                    change = (newer - older) / older
    except Exception:
        change = None
    _oi[symbol] = (now, change)
    return change


def score(symbol: str, side: str, burst_notional: float = 0.0, window_seconds: float = 15) -> tuple:
    """Returns (points, reasons, with_btc). WITH BTC is not a confidence point."""
    refresh()
    ticker = _cache["tickers"].get(symbol) or {}
    funding = _cache["funding"].get(symbol) or {}
    reasons = []

    try:
        last = float(ticker.get("lastPrice") or ticker.get("last") or 0)
        mark = float(ticker.get("markPrice") or funding.get("markPrice") or 0)
        high = float(ticker.get("high") or 0)
        low = float(ticker.get("low") or 0)
        volume = float(ticker.get("quoteVol") or 0)
    except (TypeError, ValueError):
        last = mark = high = low = volume = 0

    if last > 0 and mark > 0:
        stretch = (last - mark) / mark * 100
        if side == "spike" and stretch >= 0.15:
            reasons.append(f"last is {stretch:.2f}% above mark")
        elif side == "drop" and stretch <= -0.15:
            reasons.append(f"last is {abs(stretch):.2f}% below mark")

    if high > low and last > 0:
        pos = (last - low) / (high - low)
        if side == "spike" and pos >= 0.85:
            reasons.append("near 24h high")
        elif side == "drop" and pos <= 0.15:
            reasons.append("near 24h low")

    try:
        rate = float(funding.get("fundingRate") or 0)
    except (TypeError, ValueError):
        rate = 0
    if side == "drop" and rate >= 0.0003:
        reasons.append(f"crowded longs, funding {rate * 100:.3f}%")
    elif side == "spike" and rate <= -0.0003:
        reasons.append(f"crowded shorts, funding {rate * 100:.3f}%")

    if burst_notional > 0 and volume > 0:
        expected = volume / 86400 * max(window_seconds, 1)
        if expected > 0 and burst_notional >= expected * 5:
            reasons.append("volume burst is 5x normal")

    if symbol != "BTCUSDT":
        change = oi_change(symbol)
        if change is not None and change >= 0.005:
            reasons.append(f"Binance OI up {change * 100:.1f}% over 5m")

    btc = 0.0 if symbol == "BTCUSDT" else btc_move(time.time(), window_seconds)
    with_btc = (side == "spike" and btc >= 0.4) or (side == "drop" and btc <= -0.4)
    return len(reasons), reasons, with_btc
