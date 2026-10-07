"""Shared Telegram, file logging, and signal handoff. No secrets."""

import json
import logging
import os
import time
from pathlib import Path

import requests

DATA_DIR = Path(os.getenv("BOT_DATA_DIR", "data"))
SIGNALS_PATH = DATA_DIR / "signals.jsonl"
LOG_PATH = DATA_DIR / "bot.log"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def ensure_data() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def file_logger(name: str) -> logging.Logger:
    ensure_data()
    logger = logging.getLogger(name)
    if any(isinstance(h, logging.FileHandler) for h in logger.handlers):
        return logger
    handler = logging.FileHandler(LOG_PATH)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s %(message)s"))
    logger.addHandler(handler)
    return logger


def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(text)
        return True
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        return response.ok
    except Exception as exc:
        logging.getLogger("bitunix-common").error("Telegram send failed: %s", exc)
        return False


def emit_signal(source: str, symbol: str, side: str, entry: float, stop_pct: float, target_pct: float, note: str = "") -> bool:
    """Paper-track a late entry or an opened setup. Other alerts still send."""
    if source not in ("late", "setup") or side not in ("long", "short"):
        return True
    from pipeline import allow_alert

    if not allow_alert(source, symbol, side):
        logging.getLogger("bitunix-common").info("Deduped %s %s %s", source, symbol, side)
        return False
    ensure_data()
    row = {
        "ts": time.time(),
        "source": source,
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "stop_pct": stop_pct,
        "target_pct": target_pct,
        "note": note,
    }
    with SIGNALS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    return True
