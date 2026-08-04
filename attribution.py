"""Daily net-asset change attribution with an explicit reconciliation residual."""


def build_pnl_attribution(
    previous_net_asset,
    current_net_asset,
    previous_categories=None,
    current_categories=None,
    *,
    income=0,
    expenses=0,
    financing_cash_flow=0,
    external_cash_flow=0,
):
    previous_categories = previous_categories or {}
    current_categories = current_categories or {}
    tw = float(current_categories.get("TW_Stock_Value", 0)) - float(previous_categories.get("TW_Stock_Value", 0))
    us = float(current_categories.get("US_Stock_Value", 0)) - float(previous_categories.get("US_Stock_Value", 0))
    # FX is isolated only when USD holdings and TWD conversion are available;
    # otherwise the residual remains explicit instead of being guessed.
    fx = float(current_categories.get("FX_Contribution", 0))
    dividends = float(income)
    fees = float(expenses)
    interest = float(financing_cash_flow)
    external = float(external_cash_flow)
    net_change = float(current_net_asset) - float(previous_net_asset)
    known = tw + us + fx + dividends + fees + interest + external
    other = net_change - known
    items = {
        "twStockPrice": round(tw, 2),
        "usStockPrice": round(us, 2),
        "fx": round(fx, 2),
        "dividends": round(dividends, 2),
        "fees": round(fees, 2),
        "pledgeInterest": round(interest, 2),
        "externalCashFlow": round(external, 2),
        "other": round(other, 2),
    }
    items["netChange"] = round(net_change, 2)
    items["reconciled"] = round(sum(value for key, value in items.items() if key not in {"netChange", "reconciled"}), 2) == items["netChange"]
    return items
