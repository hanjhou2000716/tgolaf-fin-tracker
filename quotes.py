"""Resilient market-quote helpers with explicit source diagnostics."""

import datetime
import time

class QuoteUnavailableError(RuntimeError):
    pass


def _positive_price(value):
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def get_tw_stock_price(symbol, finmind_token, http_get=None, ticker_factory=None, sleep=time.sleep):
    """Fetch a Taiwan quote from FinMind, Yahoo chart, then yfinance.

    A source must return a positive numeric price.  If every source fails, the
    raised exception carries each failed source for the GitHub Actions log.
    """
    symbol = str(symbol).strip().upper()
    failures = []
    start_date = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    if http_get is None:
        import requests
        http_get = requests.get

    if finmind_token:
        for attempt in range(2):
            try:
                response = http_get(
                    "https://api.finmindtrade.com/api/v4/data",
                    params={
                        "dataset": "TaiwanStockPrice", "data_id": symbol,
                        "start_date": start_date, "token": finmind_token,
                    },
                    timeout=10,
                )
                response.raise_for_status()
                records = response.json().get("data", [])
                price = _positive_price(records[-1].get("close") if records else None)
                if price is not None:
                    print(f"TW quote {symbol}: FinMind")
                    return price
                failures.append(f"FinMind attempt {attempt + 1}: empty/invalid close")
            except Exception as error:
                failures.append(f"FinMind attempt {attempt + 1}: {type(error).__name__}")
            if attempt == 0:
                sleep(1)
    else:
        failures.append("FinMind: token missing")

    try:
        response = http_get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.TW?interval=1d&range=5d",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        response.raise_for_status()
        result = response.json()["chart"]["result"][0]
        prices = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        price = next((candidate for candidate in reversed([_positive_price(value) for value in prices]) if candidate), None)
        if price is not None:
            print(f"TW quote {symbol}: Yahoo chart")
            return price
        failures.append("Yahoo chart: empty/invalid close")
    except Exception as error:
        failures.append(f"Yahoo chart: {type(error).__name__}")

    try:
        if ticker_factory is None:
            import yfinance as yf
            ticker_factory = yf.Ticker
        prices = ticker_factory(f"{symbol}.TW").history(period="5d")["Close"].dropna()
        price = _positive_price(prices.iloc[-1] if not prices.empty else None)
        if price is not None:
            print(f"TW quote {symbol}: yfinance")
            return price
        failures.append("yfinance: empty/invalid close")
    except Exception as error:
        failures.append(f"yfinance: {type(error).__name__}")

    raise QuoteUnavailableError(f"Taiwan quote unavailable for {symbol}; " + "; ".join(failures))
