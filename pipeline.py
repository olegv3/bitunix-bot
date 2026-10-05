#!/usr/bin/env python3
"""Unified alert pipeline for all Bitunix bots.

Every bot sends raw signals here. The pipeline assigns one confidence score,
dedupes overlapping alerts for the same symbol, and fires a single Telegram
message instead of four bots spamming the same coin.
"""

import logging
import time
from collections import defaultdict

from market_context import score as context_score
from utils import send_telegram, strip_html

log = logging.getLogger("bitunix-pipeline")

# How long to suppress duplicate alerts for the same symbol (seconds).
DEDUP_SECONDS = 300

# Per-symbol cooldown tracking: symbol -> timestamp of last emitted alert.
_last_emitted = defaultdict(float)

# Symbol -> side of the most recent emitted alert, for flip detection.
_last_side = defaultdict(str)


def submit(symbol: str, side: str, source: str, price: float = 0.0,
           extra_reasons: list | None = None, burst_notional: float = 0.0,
           window_seconds: float = 15, chart: str = "", ta: str = "") -> bool:
    """Submit a raw signal from any bot.

    Returns True if an alert was actually sent, False if deduped or filtered.
    """
    now = time.time()
    if now - _last_emitted.get(symbol, 0) < DEDUP_SECONDS:
        # Allow a flip (spike -> drop or vice versa) through even inside the window.
        if _last_side.get(symbol) == side:
            log.info("Deduped %s %s from %s", side, symbol, source)
            return False

    points, reasons, with_btc = context_score(
        symbol, side, burst_notional=burst_notional, window_seconds=window_seconds
    )
    if extra_reasons:
        reasons = list(extra_reasons) + reasons
        points += len(extra_reasons)

    # Minimum bar: at least one context point, and not just riding BTC.
    if points < 1 or with_btc:
        log.info("Filtered %s %s from %s: points=%s with_btc=%s", side, symbol, source, points, with_btc)
        return False

    _last_emitted[symbol] = now
    _last_side[symbol] = side

    side_label = side.upper()
    emoji = "🟢" if side == "spike" else "🔴"
    extra = "\n".join(f"• {item}" for item in reasons)
    msg = (
        f"{emoji} <b>{side_label}</b> {symbol} via {source}\n"
        + (f"Price: <b>{price:.6g}</b>\n" if price > 0 else "")
        + (extra + "\n" if extra else "")
        + f"Confidence checks: {points}\n"
        + (f"{chart}\n" if chart else "")
        + (ta if ta else "")
    ).rstrip()

    log.warning(strip_html(msg))
    send_telegram(msg)
    return True


def reset(symbol: str | None = None) -> None:
    """Clear dedup state. Useful for tests."""
    if symbol:
        _last_emitted.pop(symbol, None)
        _last_side.pop(symbol, None)
    else:
        _last_emitted.clear()
        _last_side.clear()
