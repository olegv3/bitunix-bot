#!/usr/bin/env python3
"""Dedupes paper signals. A coin cannot be reopened for 4 hours."""

import logging
import time
from collections import defaultdict

log = logging.getLogger("bitunix-pipeline")
SIGNAL_DEDUP_SECONDS = 4 * 60 * 60
_last_signal = defaultdict(float)


def allow_alert(source: str, symbol: str, side: str) -> bool:
    key = symbol
    now = time.time()
    if now - _last_signal.get(key, 0) < SIGNAL_DEDUP_SECONDS:
        log.info("Deduped %s %s %s", source, symbol, side)
        return False
    _last_signal[key] = now
    return True
