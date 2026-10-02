"""Shared filters and alert formatting. No secrets."""

STOCK_BASES = {
    "AAPL", "AMD", "AMZN", "AVGO", "BABA", "COIN", "DIS", "GOOGL", "META",
    "MSFT", "MSTR", "NFLX", "NVDA", "TSLA", "XOM", "TWST", "KSTR", "MVLL",
    "XBI", "SPY", "QQQ", "IWM", "INTC", "PLTR", "SMCI", "ARM", "HOOD",
    "UBER", "PYPL", "BA", "JPM", "GS", "BAC", "WMT", "COST", "NKE",
    "ORCL", "CRM", "ADBE", "QCOM", "MU", "TSM", "ASML", "SHOP", "SQ",
    "RIVN", "LCID", "GME", "AMC", "DKNG", "ABNB", "SNOW", "NET", "CRWD",
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
