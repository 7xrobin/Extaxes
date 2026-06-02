"""
Live price fetching via yfinance.
No API key required.
"""
import pandas as pd
import yfinance as yf


def get_price(ticker: str) -> float:
    """
    Fetch current price for a ticker (stock or ETF).
    Returns 0.0 on failure — never raises.
    For ETFs use XETRA suffix where needed, e.g. "VWCE.DE"
    """
    try:
        data = yf.Ticker(ticker)
        price = data.fast_info.get("last_price") or data.info.get("regularMarketPrice", 0)
        return float(price or 0)
    except Exception:
        return 0.0


def get_prices(tickers: list[str]) -> dict[str, float]:
    """Batch fetch. Returns {ticker: price}."""
    return {ticker: get_price(ticker) for ticker in tickers}


# Maps the UI range codes to yfinance history periods.
# "ALL" is handled by the caller (falls back to cost basis), so it's not listed here.
PERIOD_MAP = {
    "1M":  "1mo",
    "3M":  "3mo",
    "6M":  "6mo",
    "1Y":  "1y",
    "YTD": "ytd",
}


def get_period_start_price(ticker: str, period: str) -> float:
    """
    Price at (or just after) the start of the given window — the first available
    close in the yfinance history for that period. Returns 0.0 on any failure.
    """
    yf_period = PERIOD_MAP.get(period)
    if not yf_period:
        return 0.0
    try:
        hist = yf.Ticker(ticker).history(period=yf_period)
        if hist is None or hist.empty or "Close" not in hist:
            return 0.0
        return float(hist["Close"].iloc[0] or 0.0)
    except Exception:
        return 0.0


def get_period_start_prices(tickers: list[str], period: str) -> dict[str, float]:
    """Batch variant of get_period_start_price. Returns {ticker: start_price}."""
    return {ticker: get_period_start_price(ticker, period) for ticker in tickers}


# Day offsets used to slice trailing windows out of a single 1-year history.
_PERIOD_DAYS = {"1M": 30, "3M": 91, "6M": 182, "1Y": 366}


def get_period_returns(ticker: str) -> dict[str, float]:
    """
    Trailing % return for each UI period (1M/3M/6M/1Y/YTD), computed from a single
    1-year history fetch so a ticker is only hit once. Each value is the percentage
    change from the first close in the window to the latest close. Missing windows
    (and any failure) yield 0.0 — never raises.
    """
    out = {p: 0.0 for p in PERIOD_MAP}
    try:
        hist = yf.Ticker(ticker).history(period="1y")
        if hist is None or hist.empty or "Close" not in hist:
            return out
        closes = hist["Close"].dropna()
        if closes.empty:
            return out
        current = float(closes.iloc[-1])
        last_date = closes.index[-1]
        cutoffs = {
            p: last_date - pd.Timedelta(days=days)
            for p, days in _PERIOD_DAYS.items()
        }
        cutoffs["YTD"] = pd.Timestamp(year=last_date.year, month=1, day=1, tz=last_date.tz)
        for period, cutoff in cutoffs.items():
            window = closes[closes.index >= cutoff]
            if window.empty:
                continue
            start = float(window.iloc[0])
            if start > 0:
                out[period] = (current - start) / start * 100.0
    except Exception:
        return out
    return out


# Common Germany-listed ETF tickers for yfinance
COMMON_ETFS = {
    "IE00B4L5Y983": "IWDA.AS",    # iShares MSCI World (Amsterdam)
    "IE00B3RBWM25": "VWCE.DE",    # Vanguard FTSE All-World (XETRA)
    "LU0274208692": "XMWO.DE",    # Xtrackers MSCI World (XETRA)
    "IE00B4L5YC18": "EIMI.DE",    # iShares MSCI EM (XETRA)
}
