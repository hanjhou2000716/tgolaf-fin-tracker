"""Read-only quarterly-risk parameter acceptance from a trusted snapshot.

This entry point never calls the ledger, Supabase write paths, Telegram, or
Pages deployment. It stages policy state in a temporary directory and writes
only a sanitized acceptance summary to the explicitly supplied private path.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import shutil
import tempfile
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from beta_policy import AUTO_VALIDATION_ALGORITHM, load_active_beta_policy, load_active_kelly_policy
from quarterly_risk_policy import FIXED_BETAS, ensure_quarterly_risk_policies, promote_beta_candidate_if_qualified
from risk import calculate_nav_beta


def _read_json(path: str | Path) -> Any:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value


def _is_finite_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _precision_reasons(snapshot: Mapping[str, Any], calculation: Mapping[str, Any]) -> list[str]:
    """Fail closed when rounded legacy inputs cannot settle a risk boundary."""
    def number(value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed if math.isfinite(parsed) else None

    precision = snapshot.get("inputPrecision", "exact-runtime-inputs-v1")
    if precision == "exact-runtime-inputs-v1":
        return []
    if precision != "rounded-asset-tree-v1":
        return ["INPUT_PRECISION_UNSUPPORTED"]
    bound = number(snapshot.get("roundingErrorBoundTwd"))
    if bound is None or bound < 0:
        return ["ROUNDING_BOUND_INVALID"]
    total_asset = number(snapshot.get("totalAsset"))
    total_debt = number(snapshot.get("totalDebt"))
    if total_asset is None or total_debt is None or total_asset <= total_debt:
        return ["ROUNDED_NAV_INVALID"]
    nav = total_asset - total_debt
    reasons: list[str] = []
    threshold = nav * 0.01
    for item in calculation.get("missing", []):
        if not isinstance(item, Mapping):
            continue
        value = number(item.get("valueTwd"))
        if value is not None and abs(value - threshold) <= bound:
            reasons.append("MATERIALITY_BOUNDARY_AMBIGUOUS")
            break
    values = snapshot.get("assetValuesTwd", {})
    cash = {"CASH", "CASH_TWD", "CASH_USD", "現金", "現金_TWD", "現金_USD"}
    risk_asset_value = sum(
        value for symbol, raw in values.items()
        if str(symbol).strip().upper() not in cash
        and (value := number(raw)) is not None
    ) if isinstance(values, Mapping) else 0.0
    coverage = number(calculation.get("coveragePct"))
    if coverage is not None and risk_asset_value > 0:
        coverage_uncertainty_pct = 2 * bound / risk_asset_value * 100
        if abs(coverage - 95.0) <= coverage_uncertainty_pct:
            reasons.append("COVERAGE_BOUNDARY_AMBIGUOUS")
    return reasons


def validate_snapshot_readonly(
    snapshot: Mapping[str, Any],
    *,
    state_dir: str | Path,
    today: date,
    now: datetime | None = None,
    fetcher: Any = None,
) -> dict[str, Any]:
    """Rebuild/activate quarterly policies on copied state without side effects."""
    required = {"inventory", "assetValuesTwd", "totalAsset", "totalDebt", "marketBySymbol"}
    missing = sorted(required.difference(snapshot))
    if missing:
        return {"status": "INVALID_SNAPSHOT", "missingFields": missing}
    if not all(isinstance(snapshot[key], Mapping) for key in ("inventory", "assetValuesTwd", "marketBySymbol")):
        return {"status": "INVALID_SNAPSHOT", "reasonCode": "snapshot_mapping_fields_invalid"}

    source_state = Path(state_dir)
    run_now = now or datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="quarterly-risk-readonly-") as temporary:
        root = Path(temporary)
        staged_state = root / "state"
        if source_state.exists():
            shutil.copytree(source_state, staged_state, dirs_exist_ok=True)
        else:
            staged_state.mkdir(parents=True)
        candidate_dir = root / "candidates"
        kwargs: dict[str, Any] = {
            "state_dir": staged_state,
            "candidate_dir": candidate_dir,
            "today": today,
            "now": run_now,
        }
        if fetcher is not None:
            kwargs["fetcher"] = fetcher
        prepared = ensure_quarterly_risk_policies(snapshot["inventory"], **kwargs)
        activation = promote_beta_candidate_if_qualified(
            snapshot["assetValuesTwd"],
            float(snapshot["totalAsset"]),
            float(snapshot["totalDebt"]),
            market_by_symbol=snapshot["marketBySymbol"],
            state_dir=staged_state,
            today=today,
            now=run_now,
        )
        cold_candidate = _read_json(candidate_dir / "beta-policy-candidate.json")
        restored_state = root / "restored-state"
        shutil.copytree(staged_state, restored_state, dirs_exist_ok=True)
        restored_candidate_dir = root / "restored-candidates"
        restored_kwargs = {
            **kwargs,
            "state_dir": restored_state,
            "candidate_dir": restored_candidate_dir,
        }
        restored_prepared = ensure_quarterly_risk_policies(snapshot["inventory"], **restored_kwargs)
        restored_activation = promote_beta_candidate_if_qualified(
            snapshot["assetValuesTwd"],
            float(snapshot["totalAsset"]),
            float(snapshot["totalDebt"]),
            market_by_symbol=snapshot["marketBySymbol"],
            state_dir=restored_state,
            today=today,
            now=run_now,
        )
        restored_candidate = (
            _read_json(restored_candidate_dir / "beta-policy-candidate.json")
            or _read_json(restored_state / "beta-policy-candidate.json")
        )
        restoration_matches = bool(
            isinstance(cold_candidate, Mapping)
            and isinstance(restored_candidate, Mapping)
            and cold_candidate.get("inputHash") == restored_candidate.get("inputHash")
            and cold_candidate.get("algorithmVersion") == restored_candidate.get("algorithmVersion") == AUTO_VALIDATION_ALGORITHM
        )
        beta_policy = load_active_beta_policy(restored_state / "beta-policy-active.json", as_of=today)
        kelly_policy = load_active_kelly_policy(restored_state / "kelly-policy-active.json", as_of=today)
        prepared = restored_prepared
        activation = restored_activation
        beta_summary = prepared.get("beta", {})
        kelly_summary = prepared.get("kelly", {})
        candidate_document = _read_json(candidate_dir / "beta-policy-candidate.json")
        candidate_assets = candidate_document.get("assets", {}) if isinstance(candidate_document, Mapping) else {}
        candidate_betas = {
            str(symbol): float(record["beta"])
            for symbol, record in candidate_assets.items()
            if isinstance(record, Mapping) and record.get("status") == "CANDIDATE"
            and _is_finite_number(record.get("beta"))
        } if isinstance(candidate_assets, Mapping) else {}
        candidate_calculation = calculate_nav_beta(
            snapshot["assetValuesTwd"],
            float(snapshot["totalAsset"]),
            float(snapshot["totalDebt"]),
            {**FIXED_BETAS, **candidate_betas},
            market_by_symbol=snapshot["marketBySymbol"],
        )
        beta_values = beta_policy.get("betas", {}) if beta_policy.get("status") == "READY" else {}
        beta_calculation = calculate_nav_beta(
            snapshot["assetValuesTwd"],
            float(snapshot["totalAsset"]),
            float(snapshot["totalDebt"]),
            {**FIXED_BETAS, **beta_values},
            market_by_symbol=snapshot["marketBySymbol"],
        )
        candidate_coverage = candidate_calculation.get("coveragePct")
        precision_reasons = _precision_reasons(snapshot, candidate_calculation)
        precision_ok = not precision_reasons
        kelly_values = kelly_policy.get("policy") if kelly_policy.get("status") == "READY" else None
        kelly_is_current_auto = bool(
            isinstance(kelly_values, Mapping)
            and kelly_values.get("approvalStatus") == "AUTO_VALIDATED"
            and kelly_values.get("effectiveFromQuarter") == prepared.get("effectiveQuarter")
        )
        beta_is_current_formal = bool(
            beta_policy.get("status") == "READY"
            and beta_policy.get("metadata", {}).get("approvalStatus") == "AUTO_VALIDATED"
            and beta_policy.get("metadata", {}).get("effectiveFromQuarter") == prepared.get("effectiveQuarter")
            and beta_calculation.get("status") == "READY"
            and precision_ok
        )
        unresolved = beta_summary.get("symbolsUnresolved", [])
        failed_symbols = beta_summary.get("failedSymbols", [])
        remaining_missing = sorted({
            str(item.get("symbol")) for item in failed_symbols
            if isinstance(item, Mapping) and item.get("symbol")
        } | {str(symbol) for symbol in unresolved if symbol})
        result = {
            "schemaVersion": 1,
            "status": "PASS" if beta_is_current_formal and kelly_is_current_auto and restoration_matches else "BLOCKED",
            "algorithmVersion": AUTO_VALIDATION_ALGORITHM,
            "effectiveQuarter": prepared.get("effectiveQuarter"),
            "dataCutoff": prepared.get("dataCutoff"),
            "completedWeekCutoff": prepared.get("completedWeekCutoff"),
            "beta": {
                "candidateStatus": beta_summary.get("status"),
                "activation": activation,
                "formalStatus": beta_policy.get("status"),
                "policyVersion": beta_policy.get("metadata", {}).get("policyVersion") if isinstance(beta_policy.get("metadata"), Mapping) else None,
                "navBeta": beta_calculation.get("navBeta") if beta_is_current_formal else None,
                "candidateCoveragePct": candidate_coverage,
                "formalCoveragePct": beta_calculation.get("coveragePct") if beta_policy.get("status") == "READY" else None,
                "coveragePct": beta_calculation.get("coveragePct") if beta_is_current_formal else candidate_coverage,
                "candidateMissingSymbols": candidate_calculation.get("missing", []),
                "missingSymbols": beta_calculation.get("missing", []) if not beta_is_current_formal else [],
                "unresolvedSymbols": unresolved,
                "failedSymbols": failed_symbols,
                "remainingUnmodeledSymbols": remaining_missing,
                "precisionStatus": "PASS" if precision_ok else "BLOCKED",
                "precisionReasons": precision_reasons,
                "coldCandidateInputHash": cold_candidate.get("inputHash") if isinstance(cold_candidate, Mapping) else None,
                "restoredCandidateInputHash": restored_candidate.get("inputHash") if isinstance(restored_candidate, Mapping) else None,
                "restorationStatus": "PASS" if restoration_matches else "BLOCKED",
            },
            "kelly": {
                "status": "AUTO_VALIDATED" if kelly_is_current_auto else kelly_policy.get("status", kelly_summary.get("status")),
                "reasonCode": kelly_summary.get("reasonCode") or kelly_policy.get("reason"),
                "policyVersion": kelly_values.get("activeVersion") if isinstance(kelly_values, Mapping) else kelly_summary.get("policyVersion"),
                "halfKellyLimit": kelly_values.get("halfKellyLimit") if isinstance(kelly_values, Mapping) else None,
                "dataCutoff": kelly_values.get("dataCutoff") if isinstance(kelly_values, Mapping) else kelly_summary.get("dataCutoff"),
            },
            "sourceFailures": prepared.get("sourceFailures", []),
            "sideEffects": {"ledger": False, "supabaseWrites": False, "telegram": False, "pagesDeploy": False},
        }
        return deepcopy(result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, help="Private JSON snapshot containing the required validation inputs")
    parser.add_argument("--state-dir", required=True, help="Trusted quarterly-risk artifact restored from a successful main run")
    parser.add_argument("--output", required=True, help="Private output path; existing files are never overwritten")
    taipei_today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    parser.add_argument("--as-of", default=taipei_today.isoformat(), help="Taiwan policy date in YYYY-MM-DD format")
    parser.add_argument("--expected-effective-quarter", help="Fail closed unless the candidate is for this quarter, e.g. 2026Q4")
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        parser.error("output already exists; choose a new private evidence path")
    stage = "READ_INPUT"
    try:
        snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
        as_of = date.fromisoformat(args.as_of)
        if not isinstance(snapshot, Mapping):
            raise ValueError("snapshot root must be an object")
        stage = "POLICY_VALIDATION"
        result = validate_snapshot_readonly(snapshot, state_dir=args.state_dir, today=as_of)
        if args.expected_effective_quarter and result.get("effectiveQuarter") != args.expected_effective_quarter:
            result["status"] = "BLOCKED"
            result["reasonCode"] = "EFFECTIVE_QUARTER_MISMATCH"
    except Exception as error:  # noqa: BLE001 - report only a safe type, never private exception text
        result = {
            "status": "VALIDATION_FAILED",
            "failureStage": stage,
            "reasonCode": type(error).__name__,
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return 0 if result.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
