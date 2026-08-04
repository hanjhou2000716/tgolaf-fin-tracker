"""Direct and look-through exposure aggregation."""


def build_exposure_matrix(positions, *, total_asset, etf_lookthrough=None, metadata=None):
    """Aggregate company/market/currency/issuer exposures without double count.

    ``positions`` contains market values by held symbol. ETF look-through maps
    an ETF to constituent weights (0..1). Missing metadata is represented as
    ``unknown`` rather than inferred silently.
    """
    total_asset = float(total_asset)
    if total_asset <= 0:
        return {key: {} for key in ("company", "market", "currency", "issuer")}
    etf_lookthrough = etf_lookthrough or {}
    metadata = metadata or {}
    company = {}
    market = {}
    currency = {}
    issuer = {}

    def add(target, key, value):
        target[key] = target.get(key, 0.0) + float(value)

    for symbol, raw_value in positions.items():
        value = float(raw_value)
        if value <= 0:
            continue
        info = metadata.get(symbol, {})
        market_name = info.get("market", "unknown")
        currency_name = info.get("currency", "unknown")
        issuer_name = info.get("issuer", symbol)
        add(market, market_name, value)
        add(currency, currency_name, value)
        add(issuer, issuer_name, value)
        constituents = etf_lookthrough.get(symbol, {})
        if constituents:
            for constituent, weight in constituents.items():
                add(company, constituent, value * float(weight))
        else:
            add(company, symbol, value)

    def normalize(values):
        return {
            key: {"value": round(value, 2), "percent": round(value / total_asset * 100, 2)}
            for key, value in sorted(values.items(), key=lambda item: item[1], reverse=True)
        }

    return {
        "company": normalize(company),
        "market": normalize(market),
        "currency": normalize(currency),
        "issuer": normalize(issuer),
    }
