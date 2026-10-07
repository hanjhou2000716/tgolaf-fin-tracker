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
from pathlib import Path
import shutil
import tempfile
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from beta_policy import AUTO_VALIDATION_ALGORITHM, load_active_beta_policy, load_active_kelly_policy
from quarterly_risk_policy import FIXED_BETAS, ensure_quarterly_risk_policies, promote_beta_candidate_if_qualified
from risk import calculate_nav_beta


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
        beta_policy = load_active_beta_policy(staged_state / "beta-policy-active.json", as_of=today)
        kelly_policy = load_active_kelly_policy(staged_state / "kelly-policy-active.json", as_of=today)
        beta_summary = prepared.get("beta", {})
        kelly_summary = prepared.get("kelly", {})
        beta_values = beta_policy.get("betas", {}) if beta_policy.get("status") == "READY" else {}
        beta_calculation = calculate_nav_beta(
            snapshot["assetValuesTwd"],
            float(snapshot["totalAsset"]),
            float(snapshot["totalDebt"]),
            {**FIXED_BETAS, **beta_values},
            market_by_symbol=snapshot["marketBySymbol"],
        )
        candidate_coverage = beta_calculation.get("coveragePct")
        if candidate_coverage is None and isinstance(activation, Mapping):
            candidate_coverage = activation.get("coveragePct")
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
        )
        unresolved = beta_summary.get("symbolsUnresolved", [])
        failed_symbols = beta_summary.get("failedSymbols", [])
        remaining_missing = sorted({
            str(item.get("symbol")) for item in failed_symbols
            if isinstance(item, Mapping) and item.get("symbol")
        } | {str(symbol) for symbol in unresolved if symbol})
        result = {
            "schemaVersion": 1,
            "status": "PASS" if beta_is_current_formal and kelly_is_current_auto else "BLOCKED",
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
                "coveragePct": beta_calculation.get("coveragePct") if beta_is_current_formal else candidate_coverage,
                "missingSymbols": beta_calculation.get("missing", []) if not beta_is_current_formal else [],
                "unresolvedSymbols": unresolved,
                "failedSymbols": failed_symbols,
                "remainingUnmodeledSymbols": remaining_missing,
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
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        parser.error("output already exists; choose a new private evidence path")
    try:
        snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
        as_of = date.fromisoformat(args.as_of)
        if not isinstance(snapshot, Mapping):
            raise ValueError("snapshot root must be an object")
        result = validate_snapshot_readonly(snapshot, state_dir=args.state_dir, today=as_of)
    except (OSError, ValueError, TypeError) as error:
        result = {"status": "VALIDATION_FAILED", "reasonCode": type(error).__name__}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return 0 if result.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
