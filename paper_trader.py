#!/usr/bin/env python3
"""Paper follower. Confirms each open, then banks half at 100% leveraged profit."""

import json
import os
import time
from pathlib import Path

import requests

from bot_common import SIGNALS_PATH, ensure_data, file_logger, send_telegram
from thresholds import update_from_outcomes

TICKERS_URL = "https://fapi.bitunix.com/api/v1/futures/market/tickers"
PAIRS_URL = "https://fapi.bitunix.com/api/v1/futures/market/trading_pairs"
POLL_SECONDS = 15
HOLD_SECONDS = 6 * 60 * 60
SUMMARY_SECONDS = 6 * 60 * 60
ADD_MARGINS = (1.0, 3.0, 5.0)
ADD_AT_MARGIN_PCT = (150.0, 300.0, 450.0)
STOP_DOLLARS = 50.0
BANK_AT_PCT = 100.0
DEFAULT_LEVERAGE = 20.0
DATA_DIR = Path(os.getenv("BOT_DATA_DIR", "data"))
OPEN_PATH = DATA_DIR / "open_paper.json"
CLOSED_PATH = DATA_DIR / "closed_paper.jsonl"
DASHBOARD_PATH = DATA_DIR / "dashboard.txt"

log = file_logger("bitunix-paper")
_leverage = {}
_leverage_at = 0.0


def load_open() -> dict:
    try:
        return json.loads(OPEN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_closed() -> list:
    if not CLOSED_PATH.exists():
        return []
    rows = []
    for line in CLOSED_PATH.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-500:]


def save_open(rows: dict) -> None:
    ensure_data()
    OPEN_PATH.write_text(json.dumps(rows), encoding="utf-8")


def leverage_map() -> dict:
    global _leverage_at
    if _leverage and time.time() - _leverage_at < 60 * 60:
        return _leverage
    rows = requests.get(PAIRS_URL, timeout=20).json().get("data") or []
    for row in rows:
        symbol = row.get("symbol")
        try:
            lev = float(row.get("maxLeverage") or 0)
        except (TypeError, ValueError):
            continue
        if symbol and lev > 0:
            _leverage[symbol] = min(lev, 200)
    _leverage_at = time.time()
    return _leverage


def market() -> dict:
    rows = requests.get(TICKERS_URL, timeout=20).json().get("data") or []
    prices = {}
    for row in rows:
        symbol = row.get("symbol")
        try:
            price = float(row.get("lastPrice") or row.get("last") or 0)
        except (TypeError, ValueError):
            continue
        if symbol and price > 0:
            prices[symbol] = price
    return prices


def ingest(open_rows: dict, offset: int, leverage: dict) -> int:
    if not SIGNALS_PATH.exists():
        return offset
    lines = SIGNALS_PATH.read_text(encoding="utf-8").splitlines()
    open_symbols = {row.get("symbol") for row in open_rows.values()}
    for line in lines[offset:]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        symbol = row.get("symbol")
        key = f"{row.get('source')}:{symbol}:{int(row.get('ts') or 0)}"
        if key in open_rows or not symbol or not row.get("entry") or symbol in open_symbols:
            continue
        entry = float(row["entry"])
        lev = leverage.get(symbol) or DEFAULT_LEVERAGE
        row.update({
            "margin": 1.0,
            "avg": entry,
            "adds": 0,
            "runner": 1.0,
            "banked": 0.0,
            "peak_pct": 0.0,
            "best": entry,
            "leverage": lev,
        })
        open_rows[key] = row
        open_symbols.add(symbol)
        need = entry * (1 + BANK_AT_PCT / lev / 100) if row.get("side") == "long" else entry * (1 - BANK_AT_PCT / lev / 100)
        send_telegram(
            f"\U0001f4c4 <b>PAPER TRACKING</b> {symbol}\n"
            f"{str(row.get('side')).title()} from {entry:.6g} at {lev:.0f}x\n"
            f"Half sells if price reaches <b>{need:.6g}</b>\n"
            f"Not a live order."
        )
        log.info("Paper tracking %s %s @ %s x%s", row.get("side"), symbol, entry, lev)
    return len(lines)


def move_pct(side: str, avg: float, price: float) -> float:
    if avg <= 0:
        return 0.0
    move = (price - avg) / avg * 100
    return move if side == "long" else -move


