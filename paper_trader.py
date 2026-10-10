#!/usr/bin/env python3
"""Paper follower. Half off at 70% of peak. Rest trails to 50%."""

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
BOOK_SECONDS = 60 * 60
ADD_MARGINS = (1.0, 3.0)
ADD_AT_MARGIN_PCT = (150.0, 300.0)
CAUTIOUS_ADD_AT = (250.0, 450.0)
BTC_TREND_PCT = 3.0
STOP_DOLLARS = 50.0
BANK_AT_PCT = 100.0
TRAIL_KEEP = 0.7
TRAIL_REST = 0.5
SHORT_ADD_CAP = 25.0
SHORT_EXIT_PCT = 40.0
DEFAULT_LEVERAGE = 20.0
START_CASH = 1000.0
WALLET_EPOCH = 2
DATA_DIR = Path(os.getenv("BOT_DATA_DIR", "data"))
OPEN_PATH = DATA_DIR / "open_paper.json"
CLOSED_PATH = DATA_DIR / "closed_paper.jsonl"
WALLET_PATH = DATA_DIR / "paper_wallet.json"

log = file_logger("bitunix-paper")
_leverage = {}
_leverage_at = 0.0
_tape = 0.0
_tape_at = 0.0


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


def fresh_wallet() -> dict:
    return {"start": START_CASH, "realized": 0.0, "closes": 0, "started": time.time(), "epoch": WALLET_EPOCH}


def load_wallet() -> dict:
    try:
        wallet = json.loads(WALLET_PATH.read_text(encoding="utf-8"))
        if wallet.get("epoch") == WALLET_EPOCH and wallet.get("start") == START_CASH:
            return wallet
    except (OSError, json.JSONDecodeError):
        pass
    return fresh_wallet()


def save_wallet(wallet: dict) -> None:
    ensure_data()
    WALLET_PATH.write_text(json.dumps(wallet), encoding="utf-8")


def wallet_line(wallet: dict) -> str:
    balance = START_CASH + float(wallet.get("realized") or 0)
    return f"PAPER WALLET ${balance:.2f} from ${START_CASH:.0f}. Realized ${float(wallet.get('realized') or 0):+.2f}."


def counts_for_wallet(trade: dict, wallet: dict) -> bool:
    return float(trade.get("ts") or 0) >= float(wallet.get("started") or 0)


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


def market() -> tuple:
    rows = requests.get(TICKERS_URL, timeout=20).json().get("data") or []
    prices = {}
    day = {}
    for row in rows:
        symbol = row.get("symbol")
        try:
            price = float(row.get("lastPrice") or row.get("last") or 0)
            opened = float(row.get("open") or 0)
        except (TypeError, ValueError):
            continue
        if symbol and price > 0:
            prices[symbol] = price
            if opened:
                day[symbol] = (price - opened) / opened * 100
    return prices, day


def tape_change() -> float:
    global _tape, _tape_at
    if time.time() - _tape_at < 60:
        return _tape
    try:
        rows = requests.get(TICKERS_URL, timeout=20).json().get("data") or []
    except requests.RequestException:
        return _tape
    for row in rows:
        if row.get("symbol") != "BTCUSDT":
            continue
        try:
            last = float(row.get("lastPrice") or row.get("last") or 0)
            opened = float(row.get("open") or 0)
        except (TypeError, ValueError):
            break
        _tape = (last - opened) / opened * 100 if opened else 0.0
        _tape_at = time.time()
        break
    return _tape


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
            "trail_armed": False, "half_sold": False,
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


def side_of(trade: dict) -> str:
    return str(trade.get("side") or "").upper()


