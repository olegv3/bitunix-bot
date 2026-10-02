"""Shared context for Bitunix alerts. No secrets."""

import time

import requests

TICKERS_URL = "https://fapi.bitunix.com/api/v1/futures/market/tickers"
FUNDING_URL = "https://fapi.bitunix.com/api/v1/futures/market/funding_rate/batch"

_cache = {"tickers": {}, "funding": {}, "at": 0.0}


def refresh(max_age: float = 60) -> None:
    now = time.time()
    if now - _cache["at"] < max_age and _cache["tickers"]:
        return
    try:
        rows = requests.get(TICKERS_URL, timeout=20).json().get("data") or []
        _cache["tickers"] = {row.get("symbol"): row for row in rows if row.get("symbol")}
    except Exception:
        pass
    try:
        rows = requests.get(FUNDING_URL, timeout=20).json().get("data") or []
        _cache["funding"] = {row.get("symbol"): row for row in rows if row.get("symbol")}
    except Exception:
        pass
    _cache["at"] = now


def score(symbol: str, side: str, burst_notional: float = 0.0) -> tuple:
    """side is spike or drop. Returns (score, reasons)."""
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
        expected = volume / 86400 * 8
        if expected > 0 and burst_notional >= expected * 5:
            reasons.append("volume burst is 5x normal")

    return len(reasons), reasons
