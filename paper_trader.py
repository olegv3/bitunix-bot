#!/usr/bin/env python3
"""Paper follower. Banks half at 100%, another quarter at 250%, then trails."""

import json
import os
import time
import traceback
from pathlib import Path

import requests

from bot_common import SIGNALS_PATH, ensure_data, file_logger, send_telegram
from thresholds import update_from_outcomes

TICKERS_URL = "https://fapi.bitunix.com/api/v1/futures/market/tickers"
PAIRS_URL = "https://fapi.bitunix.com/api/v1/futures/market/trading_pairs"
POLL_SECONDS = 15
HOLD_SECONDS = 6 * 60 * 60
BOOK_SECONDS = 2 * 60 * 60
ADD_MARGINS = (1.0, 3.0, 5.0)
ADD_AT_MARGIN_PCT = (150.0, 300.0, 450.0)
STOP_DOLLARS = 50.0
BANK_AT_PCT = 100.0
SECOND_BANK_PCT = 250.0
TRAIL_KEEP = 0.7
DEFAULT_LEVERAGE = 20.0
DATA_DIR = Path(os.getenv("BOT_DATA_DIR", "data"))
OPEN_PATH = DATA_DIR / "open_paper.json"
CLOSED_PATH = DATA_DIR / "closed_paper.jsonl"

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
    try:
        rows = requests.get(PAIRS_URL, timeout=20).json().get("data") or []
    except requests.RequestException:
        return _leverage
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
            "margin": 1.0, "avg": entry, "adds": 0, "runner": 1.0,
            "banked": 0.0, "peak_pct": 0.0, "best": entry, "leverage": lev,
            "partial_sent": False, "second_sent": False,
        })
        open_rows[key] = row
        open_symbols.add(symbol)
    return len(lines)


def move_pct(side: str, avg: float, price: float) -> float:
    if avg <= 0:
        return 0.0
    move = (price - avg) / avg * 100
    return move if side == "long" else -move


def gain_now(trade: dict, price: float) -> float:
    lev = float(trade.get("leverage") or DEFAULT_LEVERAGE)
    return move_pct(trade.get("side"), float(trade.get("avg") or trade.get("entry") or 0), price) * lev


def book(open_rows: dict, prices: dict) -> None:
    lines = []
    green = red = 0
    for trade in open_rows.values():
        price = prices.get(trade.get("symbol"))
        if not price:
            continue
        gain = gain_now(trade, price)
        green += gain >= 0
        red += gain < 0
        lines.append((gain, f"{trade.get('symbol')} {trade.get('side')} {gain:+.0f}% ${trade.get('margin', 1):.0f}"))
    lines.sort(reverse=True)
    shown = "\n".join(text for _, text in lines[:12]) or "none"
    send_telegram(f"PAPER BOOK {green} green, {red} red\n{shown}\nNot a live fill.")


def catch_up(open_rows: dict, prices: dict) -> None:
    due = []
    for trade in open_rows.values():
        price = prices.get(trade.get("symbol"))
        if not price:
            continue
        gain = gain_now(trade, price)
        trade["peak_pct"] = max(float(trade.get("peak_pct") or 0), gain)
        if gain >= SECOND_BANK_PCT and not trade.get("second_sent"):
            trade["banked"] = float(trade.get("banked") or 0) + float(trade.get("margin") or 1) * 0.25 * gain / 100
            trade["runner"] = 0.25
            trade["partial_sent"] = True
            trade["second_sent"] = True
            due.append(f"{trade.get('symbol')} second bank at {price:.6g}, {gain:.0f}%")
        elif gain >= BANK_AT_PCT and not trade.get("partial_sent"):
            trade["banked"] = float(trade.get("margin") or 1) * 0.5 * gain / 100
            trade["runner"] = 0.5
            trade["partial_sent"] = True
            due.append(f"{trade.get('symbol')} half off at {price:.6g}, {gain:.0f}%")
    if due:
        send_telegram("PAPER STAGGER catch-up\n" + "\n".join(due[:20]) + "\nNot a live fill.")
    book(open_rows, prices)