def book(open_rows: dict, prices: dict, wallet: dict) -> None:
    lines = []
    green = red = 0
    open_pnl = 0.0
    for trade in open_rows.values():
        if not counts_for_wallet(trade, wallet):
            continue
        price = prices.get(trade.get("symbol"))
        if not price:
            continue
        gain = gain_now(trade, price)
        green += gain >= 0
        red += gain < 0
        open_pnl += float(trade.get("margin") or 1) * float(trade.get("runner") or 1) * gain / 100
        open_pnl += float(trade.get("banked") or 0)
        lines.append((gain, f"{trade.get('symbol')} {side_of(trade)} {gain:+.0f}% ${trade.get('margin', 1):.0f}"))
    lines.sort(reverse=True)
    texts = [text for _, text in lines] or ["none"]
    chunks = [texts[i:i + 40] for i in range(0, len(texts), 40)]
    for index, chunk in enumerate(chunks, start=1):
        header = f"PAPER BOOK {green} green, {red} red" if index == 1 else f"PAPER BOOK continued {index}/{len(chunks)}"
        extra = f"\n{wallet_line(wallet)} Open marks ${open_pnl:+.2f}. Equity ${START_CASH + float(wallet.get('realized') or 0) + open_pnl:.2f}." if index == 1 else ""
        send_telegram(header + extra + "\n" + "\n".join(chunk) + "\nNot a live fill.")


def catch_up(open_rows: dict, prices: dict, wallet: dict) -> None:
    due = []
    for trade in open_rows.values():
        price = prices.get(trade.get("symbol"))
        if not price:
            continue
        gain = gain_now(trade, price)
        trade["peak_pct"] = max(float(trade.get("peak_pct") or 0), gain)
        if gain >= BANK_AT_PCT and not trade.get("trail_armed") and counts_for_wallet(trade, wallet):
            trade["trail_armed"] = True
            due.append(f"{trade.get('symbol')} {side_of(trade)} trail armed at {gain:.0f}%")
    if due:
        send_telegram("PAPER TRAIL armed\n" + "\n".join(due[:20]) + "\nHalf off at 70% of peak. Rest trails to 50%. Not a live fill.")
    book(open_rows, prices, wallet)


def resolve(trade: dict, price: float, now: float, tape: float, day_change: float):
    side = trade.get("side")
    lev = float(trade.get("leverage") or DEFAULT_LEVERAGE)
    adds = int(trade.get("adds") or 0)
    runner = float(trade.get("runner") or 1)
    avg = float(trade.get("avg") or trade.get("entry") or 0)
    if side == "long":
        trade["best"] = max(float(trade.get("best") or price), price)
    else:
        trade["best"] = min(float(trade.get("best") or price), price)
    peak = gain_now(trade, trade["best"])
    current = gain_now(trade, price)
    trade["peak_pct"] = max(float(trade.get("peak_pct") or 0), peak)
    open_dollars = float(trade.get("margin") or 1) * runner * current / 100
    if side == "short" and day_change >= SHORT_EXIT_PCT:
        return "momentum", open_dollars + float(trade.get("banked") or 0)
    cautious = (side == "long" and tape <= -BTC_TREND_PCT) or (side == "short" and tape >= BTC_TREND_PCT)
    add_at = CAUTIOUS_ADD_AT if cautious else ADD_AT_MARGIN_PCT
    runaway = side == "short" and day_change >= SHORT_ADD_CAP
    if runner >= 1 and adds < len(ADD_MARGINS) and not trade.get("trail_armed") and not runaway:
        against = -move_pct(side, float(trade.get("entry") or avg), price)
        if against >= add_at[adds] / lev:
            add = ADD_MARGINS[adds]
            margin = float(trade["margin"])
            trade["avg"] = (avg * margin + price * add) / (margin + add)
            trade["margin"] = margin + add
            trade["adds"] = adds + 1
            return "add", open_dollars
    if current >= BANK_AT_PCT and not trade.get("trail_armed"):
        trade["trail_armed"] = True
        return "armed", open_dollars
    if trade.get("trail_armed") and trade["peak_pct"] >= BANK_AT_PCT and not trade.get("half_sold") and current <= trade["peak_pct"] * TRAIL_KEEP and current > 0:
        banked = float(trade.get("margin") or 1) * 0.5 * current / 100
        trade["banked"] = float(trade.get("banked") or 0) + banked
        trade["runner"] = 0.5
        trade["half_sold"] = True
        return "half", banked
    if trade.get("half_sold") and trade["peak_pct"] >= BANK_AT_PCT and current <= trade["peak_pct"] * TRAIL_REST and current > 0:
        return "trail", open_dollars + float(trade.get("banked") or 0)
    if runner >= 1 and open_dollars <= -STOP_DOLLARS:
        return "stop", open_dollars
    age = now - float(trade.get("ts") or now)
    if age >= HOLD_SECONDS and (current <= 0 or not trade.get("trail_armed")):
        return "timeout", open_dollars + float(trade.get("banked") or 0)
    return None, open_dollars


