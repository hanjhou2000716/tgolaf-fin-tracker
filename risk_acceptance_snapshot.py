"""Read the latest Growth portfolio snapshot for a controlled risk acceptance.

This module performs one authenticated PostgREST GET and writes the exact
same-run NAV Beta inputs to a temporary private runner file. It never writes to
Supabase or prints portfolio values.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping

import requests

from supabase_sync import _valid_private_snapshot_payload


def _canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _legacy_rounded_inputs(payload: Mapping[str, Any]) -> dict[str, Any]:
    portfolio = payload.get("portfolio")
    if not isinstance(portfolio, Mapping):
        raise RuntimeError("LATEST_PRIVATE_SNAPSHOT_PORTFOLIO_MISSING")
    tree = portfolio.get("assetTree")
    if not isinstance(tree, Mapping):
        raise RuntimeError("LATEST_PRIVATE_SNAPSHOT_ASSET_TREE_MISSING")
    values: dict[str, float] = {}
    market_by_symbol: dict[str, str] = {}
    cash_twd = cash_usd = funds = 0.0
    leaf_count = 0

    def visit(node: Mapping[str, Any], market: str | None = None, in_fund_group: bool = False) -> None:
        nonlocal cash_twd, cash_usd, funds, leaf_count
        label = str(node.get("label", "")).strip()
        if label == "現貨台股":
            market = "tw"
        elif label == "現貨美股":
            market = "us"
        in_fund_group = in_fund_group or label == "現金與基金"
        children = node.get("children")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, Mapping):
                    visit(child, market, in_fund_group)
            return
        if node.get("kind") != "leaf":
            return
        amount = _finite(node.get("value"))
        if amount is None or amount < 0:
            raise RuntimeError("LATEST_PRIVATE_SNAPSHOT_ASSET_TREE_VALUE_INVALID")
        leaf_count += 1
        asset_class = str(node.get("assetClass", "")).lower()
        if asset_class == "stock":
            symbol = label.upper()
            if not symbol or symbol in values:
                raise RuntimeError("LATEST_PRIVATE_SNAPSHOT_DUPLICATE_OR_EMPTY_SYMBOL")
            values[symbol] = amount
            market_by_symbol[symbol] = market or "other"
        elif asset_class == "cash":
            currency = str(node.get("currency", "")).upper()
            if currency == "TWD":
                cash_twd += amount
            elif currency == "USD":
                # Asset-tree cash value is already denominated in TWD.
                cash_usd += amount
            else:
                raise RuntimeError("LATEST_PRIVATE_SNAPSHOT_CASH_CURRENCY_INVALID")
        elif in_fund_group and label in {"現金", "台幣現金"}:
            # Older asset trees represented the combined cash balance as an
            # untyped leaf. Keep it at Beta 0 rather than misclassifying it as
            # an unmodeled fund position.
            cash_twd += amount
        elif in_fund_group:
            funds += amount

    visit(tree)
    if cash_twd > 0:
        values["CASH_TWD"] = cash_twd
    if cash_usd > 0:
        values["CASH_USD"] = cash_usd
    if funds > 0:
        values["FUND"] = funds
    total_asset = _finite(portfolio.get("totalAsset"))
    total_debt = _finite(portfolio.get("totalDebt"))
    if total_asset is None or total_debt is None:
        raise RuntimeError("LATEST_PRIVATE_SNAPSHOT_TOTALS_INVALID")
    tree_total = _finite(tree.get("value"))
    rounding_bound = max(0.02, leaf_count * 0.005 + 0.01)
    if tree_total is None or abs(tree_total - total_asset) > rounding_bound:
        raise RuntimeError("LATEST_PRIVATE_SNAPSHOT_TREE_TOTAL_MISMATCH")
    inventory = portfolio.get("inventory") or payload.get("inventory")
    if not isinstance(inventory, Mapping):
        raise RuntimeError("LATEST_PRIVATE_SNAPSHOT_INVENTORY_MISSING")
    return {
        "schemaVersion": 1,
        "snapshotAsOf": str(payload.get("generatedAt") or ""),
        "snapshotInputHash": _canonical_hash({"assetTree": tree, "totalAsset": total_asset, "totalDebt": total_debt}),
        "inventory": inventory,
        "assetValuesTwd": values,
        "totalAsset": total_asset,
        "totalDebt": total_debt,
        "marketBySymbol": market_by_symbol,
        "inputPrecision": "rounded-asset-tree-v1",
        "roundingErrorBoundTwd": rounding_bound,
    }


def load_latest_risk_inputs(*, session: Any = None, now: datetime | None = None, max_age: timedelta = timedelta(hours=24)) -> dict[str, Any]:
    base_url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    user_id = os.getenv("SUPABASE_USER_ID", "").strip()
    if not all((base_url, service_key, user_id)):
        raise RuntimeError("PRIVATE_SNAPSHOT_CREDENTIALS_MISSING")

    client = session or requests
    response = client.get(
        f"{base_url}/rest/v1/portfolio_snapshots",
        headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
        params={
            "user_id": f"eq.{user_id}",
            "select": "generated_at,payload",
            "order": "generated_at.desc",
            "limit": "1",
        },
        timeout=20,
    )
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
        raise RuntimeError("LATEST_PRIVATE_SNAPSHOT_MISSING")
    row = rows[0]
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        raise RuntimeError("LATEST_PRIVATE_SNAPSHOT_INVALID")
    if not _valid_private_snapshot_payload(dict(payload)):
        raise RuntimeError("LATEST_PRIVATE_SNAPSHOT_INVALID")

    generated_at = str(row.get("generated_at") or payload.get("generatedAt") or "")
    try:
        generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError("LATEST_PRIVATE_SNAPSHOT_TIMESTAMP_INVALID") from error
    if generated.tzinfo is None:
        raise RuntimeError("LATEST_PRIVATE_SNAPSHOT_TIMESTAMP_INVALID")
    current = now or datetime.now(timezone.utc)
    age = current.astimezone(timezone.utc) - generated.astimezone(timezone.utc)
    if age < timedelta(minutes=-5) or age > max_age:
        raise RuntimeError("LATEST_PRIVATE_SNAPSHOT_STALE")

    risk_inputs = payload.get("riskValidationInputs")
    if not isinstance(risk_inputs, Mapping):
        return _legacy_rounded_inputs(payload)
    if risk_inputs.get("schemaVersion") != 1:
        raise RuntimeError("EXACT_RISK_INPUTS_INVALID")
    canonical = {key: value for key, value in risk_inputs.items() if key != "inputHash"}
    if not isinstance(risk_inputs.get("inputHash"), str) or _canonical_hash(canonical) != risk_inputs.get("inputHash"):
        raise RuntimeError("RISK_INPUT_HASH_MISMATCH")
    required = {"inventory", "assetValuesTwd", "totalAsset", "totalDebt", "marketBySymbol"}
    if not required.issubset(risk_inputs) or not all(isinstance(risk_inputs[key], Mapping) for key in ("inventory", "assetValuesTwd", "marketBySymbol")):
        raise RuntimeError("EXACT_RISK_INPUTS_INVALID")
    total_asset = _finite(risk_inputs.get("totalAsset"))
    total_debt = _finite(risk_inputs.get("totalDebt"))
    values = risk_inputs.get("assetValuesTwd")
    if total_asset is None or total_debt is None or total_asset <= 0 or total_debt < 0:
        raise RuntimeError("EXACT_RISK_TOTALS_INVALID")
    if any(_finite(value) is None or float(value) < 0 for value in values.values()):
        raise RuntimeError("EXACT_RISK_POSITION_VALUE_INVALID")
    portfolio = payload.get("portfolio")
    root_inventory = payload.get("inventory")
    if not isinstance(portfolio, Mapping) or not isinstance(root_inventory, Mapping):
        raise RuntimeError("EXACT_RISK_RECONCILIATION_FIELDS_MISSING")
    portfolio_asset, portfolio_debt = _finite(portfolio.get("totalAsset")), _finite(portfolio.get("totalDebt"))
    if (
        portfolio_asset is None or portfolio_debt is None
        or abs(portfolio_asset - total_asset) > 0.011
        or abs(portfolio_debt - total_debt) > 0.011
        or dict(risk_inputs["inventory"]) != dict(root_inventory)
    ):
        raise RuntimeError("EXACT_RISK_INPUTS_SNAPSHOT_MISMATCH")
    position_total = sum(float(value) for value in values.values())
    if abs(position_total - total_asset) > 0.011:
        raise RuntimeError("EXACT_RISK_POSITION_TOTAL_MISMATCH")
    return {
        "schemaVersion": 1,
        "snapshotAsOf": generated.astimezone(timezone.utc).isoformat(),
        "snapshotInputHash": risk_inputs["inputHash"],
        "inventory": risk_inputs["inventory"],
        "assetValuesTwd": risk_inputs["assetValuesTwd"],
        "totalAsset": total_asset,
        "totalDebt": total_debt,
        "marketBySymbol": risk_inputs["marketBySymbol"],
        "inputPrecision": "exact-runtime-inputs-v1",
        "roundingErrorBoundTwd": 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="Temporary private runner path; never upload this file")
    args = parser.parse_args()
    snapshot = load_latest_risk_inputs()
    path = Path(args.output)
    if path.exists():
        parser.error("output exists; use a fresh private path")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    print(json.dumps({"status": "SNAPSHOT_READY", "snapshotInputHash": snapshot["snapshotInputHash"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
