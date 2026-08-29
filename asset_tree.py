"""Pure portfolio hierarchy helpers used by the Growth treemap."""

import math


def _quantity(value):
    """Return a positive finite quantity, preserving fractional shares."""
    try:
        quantity = float(value)
    except (TypeError, ValueError):
        return None
    return quantity if math.isfinite(quantity) and quantity > 0 else None


def _leaf(label, value, category, *, shares=None, pledged_shares=None):
    value = max(0.0, float(value or 0))
    leaf = {"label": str(label), "value": round(value, 2), "kind": "leaf", "category": category}
    quantity = _quantity(shares)
    if quantity is not None:
        leaf["shares"] = int(quantity) if quantity.is_integer() else quantity
    pledged = _quantity(pledged_shares)
    if pledged is not None:
        leaf["pledgedShares"] = int(pledged) if pledged.is_integer() else pledged
    return leaf


def _group(label, children, category=None):
    children = [child for child in children if child["value"] > 0]
    value = round(sum(child["value"] for child in children), 2)
    return {
        "label": label,
        "value": value,
        "kind": "group",
        "category": category or label,
        "children": children,
    }


def build_asset_tree(
    tw_positions,
    us_positions,
    cash_value,
    fund_positions,
    *,
    tw_shares=None,
    us_shares=None,
    pledged_shares=None,
):
    """Build a gross-asset hierarchy; liabilities are intentionally excluded.

    Values are expected to be converted to TWD before calling this function.
    The returned root value is the sum of its visible children, making the
    treemap denominator explicit and preventing debt from being rendered as an
    asset tile.
    """
    tw_positions = {str(key): float(value or 0) for key, value in (tw_positions or {}).items()}
    us_positions = {str(key): float(value or 0) for key, value in (us_positions or {}).items()}
    tw_shares = {str(key): value for key, value in (tw_shares or {}).items() if key != "History"}
    us_shares = {str(key): value for key, value in (us_shares or {}).items() if key != "History"}
    pledged_shares = {str(key): value for key, value in (pledged_shares or {}).items() if key != "History"}
    fund_positions = {str(key): float(value or 0) for key, value in (fund_positions or {}).items() if key != "History"}

    tw_market_etfs = {"006208"}
    tw_tsmc = {"2330"}
    tw_leveraged = {"00685L"}
    us_market_etfs = {"QQQM", "QQQ", "SPYG", "VOO", "VTI"}
    us_tsm = {"TSM"}

    tw_groups = {
        "台股市值型": [],
        "台積電": [],
        "台股槓桿型": [],
        "其它台股": [],
    }
    for symbol, value in tw_positions.items():
        if symbol in tw_market_etfs:
            group = "台股市值型"
        elif symbol in tw_tsmc:
            group = "台積電"
        elif symbol in tw_leveraged:
            group = "台股槓桿型"
        else:
            group = "其它台股"
        tw_groups[group].append(
            _leaf(
                symbol,
                value,
                group,
                shares=tw_shares.get(symbol),
                pledged_shares=pledged_shares.get(symbol),
            )
        )

    us_groups = {
        "美股市值型": [],
        "台積電 ADR": [],
        "其它美股": [],
    }
    for symbol, value in us_positions.items():
        if symbol in us_market_etfs:
            group = "美股市值型"
        elif symbol in us_tsm:
            group = "台積電 ADR"
        else:
            group = "其它美股"
        us_groups[group].append(_leaf(symbol, value, group, shares=us_shares.get(symbol)))

    tw_children = [_group(label, children, "現貨台股") for label, children in tw_groups.items() if children]
    us_children = [_group(label, children, "現貨美股") for label, children in us_groups.items() if children]
    cash_children = []
    if float(cash_value or 0) > 0:
        cash_children.append(_leaf("現金", cash_value, "現金與基金"))
    cash_children.extend(_leaf(label, value, "現金與基金") for label, value in fund_positions.items() if value > 0)

    children = []
    if tw_children:
        children.append(_group("現貨台股", tw_children, "現貨台股"))
    if us_children:
        children.append(_group("現貨美股", us_children, "現貨美股"))
    if cash_children:
        children.append(_group("現金與基金", cash_children, "現金與基金"))

    return {
        "label": "總資產",
        "value": round(sum(child["value"] for child in children), 2),
        "kind": "root",
        "category": "總資產",
        "children": children,
    }
