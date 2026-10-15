"""Resilient market-quote helpers with explicit source diagnostics."""

import datetime
import hashlib
import json
import math
import time
from zoneinfo import ZoneInfo

class QuoteUnavailableError(RuntimeError):
    pass


OTC_SYMBOLS = {"00886", "3455"}


def yahoo_market_symbols(symbol):
    """Prefer the known market, then try the alternate Yahoo Taiwan suffix."""
    preferred = "TWO" if symbol in OTC_SYMBOLS else "TW"
    alternate = "TW" if preferred == "TWO" else "TWO"
    return (f"{symbol}.{preferred}", f"{symbol}.{alternate}")


def _positive_price(value):
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def _evidence_hash(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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

    market_symbols = yahoo_market_symbols(symbol)
    for market_symbol in market_symbols:
        try:
            response = http_get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{market_symbol}?interval=1d&range=5d",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            response.raise_for_status()
            result = response.json()["chart"]["result"][0]
            prices = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
            price = next((candidate for candidate in reversed([_positive_price(value) for value in prices]) if candidate), None)
            if price is not None:
                print(f"TW quote {symbol}: Yahoo chart ({market_symbol})")
                return price
            failures.append(f"Yahoo chart {market_symbol}: empty/invalid close")
        except Exception as error:
            failures.append(f"Yahoo chart {market_symbol}: {type(error).__name__}")

    if ticker_factory is None:
        import yfinance as yf
        ticker_factory = yf.Ticker
    for market_symbol in market_symbols:
        try:
            prices = ticker_factory(market_symbol).history(period="5d")["Close"].dropna()
            price = _positive_price(prices.iloc[-1] if not prices.empty else None)
            if price is not None:
                print(f"TW quote {symbol}: yfinance ({market_symbol})")
                return price
            failures.append(f"yfinance {market_symbol}: empty/invalid close")
        except Exception as error:
            failures.append(f"yfinance {market_symbol}: {type(error).__name__}")

    raise QuoteUnavailableError(f"Taiwan quote unavailable for {symbol}; " + "; ".join(failures))


def get_tw_stock_close_on_date(symbol, trade_date, finmind_token, http_get=None, ticker_factory=None):
    """Fetch an exact-date Taiwan close; never substitute the latest quote."""
    symbol = str(symbol).strip().upper()
    if isinstance(trade_date, datetime.datetime):
        trade_date = trade_date.date()
    if not isinstance(trade_date, datetime.date):
        trade_date = datetime.date.fromisoformat(str(trade_date))
    day = trade_date.isoformat()
    failures = []
    if http_get is None:
        import requests
        http_get = requests.get
    if finmind_token:
        try:
            response = http_get(
                "https://api.finmindtrade.com/api/v4/data",
                params={
                    "dataset": "TaiwanStockPrice", "data_id": symbol,
                    "start_date": day, "end_date": day, "token": finmind_token,
                },
                timeout=10,
            )
            response.raise_for_status()
            records = response.json().get("data", [])
            matches = [row for row in records if str(row.get("date", ""))[:10] == day]
            price = _positive_price(matches[-1].get("close") if matches else None)
            if price is not None:
                return {
                    "price": price, "source": "FinMind TaiwanStockPrice", "date": day,
                    "evidence_hash": _evidence_hash({"symbol": symbol, "date": day, "records": matches}),
                }
            failures.append("FinMind: exact-date close unavailable")
        except Exception as error:
            failures.append(f"FinMind: {type(error).__name__}")

    for market_symbol in yahoo_market_symbols(symbol):
        try:
            response = http_get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{market_symbol}",
                params={"period1": int(datetime.datetime.combine(trade_date, datetime.time.min, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()),
                        "period2": int(datetime.datetime.combine(trade_date + datetime.timedelta(days=1), datetime.time.min, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()),
                        "interval": "1d", "events": "history"},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
            )
            response.raise_for_status()
            result = response.json()["chart"]["result"][0]
            zone = ZoneInfo(result.get("meta", {}).get("exchangeTimezoneName") or "Asia/Taipei")
            timestamps = result.get("timestamp", [])
            closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
            candidates = [
                _positive_price(close)
                for stamp, close in zip(timestamps, closes)
                if datetime.datetime.fromtimestamp(stamp, tz=zone).date() == trade_date
            ]
            price = next((value for value in candidates if value is not None), None)
            if price is not None:
                return {
                    "price": price, "source": f"Yahoo Chart API {market_symbol}", "date": day,
                    "evidence_hash": _evidence_hash({"symbol": market_symbol, "date": day, "result": result}),
                }
            failures.append(f"Yahoo {market_symbol}: exact-date close unavailable")
        except Exception as error:
            failures.append(f"Yahoo {market_symbol}: {type(error).__name__}")

    if ticker_factory is None:
        import yfinance as yf
        ticker_factory = yf.Ticker
    for market_symbol in yahoo_market_symbols(symbol):
        try:
            prices = ticker_factory(market_symbol).history(
                start=day,
                end=(trade_date + datetime.timedelta(days=1)).isoformat(),
                auto_adjust=False,
            )["Close"].dropna()
            if getattr(prices, "empty", False):
                failures.append(f"yfinance {market_symbol}: exact-date close unavailable")
                continue
            for index, value in prices.items():
                indexed_date = index.date() if hasattr(index, "date") else datetime.date.fromisoformat(str(index)[:10])
                if indexed_date == trade_date:
                    price = _positive_price(value)
                    if price is not None:
                        return {
                            "price": price, "source": f"yfinance {market_symbol}", "date": day,
                            "evidence_hash": _evidence_hash({"symbol": market_symbol, "date": day, "close": price}),
                        }
            failures.append(f"yfinance {market_symbol}: exact-date close unavailable")
        except Exception as error:
            failures.append(f"yfinance {market_symbol}: {type(error).__name__}")
    raise QuoteUnavailableError(f"Taiwan close unavailable for {symbol} on {day}; " + "; ".join(failures))
