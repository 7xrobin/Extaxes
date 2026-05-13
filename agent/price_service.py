"""
Live price fetching via yfinance.
No API key required.
"""
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


# Common Germany-listed ETF tickers for yfinance
COMMON_ETFS = {
    "IE00B4L5Y983": "IWDA.AS",    # iShares MSCI World (Amsterdam)
    "IE00B3RBWM25": "VWCE.DE",    # Vanguard FTSE All-World (XETRA)
    "LU0274208692": "XMWO.DE",    # Xtrackers MSCI World (XETRA)
    "IE00B4L5YC18": "EIMI.DE",    # iShares MSCI EM (XETRA)
}
