"""Cross-bot alert dedup. Same coin and side inside the window counts once."""

import time

WINDOW_SECONDS = 90
_recent = {}


def allow_alert(source: str, symbol: str, side: str) -> bool:
    now = time.time()
    key = (symbol.upper(), side)
    previous = _recent.get(key)
    if previous and now - previous["ts"] < WINDOW_SECONDS:
        return False
    _recent[key] = {"ts": now, "source": source}
    stale = [item for item, row in _recent.items() if now - row["ts"] > 600]
    for item in stale:
        _recent.pop(item, None)
    return True
