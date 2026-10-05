#!/usr/bin/env python3
"""Shared utilities for Bitunix bots. No secrets."""

import logging
import os

import requests

log = logging.getLogger("bitunix-utils")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def send_telegram(text: str) -> None:
    """Send an HTML message to Telegram. Falls back to printing if env vars are missing."""
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


def ema(values: list, length: int) -> float:
    """Exponential moving average."""
    k = 2 / (length + 1)
    value = values[0]
    for price in values[1:]:
        value = price * k + value * (1 - k)
    return value


def rsi(values: list, period: int = 14) -> float:
    """Relative strength index over the last `period` diffs."""
    if len(values) < period + 1:
        return 50.0
    gain = loss = 0.0
    for older, newer in zip(values[-(period + 1) : -1], values[-period:]):
        diff = newer - older
        gain += max(diff, 0)
        loss += max(-diff, 0)
    if loss == 0:
        return 100.0
    return 100 - 100 / (1 + gain / loss)


def strip_html(text: str) -> str:
    """Remove basic HTML tags for plain-text logging."""
    return text.replace("<b>", "").replace("</b>", "")
