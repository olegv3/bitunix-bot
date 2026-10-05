"""Late-entry threshold learned from resolved paper trades. Falls back to 0.30%."""

import json
import os
from pathlib import Path

DATA_DIR = Path(os.getenv("BOT_DATA_DIR", "data"))
PATH = DATA_DIR / "thresholds.json"
DEFAULT_LATE_PCT = 0.30


def late_move_pct() -> float:
    try:
        payload = json.loads(PATH.read_text(encoding="utf-8"))
        value = float(payload.get("late_move_pct", DEFAULT_LATE_PCT))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return DEFAULT_LATE_PCT
    return min(max(value, 0.15), 1.5)


def update_from_outcomes(rows: list) -> None:
    """Use the median favorable move of timed-out trades as the next late-entry bar."""
    moves = [abs(float(row.get("favorable_pct") or 0)) for row in rows if row.get("result") == "timeout"]
    if len(moves) < 8:
        return
    moves.sort()
    median = moves[len(moves) // 2]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps({"late_move_pct": round(median, 2), "samples": len(moves)}), encoding="utf-8")