def resolve(trade: dict, price: float, now: float):
    side = trade.get("side")
    lev = float(trade.get("leverage") or DEFAULT_LEVERAGE)
    adds = int(trade.get("adds") or 0)
    runner = float(trade.get("runner") or 1)
    avg = float(trade.get("avg") or trade.get("entry") or 0)
    if side == "long":
        trade["best"] = max(float(trade.get("best") or price), price)
    else:
        trade["best"] = min(float(trade.get("best") or price), price)
    gain = gain_now(trade, trade["best"])
    trade["peak_pct"] = max(float(trade.get("peak_pct") or 0), gain)
    open_dollars = float(trade.get("margin") or 1) * runner * gain / 100
    if runner >= 1 and adds < len(ADD_MARGINS):
        against = -move_pct(side, float(trade.get("entry") or avg), price)
        if against >= ADD_AT_MARGIN_PCT[adds] / lev:
            add = ADD_MARGINS[adds]
            margin = float(trade["margin"])
            trade["avg"] = (avg * margin + price * add) / (margin + add)
            trade["margin"] = margin + add
            trade["adds"] = adds + 1
            return "add", open_dollars
    if gain >= SECOND_BANK_PCT and not trade.get("second_sent"):
        trade["banked"] = float(trade.get("banked") or 0) + float(trade.get("margin") or 1) * 0.25 * gain / 100
        trade["runner"] = 0.25
        trade["partial_sent"] = True
        trade["second_sent"] = True
        return "second", open_dollars
    if gain >= BANK_AT_PCT and not trade.get("partial_sent"):
        trade["banked"] = float(trade.get("margin") or 1) * 0.5 * gain / 100
        trade["runner"] = 0.5
        trade["partial_sent"] = True
        return "partial", open_dollars
    if trade.get("partial_sent") and trade["peak_pct"] >= BANK_AT_PCT and gain <= trade["peak_pct"] * TRAIL_KEEP and gain > 0:
        return "trail", open_dollars + float(trade.get("banked") or 0)
    if runner >= 1 and open_dollars <= -STOP_DOLLARS:
        return "stop", open_dollars
    if now - float(trade.get("ts") or now) >= HOLD_SECONDS:
        return "timeout", open_dollars + float(trade.get("banked") or 0)
    return None, open_dollars


def main() -> None:
    ensure_data()
    open_rows = load_open()
    offset = 0
    closed = load_closed()
    last_book = time.time()
    try:
        offset = ingest(open_rows, 0, leverage_map())
        catch_up(open_rows, market())
        save_open(open_rows)
    except Exception:
        send_telegram("Paper follower startup error\n" + traceback.format_exc()[-500:])
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
                        f"PAPER ADD {trade.get('symbol')} margin ${trade['margin']:.0f} at {price:.6g}. "
                        f"Average now {float(trade['avg']):.6g}. Not a live fill."
                    )
                elif result == "partial":
                    send_telegram(f"PAPER PARTIAL {trade.get('symbol')} half off at {price:.6g}. Banked ${float(trade.get('banked') or 0):.2f}. Not a live fill.")
                elif result == "second":
                    send_telegram(f"PAPER SECOND {trade.get('symbol')} another quarter off at {price:.6g}. Runner is 25%. Not a live fill.")
                elif result:
                    send_telegram(f"PAPER {result.upper()} {trade.get('symbol')} ${dollars:+.2f}. Not a live fill.")
                    open_rows.pop(key, None)
                    done = dict(trade)
                    done.update({"result": result, "exit": price, "pnl": dollars, "closed_at": now})
                    closed.append(done)
                    with CLOSED_PATH.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(done) + "\n")
            save_open(open_rows)
            update_from_outcomes(closed)
            if now - last_book >= BOOK_SECONDS:
                book(open_rows, prices)
                last_book = now
        except Exception as exc:
            log.error("Paper loop failed: %s", exc)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
