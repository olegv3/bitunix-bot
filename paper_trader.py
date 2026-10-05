#!/usr/bin/env python3
"""Paper-trade follower. Not a live order bot.

Reads data/signals.jsonl, tracks each hypothetical entry, and pings Telegram
when price hits the stop, the target, or the hold window expires.
"""

import json
import os
import time
from pathlib import Path

import requests

from bot_common import SIGNALS_PATH, ensure_data, file_logger, send_telegram
from thresholds import update_from_outcomes

TICKERS_URL = "https://fapi.bitunix.com/api/v1/futures/market/tickers"
POLL_SECONDS = 15
HOLD_SECONDS = 6 * 60 * 60
SUMMARY_SECONDS = 6 * 60 * 60
DATA_DIR = Path(os.getenv("BOT_DATA_DIR", "data"))
OPEN_PATH = DATA_DIR / "open_paper.json"
CLOSED_PATH = DATA_DIR / "closed_paper.jsonl"
DASHBOARD_PATH = DATA_DIR / "dashboard.txt"

log = file_logger("bitunix-paper")


def load_open() -> dict:
    try:
        return json.loads(OPEN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_open(rows: dict) -> None:
    ensure_data()
    OPEN_PATH.write_text(json.dumps(rows), encoding="utf-8")


def ingest(open_rows: dict, offset: int) -> int:
    if not SIGNALS_PATH.exists():
        return offset
    lines = SIGNALS_PATH.read_text(encoding="utf-8").splitlines()
    for line in lines[offset:]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = f"{row.get('source')}:{row.get('symbol')}:{int(row.get('ts') or 0)}"
        if key in open_rows or not row.get("symbol") or not row.get("entry"):
            continue
        open_rows[key] = row
        log.info("Paper open %s %s %s @ %s", row.get("source"), row.get("side"), row.get("symbol"), row.get("entry"))
    return len(lines)


def prices() -> dict:
    rows = requests.get(TICKERS_URL, timeout=20).json().get("data") or []
    out = {}
    for row in rows:
        symbol = row.get("symbol")
        try:
            price = float(row.get("lastPrice") or row.get("last") or 0)
        except (TypeError, ValueError):
            continue
        if symbol and price > 0:
            out[symbol] = price
    return out


def resolve(trade: dict, price: float, now: float):
    entry = float(trade["entry"])
    if entry <= 0:
        return "invalid", 0.0
    move = (price - entry) / entry * 100
    side = trade.get("side")
    favorable = move if side == "long" else -move
    trade["favorable_pct"] = max(float(trade.get("favorable_pct") or 0), favorable)
    stop = float(trade.get("stop_pct") or 1.2)
    target = float(trade.get("target_pct") or 2.0)
    if favorable <= -stop:
        return "stop", favorable
    if favorable >= target:
        return "target", favorable
    if now - float(trade.get("ts") or now) >= HOLD_SECONDS:
        return "timeout", favorable
    return None, favorable


def write_dashboard(closed: list) -> str:
    by_source = {}
    for row in closed[-200:]:
        bucket = by_source.setdefault(row.get("source") or "?", {"n": 0, "win": 0, "sum": 0.0})
        bucket["n"] += 1
        bucket["sum"] += float(row.get("favorable_pct") or 0)
        if row.get("result") == "target":
            bucket["win"] += 1
    lines = ["Paper scoreboard (last 200 closes)"]
    for source, bucket in sorted(by_source.items()):
        win_rate = 100 * bucket["win"] / bucket["n"] if bucket["n"] else 0
        avg = bucket["sum"] / bucket["n"] if bucket["n"] else 0
        lines.append(f"{source}: {bucket['n']} closed, {win_rate:.0f}% target, avg favorable {avg:+.2f}%")
    if len(lines) == 1:
        lines.append("No closed paper trades yet.")
    text = "\n".join(lines)
    DASHBOARD_PATH.write_text(text + "\n", encoding="utf-8")
    return text


def main() -> None:
    ensure_data()
    log.info("Paper trader starting")
    send_telegram(
        "Paper trader is running. Every signal is tracked with a hypothetical entry, stop, and target. "
        "I will message when it hits the stop, the target, or times out. Not a live order."
    )
    open_rows = load_open()
    offset = 0
    closed = []
    last_summary = time.time()
    while True:
        try:
            offset = ingest(open_rows, offset)
            live = prices()
            now = time.time()
            for key, trade in list(open_rows.items()):
                price = live.get(trade.get("symbol"))
                if not price:
                    continue
                result, favorable = resolve(trade, price, now)
                if not result:
                    continue
                open_rows.pop(key, None)
                done = dict(trade)
                done.update({"result": result, "exit": price, "favorable_pct": favorable, "closed_at": now})
                closed.append(done)
                with CLOSED_PATH.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(done) + "\n")
                send_telegram(
                    f"\U0001f4c4 <b>PAPER {result.upper()}</b> {trade.get('symbol')}\n"
                    f"{trade.get('source')} {trade.get('side')} from {float(trade['entry']):.6g} to {price:.6g}\n"
                    f"Favorable move <b>{favorable:+.2f}%</b>\n"
                    f"Not a live fill."
                )
                log.info("Paper %s %s %+.2f%%", result, trade.get("symbol"), favorable)
            save_open(open_rows)
            update_from_outcomes(closed)
            if now - last_summary >= SUMMARY_SECONDS:
                send_telegram(write_dashboard(closed))
                last_summary = now
            else:
                write_dashboard(closed)
        except Exception as exc:
            log.error("Paper loop failed: %s", exc)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