def main() -> None:
    ensure_data()
    open_rows = load_open()
    offset = 0
    closed = load_closed()
    wallet = load_wallet()
    save_wallet(wallet)
    last_book = time.time()
    send_telegram(wallet_line(wallet) + " Half off at 70% of peak. Rest trails to 50%. Not a live fill.")
    try:
        prices, day = market()
        offset = ingest(open_rows, 0, leverage_map())
        catch_up(open_rows, prices, wallet)
        save_open(open_rows)
    except Exception:
        send_telegram("Paper follower startup error\n" + traceback.format_exc()[-500:])
    while True:
        try:
            prices, day = market()
            tape = tape_change()
            offset = ingest(open_rows, offset, leverage_map())
            now = time.time()
            for key, trade in list(open_rows.items()):
                price = prices.get(trade.get("symbol"))
                if not price:
                    continue
                result, dollars = resolve(trade, price, now, tape, float(day.get(trade.get("symbol")) or 0))
                side = side_of(trade)
                if result == "add":
                    send_telegram(
                        f"PAPER ADD {trade.get('symbol')} {side} margin ${trade['margin']:.0f} at {price:.6g}. "
                        f"Average now {float(trade['avg']):.6g}. Not a live fill."
                    )
                elif result == "armed":
                    if counts_for_wallet(trade, wallet):
                        send_telegram(
                            f"PAPER TRAIL ARMED {trade.get('symbol')} {side} at {price:.6g}. "
                            f"Half off if it gives back to 70% of the peak. Rest trails to 50%. Not a live fill."
                        )
                elif result == "half":
                    if counts_for_wallet(trade, wallet):
                        wallet["realized"] = float(wallet.get("realized") or 0) + dollars
                        save_wallet(wallet)
                        send_telegram(
                            f"PAPER HALF {trade.get('symbol')} {side} ${dollars:+.2f} at {price:.6g}. "
                            f"Rest stays on. {wallet_line(wallet)} Not a live fill."
                        )
                elif result:
                    counted = counts_for_wallet(trade, wallet)
                    if counted:
                        wallet["realized"] = float(wallet.get("realized") or 0) + dollars
                        wallet["closes"] = int(wallet.get("closes") or 0) + 1
                        save_wallet(wallet)
                        send_telegram(f"PAPER {result.upper()} {trade.get('symbol')} {side} ${dollars:+.2f}. {wallet_line(wallet)} Not a live fill.")
                    else:
                        send_telegram(f"PAPER {result.upper()} {trade.get('symbol')} {side} ${dollars:+.2f}. Old trade, not counted. Not a live fill.")
                    open_rows.pop(key, None)
                    done = dict(trade)
                    done.update({"result": result, "exit": price, "pnl": dollars, "closed_at": now, "counted": counted})
                    closed.append(done)
                    with CLOSED_PATH.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(done) + "\n")
            save_open(open_rows)
            update_from_outcomes(closed)
            if now - last_book >= BOOK_SECONDS:
                book(open_rows, prices, wallet)
                last_book = now
        except Exception as exc:
            log.error("Paper loop failed: %s", exc)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
