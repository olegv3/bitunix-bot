"""Shared filters and alert formatting. No secrets."""

import requests

STOCK_BASES = {
    "AAPL", "AMD", "AMZN", "AVGO", "BABA", "COIN", "DIS", "GOOGL", "META",
    "MSFT", "MSTR", "NFLX", "NVDA", "TSLA", "XOM", "TWST", "KSTR", "MVLL",
    "XBI", "SPY", "QQQ", "IWM", "EWZ", "INTC", "PLTR", "SMCI", "ARM", "HOOD",
    "UBER", "PYPL", "BA", "JPM", "GS", "BAC", "WMT", "COST", "NKE",
    "ORCL", "CRM", "ADBE", "QCOM", "MU", "TSM", "ASML", "SHOP", "SQ",
    "RIVN", "LCID", "GME", "AMC", "DKNG", "ABNB", "SNOW", "NET", "CRWD",
    "SAMSUNG", "SNDK", "SOXL", "TQQQ", "SQQQ", "SPXU", "UVXY", "ARKK",
}
COMMODITY_BASES = {"XAU", "XAG", "CL"}


def base_of(symbol: str) -> str:
    symbol = symbol.upper()
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def allowed(symbol: str) -> bool:
    base = base_of(symbol)
    if base in COMMODITY_BASES:
        return True
    if base in STOCK_BASES:
        return False
    return symbol.upper().endswith("USDT")


def chart_link(symbol: str) -> str:
    return f"https://www.bitunix.com/contract-trade/{symbol.upper()}"


def strength(change_pct: float) -> str:
    size = abs(change_pct)
    if size >= 4:
        return "LARGE"
    if size >= 2:
        return "MEDIUM"
    return "SMALL"


def ema(values: list, length: int) -> float:
    k = 2 / (length + 1)
    value = values[0]
    for price in values[1:]:
        value = price * k + value * (1 - k)
    return value


def ta_snapshot(symbol: str, price: float) -> str:
    try:
        rows = requests.get(
            "https://fapi.bitunix.com/api/v1/futures/market/kline",
            params={"symbol": symbol, "interval": "15m", "limit": "80"},
            timeout=8,
        ).json().get("data") or []
        closes = [float(row["close"]) for row in rows]
        highs = [float(row["high"]) for row in rows]
        lows = [float(row["low"]) for row in rows]
    except (TypeError, ValueError, requests.RequestException):
        return ""
    if len(closes) < 30 or price <= 0:
        return ""
    rsi_gain = rsi_loss = 0.0
    for older, newer in zip(closes[-15:-1], closes[-14:]):
        diff = newer - older
        rsi_gain += max(diff, 0)
        rsi_loss += max(-diff, 0)
    rsi = 100 if rsi_loss == 0 else 100 - 100 / (1 + rsi_gain / rsi_loss)
    macd = ema(closes, 12) - ema(closes, 26)
    mid = sum(closes[-20:]) / 20
    band = (sum((item - mid) ** 2 for item in closes[-20:]) / 20) ** 0.5
    above = [item for item in highs[-40:] if item > price * 1.001]
    below = [item for item in lows[-40:] if item < price * 0.999]
    try:
        higher = requests.get(
            "https://fapi.bitunix.com/api/v1/futures/market/kline",
            params={"symbol": symbol, "interval": "4h", "limit": "80"},
            timeout=8,
        ).json().get("data") or []
        high_4h = [float(row["high"]) for row in higher]
        low_4h = [float(row["low"]) for row in higher]
        above = [item for item in high_4h if item > price * 1.002] or above
        below = [item for item in low_4h if item < price * 0.998] or below
    except (TypeError, ValueError, requests.RequestException):
        pass
    support = f"{max(below):.6g}" if below else "at the low"
    resistance = f"{min(above):.6g}" if above else "at the high"
    return (
        f"15m RSI {rsi:.0f} \u00b7 MACD {macd:+.4g}\n"
        f"EMA9 {ema(closes, 9):.6g} \u00b7 EMA21 {ema(closes, 21):.6g}\n"
        f"Bollinger {mid - 2 * band:.6g} to {mid + 2 * band:.6g}\n"
        f"4h support {support} \u00b7 resistance {resistance}"
    )