def resolve(trade: dict, price: float, now: float):
    side = trade.get("side")
    lev = float(trade.get("leverage") or DEFAULT_LEVERAGE)
    adds = int(trade.get("adds") or 0)
    runner = float(trade.get("runner") or 1)
    if side == "long":
        trade["best"] = max(float(trade.get("best") or price), price)
    else:
        trade["best"] = min(float(trade.get("best") or price), price)
    check_price = trade["best"]
    gain = move_pct(side, float(trade["avg"]), check_price) * lev
    trade["peak_pct"] = max(float(trade.get("peak_pct") or 0), gain)
    open_dollars = float(trade.get("margin") or 1) * runner * gain / 100
    if runner >= 1 and adds < len(ADD_MARGINS):
        against = -move_pct(side, float(trade["entry"]), price)
        if against >= ADD_AT_MARGIN_PCT[adds] / lev:
            add = ADD_MARGINS[adds]
            margin = float(trade["margin"])
            avg = float(trade["avg"])
            trade["avg"] = (avg * margin + price * add) / (margin + add)
            trade["margin"] = margin + add
            trade["adds"] = adds + 1
            return "add", open_dollars
    if runner >= 1 and gain >= BANK_AT_PCT:
        bank = float(trade["margin"]) * 0.5 * gain / 100
        trade["banked"] = float(trade.get("banked") or 0) + bank
        trade["runner"] = 0.5
        return "partial", open_dollars
    if runner < 1 and trade["peak_pct"] >= BANK_AT_PCT and gain <= trade["peak_pct"] * 0.5 and gain > 0:
        return "trail", open_dollars + float(trade.get("banked") or 0)
    if runner >= 1 and open_dollars <= -STOP_DOLLARS:
        return "stop", open_dollars
    if now - float(trade.get("ts") or now) >= HOLD_SECONDS:
        return "timeout", open_dollars + float(trade.get("banked") or 0)
    return None, open_dollars


def write_dashboard(closed: list) -> str:
    by_source = {}
    for row in closed[-200:]:
        bucket = by_source.setdefault(row.get("source") or "?", {"n": 0, "win": 0, "sum": 0.0})
        bucket["n"] += 1
        bucket["sum"] += float(row.get("pnl") or 0)
        if float(row.get("pnl") or 0) > 0:
            bucket["win"] += 1
    lines = ["Paper scoreboard (last 200 closes)"]
    for source, bucket in sorted(by_source.items()):
        win_rate = 100 * bucket["win"] / bucket["n"] if bucket["n"] else 0
        avg = bucket["sum"] / bucket["n"] if bucket["n"] else 0
        lines.append(f"{source}: {bucket['n']} closed, {win_rate:.0f}% green, avg ${avg:+.2f}")
    if len(lines) == 1:
        lines.append("No closed paper trades yet.")
    text = "\n".join(lines)
    DASHBOARD_PATH.write_text(text + "\n", encoding="utf-8")
    return text


def main() -> None:
    ensure_data()
    log.info("Paper trader starting, signals %s", SIGNALS_PATH)
    send_telegram("Paper follower is running. A tracked trade sends PAPER TRACKING, then PAPER PARTIAL at 100%.")
    open_rows = load_open()
    offset = 0
    closed = load_closed()
    last_summary = time.time()
    while True:
        try:
            prices = market()
            offset = ingest(open_rows, offset, leverage_map())
            now = time.time()
            for key, trade in list(open_rows.items()):
                price = prices.get(trade.get("symbol"))
                if not price:
                    continue
                result, dollars = resolve(trade, price, now)
                if result == "add":
                    send_telegram(
                        f"\U0001f4c4 <b>PAPER ADD</b> {trade.get('symbol')}\n"
                        f"{trade.get('side')} add {int(trade['adds'])}, margin now ${trade['margin']:.0f}\n"
                        f"Average {float(trade['avg']):.6g}, price {price:.6g}\n"
                        f"Not a live fill."
                    )
                    continue
                if result == "partial":
                    send_telegram(
                        f"\U0001f4c4 <b>PAPER PARTIAL</b> {trade.get('symbol')}\n"
                        f"Sold half around 100% leveraged profit at {float(trade.get('best') or price):.6g}\n"
                        f"Banked ${float(trade.get('banked') or 0):.2f}. Half is still riding.\n"
                        f"Not a live fill."
                    )
                    continue
                if not result:
                    continue
                open_rows.pop(key, None)
                done = dict(trade)
                done.update({"result": result, "exit": price, "pnl": dollars, "closed_at": now})
                closed.append(done)
                with CLOSED_PATH.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(done) + "\n")
                send_telegram(
                    f"\U0001f4c4 <b>PAPER {result.upper()}</b> {trade.get('symbol')}\n"
                    f"{trade.get('side')} from {float(trade['entry']):.6g} to {price:.6g}\n"
                    f"Result <b>${dollars:+.2f}</b>\n"
                    f"Not a live fill."
                )
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
