"""Upload the private snapshot to Supabase without exposing service credentials."""

import json
import hashlib
import math
import os
from collections import Counter
from pathlib import Path

import requests
from ledger import transaction_payload


class TransactionSyncResult(str):
    """String-compatible sync result with row-level quarantine details.

    Existing callers historically compared the return value with strings such
    as ``"uploaded"``.  Keeping this as a ``str`` subclass preserves that
    contract while allowing the pipeline to expose immutable conflicts as
    ingestion health details instead of aborting the whole build.
    """

    def __new__(cls, status: str, *, conflicts=(), replays=(), derived_price_replays=(), conflict_report=(), conflict_summary=None, uploaded=0, unchanged=0):
        instance = super().__new__(cls, status)
        instance.status = status
        instance.conflicts = tuple(conflicts)
        instance.replays = tuple(replays)
        instance.derived_price_replays = tuple(derived_price_replays)
        instance.conflict_report = tuple(conflict_report)
        instance.conflict_summary = dict(conflict_summary or {})
        instance.uploaded = int(uploaded)
        instance.unchanged = int(unchanged)
        return instance


_FINGERPRINT_KEYS = (
    "submitted_at", "transaction_date", "asset_type", "symbol", "action",
    "quantity", "unit", "currency", "price", "reversal_of",
)
_CORE_KEYS = (
    "transaction_date", "asset_type", "symbol", "action", "quantity",
    "unit", "currency", "price", "reversal_of",
)
_METADATA_KEYS = (
    "submitted_at", "submitter_email", "approved", "source_row_id",
    "compatibility_used", "reconciliation_delta", "source_fingerprint",
    "source_fingerprint_version",
)
_LEGACY_MARKERS = frozenset({
    "legacy_target_from_price_field",
    "legacy_mixed_form_row",
})
_FINGERPRINT_VERSION = "2"


def _is_derived_price(payload: dict) -> bool:
    """Return whether price was supplied by settlement quote enrichment."""
    marker = str(payload.get("compatibility_used") or "").strip().lower()
    return marker.startswith("settlement_quote_estimate:")


def _marker_class(value) -> str:
    """Return a non-sensitive compatibility category for diagnostics."""
    marker = str(value or "").strip().lower()
    if not marker:
        return "none"
    if marker.startswith("settlement_quote_estimate:"):
        return "settlement_quote_estimate"
    if marker.startswith("settlement_quote_pending:"):
        return "settlement_quote_pending"
    if marker in _LEGACY_MARKERS:
        return marker
    return "other"


def _normalise_value(value):
    """Canonicalise payload values without changing their financial meaning."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).strip()
    # Decimal serialisation is deliberately normalised so 100 and 100.0 are
    # treated as the same replay, while non-numeric values remain exact.
    try:
        from decimal import Decimal
        number = Decimal(text.replace(",", ""))
        if number.is_finite():
            normalised = format(number, "f")
            if "." in normalised:
                normalised = normalised.rstrip("0").rstrip(".")
            return normalised or "0"
    except Exception:
        pass
    return text


def _canonical_event(payload: dict, *, include_submitted_at=True, include_price=True) -> dict:
    keys = _FINGERPRINT_KEYS if include_submitted_at else _CORE_KEYS
    if not include_price:
        keys = tuple(key for key in keys if key != "price")
    event = {key: _normalise_value(payload.get(key)) for key in keys}
    event["price_mode"] = "derived" if _is_derived_price(payload) else "explicit"
    return event


def transaction_fingerprint(payload: dict) -> str:
    """Return a stable identity for one financial event.

    Source row numbers and transaction UUIDs are intentionally excluded. This
    lets a reordered Sheet row be recognised as a replay while the timestamp
    and financial fields still distinguish two otherwise identical trades.
    """
    existing = payload.get("source_fingerprint")
    if existing and str(payload.get("source_fingerprint_version")) == _FINGERPRINT_VERSION:
        return str(existing)
    canonical = _canonical_event(payload, include_price=not _is_derived_price(payload))
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _changed_fields(previous: dict, current: dict) -> list[str]:
    """List immutable financial fields changed by a candidate replay."""
    changed = []
    for key in _CORE_KEYS:
        if _normalise_value(previous.get(key)) != _normalise_value(current.get(key)):
            changed.append(key)
    return changed


def _replay_record(payload: dict, previous_id: str | None = None) -> dict:
    return {
        "transaction_id": str(payload.get("transaction_id") or ""),
        "source_row_id": str(payload.get("source_row_id") or ""),
        "matched_existing_transaction_id": str(previous_id or payload.get("transaction_id") or ""),
        "classification": "REPLAY",
        "reason": "same_financial_event",
    }


def _derived_price_replay(previous: dict, current: dict) -> bool:
    """Allow only system-estimate price changes to replay safely."""
    if not _is_derived_price(current) or not _is_derived_price(previous):
        return False
    return _canonical_event(previous, include_price=False) == _canonical_event(current, include_price=False)


def _legacy_mixed_derived_price_replay(previous: dict, current: dict) -> bool:
    """Recognise prices enriched on legacy mixed-form trade rows.

    The legacy mixed-form parser intentionally emits BUY/SELL rows without a
    price.  Settlement enrichment then attaches a quote while preserving the
    compatibility marker, so those payloads still carry
    ``legacy_mixed_form_row`` instead of the newer settlement marker.  Only a
    price-only change on an otherwise identical trade is eligible here; all
    other core-field changes continue through the strict conflict path.
    """
    if previous.get("compatibility_used") != "legacy_mixed_form_row":
        return False
    if current.get("compatibility_used") != "legacy_mixed_form_row":
        return False
    if str(previous.get("action") or "") not in {"BUY", "SELL", "買入", "賣出"}:
        return False
    if previous.get("price") in (None, "") or current.get("price") in (None, ""):
        return False
    if _normalise_value(previous.get("price")) == _normalise_value(current.get("price")):
        return False
    changed = _changed_fields(previous, current)
    return changed == ["price"]


def _metadata_only_replay(previous: dict, current: dict) -> bool:
    """Accept non-financial serialization changes without weakening core guards.

    This is intentionally checked only after an existing transaction ID has
    matched.  A source-row reorder still goes through the fingerprint and
    legacy matching paths, so two legitimate identical trades are not merged
    merely because their financial fields happen to match.
    """
    if not previous or _is_derived_price(previous) != _is_derived_price(current):
        return False
    return not _changed_fields(previous, current)


def _metadata_changed_fields(previous: dict, current: dict) -> list[str]:
    return [
        key for key in _METADATA_KEYS
        if _normalise_value(previous.get(key)) != _normalise_value(current.get(key))
    ]


def _conflict_record(payload: dict, previous: dict | None = None, *, matched_id: str | None = None) -> dict:
    previous = previous or {}
    return {
        "transaction_id": str(payload.get("transaction_id") or ""),
        "source_row_id": str(payload.get("source_row_id") or ""),
        "reason": "immutable_ledger_conflict",
        "detail": "既有 Supabase immutable ledger payload 與本次解析結果不同；已隔離，未覆寫歷史事件。",
        "classification": "CONFLICT",
        "matched_existing_transaction_id": str(matched_id or payload.get("transaction_id") or ""),
        "changed_fields": _changed_fields(previous, payload),
        "metadata_changed_fields": _metadata_changed_fields(previous, payload),
        "source_row_id_changed": _normalise_value(previous.get("source_row_id")) != _normalise_value(payload.get("source_row_id")),
        "compatibility_marker_previous": _marker_class(previous.get("compatibility_used")),
        "compatibility_marker_current": _marker_class(payload.get("compatibility_used")),
        "existing_payload": previous,
        "current_payload": payload,
    }


def _conflict_summary(conflicts, replays=()) -> dict:
    """Summarize conflicts without exposing private financial values."""
    field_counts = Counter()
    marker_pairs = Counter()
    metadata_only = 0
    source_row_drifts = 0
    for item in conflicts:
        changed = tuple(sorted(str(field) for field in (item.get("changed_fields") or [])))
        if not changed:
            metadata_only += 1
        for field in changed:
            field_counts[field] += 1
        if item.get("source_row_id_changed"):
            source_row_drifts += 1
        previous_marker = item.get("compatibility_marker_previous") or _marker_class(
            (item.get("existing_payload") or {}).get("compatibility_used")
        )
        current_marker = item.get("compatibility_marker_current") or _marker_class(
            (item.get("current_payload") or {}).get("compatibility_used")
        )
        marker_pairs[f"{previous_marker}->{current_marker}"] += 1
    replay_classes = Counter(str(item.get("classification") or "REPLAY") for item in replays)
    return {
        "conflictCount": len(conflicts),
        "transactionConflictCount": len(conflicts),
        "coreConflictCount": len(conflicts) - metadata_only,
        "metadataOnlyConflictCount": metadata_only,
        "metadataReplayCount": replay_classes.get("REPLAY_METADATA", 0),
        "sourceFingerprintReplayCount": replay_classes.get("REPLAY_SOURCE_FINGERPRINT", 0),
        "derivedPriceReplayCount": replay_classes.get("REPLAY_DERIVED_PRICE", 0),
        "sourceRowIdDriftCount": source_row_drifts,
        "changedFieldCounts": dict(sorted(field_counts.items())),
        "compatibilityMarkerPairs": dict(sorted(marker_pairs.items())),
    }


def _same_legacy_reconciliation(previous: dict, current: dict) -> bool:
    """Allow a safe replay after the Form V2 compatibility migration.

    The first production run may have persisted a legacy cash correction with
    ``legacy_target_from_price_field``.  The V2 parser emits the same financial
    event with the real submitter and without that compatibility marker.  The
    ledger remains immutable: this helper only accepts the replay when every
    financial/source field is identical and never mutates the stored row.
    """
    # Legacy mixed-form snapshot rows (including securities, funds, and
    # pledge debt) were historically serialized with a derived adjustment.
    # The compatibility marker is the allow-list boundary; ordinary UUID
    # conflicts must still fail closed.
    legacy_markers = _LEGACY_MARKERS
    if previous.get("compatibility_used") not in legacy_markers:
        return False
    if current.get("compatibility_used") not in legacy_markers | {None}:
        return False
    stable_keys = (
        "source_row_id", "action", "symbol", "currency", "asset_type", "unit",
        "quantity", "price", "reversal_of", "transaction_date",
    )
    for key in stable_keys:
        before, after = previous.get(key), current.get(key)
        if key in {"quantity", "price"}:
            try:
                from decimal import Decimal
                if Decimal(str(before)) != Decimal(str(after)):
                    return False
                continue
            except Exception:
                pass
        if before != after:
            return False
    # ``reconciliation_delta`` is derived from the replay state immediately
    # before this command.  It can legitimately change when a migration
    # restores historical deposits/withdrawals that were previously omitted
    # from the legacy inventory stream.  It must therefore not turn an
    # otherwise identical immutable command into a UUID conflict.  The
    # immutable facts above (source, action, cash target, currency and date)
    # remain strict; the current run records its own derived adjustment in the
    # audit payload without mutating the stored transaction row.
    return True


def _finite_json(value):
    """Convert non-finite floats to JSON null before external writes."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _finite_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite_json(item) for item in value]
    return value


def _required_config():
    return {
        "url": os.getenv("SUPABASE_URL", "").strip().rstrip("/"),
        "service_role_key": os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip(),
        "user_id": os.getenv("SUPABASE_USER_ID", "").strip(),
    }


def load_goal_state(*, session=None) -> dict | None:
    """Load the private monotonic Goal Ladder state through the service boundary."""
    config = _required_config()
    if not all(config.values()):
        return None
    headers = {"apikey": config["service_role_key"], "Authorization": f"Bearer {config['service_role_key']}"}
    http = session or requests
    response = http.get(
        f"{config['url']}/rest/v1/goal_ladder_states",
        headers=headers,
        params={"user_id": f"eq.{config['user_id']}", "select": "state", "limit": "1"},
        timeout=20,
    )
    if getattr(response, "status_code", 200) == 404:
        required = os.getenv("SUPABASE_PRIVATE_SYNC_REQUIRED", "false").lower() in {"1", "true", "yes", "on"}
        if required:
            response.raise_for_status()
        print("Supabase goal state table is not migrated; using initial Goal Ladder state")
        return None
    response.raise_for_status()
    rows = response.json()
    return rows[0].get("state") if rows else None


def save_goal_state(state: dict, *, session=None) -> str:
    """Upsert private Goal state; service role never reaches browser code."""
    config = _required_config()
    required = os.getenv("SUPABASE_PRIVATE_SYNC_REQUIRED", "false").lower() in {"1", "true", "yes", "on"}
    if not all(config.values()):
        if required:
            raise RuntimeError("Supabase goal state sync is required but credentials are missing")
        return "skipped"
    headers = {
        "apikey": config["service_role_key"],
        "Authorization": f"Bearer {config['service_role_key']}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    http = session or requests
    response = http.post(
        f"{config['url']}/rest/v1/goal_ladder_states?on_conflict=user_id",
        headers=headers,
        json={"user_id": config["user_id"], "state": _finite_json(state)},
        timeout=20,
    )
    if getattr(response, "status_code", 200) == 404:
        if required:
            response.raise_for_status()
        print("Supabase goal state table is not migrated; state persistence skipped")
        return "skipped"
    response.raise_for_status()
    return "uploaded"


def upload_private_snapshot(path: str, *, session=None) -> str:
    """Upsert one private snapshot; return ``uploaded`` or ``skipped``.

    The default is non-blocking so the public Demo can still be built before
    Supabase secrets are configured. Set SUPABASE_PRIVATE_SYNC_REQUIRED=true
    in production to fail closed when the private sync is unavailable.
    """
    config = _required_config()
    configured = all(config.values())
    required = os.getenv("SUPABASE_PRIVATE_SYNC_REQUIRED", "false").lower() in {"1", "true", "yes", "on"}
    if not configured:
        if required:
            raise RuntimeError("Supabase private sync is required but SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, or SUPABASE_USER_ID is missing")
        print("Supabase private sync skipped; credentials are not configured")
        return "skipped"

    snapshot = _finite_json(json.loads(Path(path).read_text(encoding="utf-8")))
    generated_at = snapshot.get("generatedAt")
    if not generated_at:
        raise ValueError("Private snapshot is missing generatedAt")
    body = {"user_id": config["user_id"], "generated_at": generated_at, "payload": snapshot}
    headers = {
        "apikey": config["service_role_key"],
        "Authorization": f"Bearer {config['service_role_key']}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    http = session or requests
    response = http.post(
        f"{config['url']}/rest/v1/portfolio_snapshots?on_conflict=user_id",
        headers=headers,
        json=body,
        timeout=20,
    )
    response.raise_for_status()
    print("Supabase private snapshot uploaded")
    return "uploaded"


def load_private_snapshot(*, session=None) -> dict | None:
    """Load the latest private snapshot for LKG recovery inside the worker.

    This endpoint is never called by the public site.  It is only used when a
    whole Form header is invalid, so the pipeline can publish a fresh health
    status while retaining the last validated portfolio state.
    """
    config = _required_config()
    if not all(config.values()):
        return None
    headers = {
        "apikey": config["service_role_key"],
        "Authorization": f"Bearer {config['service_role_key']}",
    }
    http = session or requests
    response = http.get(
        f"{config['url']}/rest/v1/portfolio_snapshots",
        headers=headers,
        params={
            "user_id": f"eq.{config['user_id']}",
            "select": "generated_at,payload",
            "order": "generated_at.desc",
            "limit": "1",
        },
        timeout=20,
    )
    if getattr(response, "status_code", 200) == 404:
        return None
    response.raise_for_status()
    rows = response.json()
    if not rows:
        return None
    row = rows[0] if isinstance(rows[0], dict) else {}
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return None
    return {
        "generated_at": row.get("generated_at") or payload.get("generatedAt"),
        "payload": payload,
    }


def upload_private_transactions(transactions, *, session=None) -> str:
    """Append transactions without overwriting existing ledger entries.

    Existing UUIDs are compared before inserts. A matching replay is ignored.
    Reusing a UUID with different content is quarantined as an immutable-ledger
    conflict; valid rows still upload and the caller receives a degraded result
    with the conflict details. The service-role key is only read inside this
    server-side job.
    """
    config = _required_config()
    required = os.getenv("SUPABASE_PRIVATE_SYNC_REQUIRED", "false").lower() in {"1", "true", "yes", "on"}
    if not transactions:
        return TransactionSyncResult("skipped")
    if not all(config.values()):
        if required:
            raise RuntimeError("Supabase transaction sync is required but credentials are missing")
        print("Supabase transaction sync skipped; credentials are not configured")
        return TransactionSyncResult("skipped")

    payloads = [transaction_payload(transaction) for transaction in transactions]
    for payload in payloads:
        # Stored privately with the ledger row. It is never included in the
        # public Demo contract and makes row-number changes replay-safe.
        payload["source_fingerprint_version"] = _FINGERPRINT_VERSION
        payload["source_fingerprint"] = transaction_fingerprint(payload)
    ids = [payload["transaction_id"] for payload in payloads]
    headers = {
        "apikey": config["service_role_key"],
        "Authorization": f"Bearer {config['service_role_key']}",
        "Content-Type": "application/json",
    }
    http = session or requests
    response = http.get(
        f"{config['url']}/rest/v1/portfolio_transactions",
        headers=headers,
        params={
            "user_id": f"eq.{config['user_id']}",
            "select": "transaction_id,payload",
        },
        timeout=20,
    )
    response.raise_for_status()
    existing = {str(row["transaction_id"]): row.get("payload", {}) for row in response.json()}
    existing_by_fingerprint = {}
    existing_by_derived_core = {}
    existing_by_legacy_derived_core = {}
    for existing_id, existing_payload in existing.items():
        if isinstance(existing_payload, dict):
            existing_by_fingerprint.setdefault(transaction_fingerprint(existing_payload), (existing_id, existing_payload))
            if _is_derived_price(existing_payload):
                derived_key = json.dumps(
                    _canonical_event(existing_payload, include_price=False),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                existing_by_derived_core.setdefault(derived_key, (existing_id, existing_payload))
            if existing_payload.get("compatibility_used") == "legacy_mixed_form_row":
                legacy_key = json.dumps(
                    _canonical_event(existing_payload, include_price=False),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                existing_by_legacy_derived_core.setdefault(legacy_key, (existing_id, existing_payload))
    conflicts = []
    conflict_report = []
    replays = []
    derived_price_replays = []
    for payload in payloads:
        previous = existing.get(payload["transaction_id"])
        if previous is not None:
            if previous == payload:
                replays.append(_replay_record(payload, payload["transaction_id"]))
                continue
            fingerprint_match = existing_by_fingerprint.get(payload["source_fingerprint"])
            if fingerprint_match and fingerprint_match[0] != payload["transaction_id"]:
                # Google Forms row numbers are not stable: inserting a new
                # response can reuse an old UUID5(row-number) for a different
                # event.  A full source fingerprint match to another
                # immutable row proves this is the original event, so retain
                # that row and quarantine neither event.  No field is
                # normalised or overwritten here; unmatched core changes
                # continue through the strict CONFLICT path below.
                replay = _replay_record(payload, fingerprint_match[0])
                replay.update({
                    "classification": "REPLAY_SOURCE_FINGERPRINT",
                    "reason": "source_row_id_reused_after_sheet_row_shift",
                    "ignored_identity_fields": ["transaction_id", "source_row_id"],
                })
                replays.append(replay)
                continue
            if _derived_price_replay(previous, payload):
                if _normalise_value(previous.get("price")) != _normalise_value(payload.get("price")):
                    replay = _replay_record(payload, payload["transaction_id"])
                    replay.update({
                        "classification": "REPLAY_DERIVED_PRICE",
                        "reason": "settlement_quote_changed",
                        "ignored_derived_fields": ["price"],
                        "previous_price": previous.get("price"),
                        "current_price": payload.get("price"),
                    })
                    derived_price_replays.append(replay)
                    replays.append(replay)
                    continue
                replays.append(_replay_record(payload, payload["transaction_id"]))
                continue
            if _legacy_mixed_derived_price_replay(previous, payload):
                replay = _replay_record(payload, payload["transaction_id"])
                replay.update({
                    "classification": "REPLAY_DERIVED_PRICE",
                    "reason": "legacy_mixed_form_settlement_quote_changed",
                    "ignored_derived_fields": ["price"],
                })
                derived_price_replays.append(replay)
                replays.append(replay)
                continue
            if _metadata_only_replay(previous, payload):
                replay = _replay_record(payload, payload["transaction_id"])
                replay.update({
                    "classification": "REPLAY_METADATA",
                    "reason": "non_financial_serialization_changed",
                    "ignored_metadata_fields": _metadata_changed_fields(previous, payload),
                })
                replays.append(replay)
                continue
            if transaction_fingerprint(previous) == payload["source_fingerprint"] or _same_legacy_reconciliation(previous, payload):
                replays.append(_replay_record(payload, payload["transaction_id"]))
                continue
            record = _conflict_record(payload, previous)
            conflicts.append(record)
            conflict_report.append(record)
            continue
        matched = existing_by_fingerprint.get(payload["source_fingerprint"])
        if matched:
            # The source row/UUID changed, but immutable financial facts did
            # not. Keep the original ledger row and classify this as REPLAY.
            if _derived_price_replay(matched[1], payload) and _normalise_value(matched[1].get("price")) != _normalise_value(payload.get("price")):
                replay = _replay_record(payload, matched[0])
                replay.update({
                    "classification": "REPLAY_DERIVED_PRICE",
                    "reason": "settlement_quote_changed",
                    "ignored_derived_fields": ["price"],
                    "previous_price": matched[1].get("price"),
                    "current_price": payload.get("price"),
                })
                derived_price_replays.append(replay)
                replays.append(replay)
            else:
                replays.append(_replay_record(payload, matched[0]))
            continue
        if payload.get("compatibility_used") == "legacy_mixed_form_row":
            legacy_key = json.dumps(
                _canonical_event(payload, include_price=False),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            legacy_match = existing_by_legacy_derived_core.get(legacy_key)
            if legacy_match and _legacy_mixed_derived_price_replay(legacy_match[1], payload):
                replay = _replay_record(payload, legacy_match[0])
                replay.update({
                    "classification": "REPLAY_DERIVED_PRICE",
                    "reason": "legacy_mixed_form_settlement_quote_changed",
                    "ignored_derived_fields": ["price"],
                })
                derived_price_replays.append(replay)
                replays.append(replay)
                continue
        if _is_derived_price(payload):
            derived_key = json.dumps(
                _canonical_event(payload, include_price=False),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            matched = existing_by_derived_core.get(derived_key)
            if matched:
                replay = _replay_record(payload, matched[0])
                replay.update({
                    "classification": "REPLAY_DERIVED_PRICE",
                    "reason": "settlement_quote_changed",
                    "ignored_derived_fields": ["price"],
                    "previous_price": matched[1].get("price"),
                    "current_price": payload.get("price"),
                })
                derived_price_replays.append(replay)
                replays.append(replay)

    conflict_summary = _conflict_summary(conflict_report, replays)
    conflict_ids = {item["transaction_id"] for item in conflicts}
    replay_ids = {item["transaction_id"] for item in replays}
    if conflicts:
        print(f"Supabase immutable ledger conflicts quarantined: {len(conflicts)}")
        fields = ",".join(
            f"{key}={value}" for key, value in conflict_summary["changedFieldCounts"].items()
        ) or "none"
        markers = ",".join(
            f"{key}={value}" for key, value in conflict_summary["compatibilityMarkerPairs"].items()
        ) or "none"
        print(
            "Supabase conflict summary: "
            f"core={conflict_summary['coreConflictCount']}, "
            f"metadata_only={conflict_summary['metadataOnlyConflictCount']}, "
            f"source_row_drift={conflict_summary['sourceRowIdDriftCount']}, "
            f"fields={fields}, markers={markers}"
        )
    if replays:
        print(f"Supabase ledger replays accepted: {len(replays)}")
    if derived_price_replays:
        print(f"Supabase derived price replays accepted: {len(derived_price_replays)}")

    missing = [
        payload
        for payload in payloads
        if payload["transaction_id"] not in existing
        and payload["transaction_id"] not in conflict_ids
        and payload["transaction_id"] not in replay_ids
    ]
    if missing:
        rows = [
            {
                "user_id": config["user_id"],
                "transaction_id": payload["transaction_id"],
                "source_row_id": payload["source_row_id"],
                "reversal_of": payload.get("reversal_of"),
                "payload": payload,
            }
            for payload in missing
        ]
        insert_headers = {**headers, "Prefer": "resolution=ignore-duplicates,return=minimal"}
        insert_response = http.post(
            f"{config['url']}/rest/v1/portfolio_transactions",
            headers=insert_headers,
            json=rows,
            timeout=20,
        )
        insert_response.raise_for_status()
        print(f"Supabase transactions appended: {len(missing)}")
        status = "degraded" if conflicts else "uploaded"
        return TransactionSyncResult(
            status,
            conflicts=conflicts,
            replays=replays,
            derived_price_replays=derived_price_replays,
            conflict_report=conflict_report,
            conflict_summary=conflict_summary,
            uploaded=len(missing),
            unchanged=len(replays),
        )
    print("Supabase transactions already synchronized")
    return TransactionSyncResult(
        "degraded" if conflicts else "unchanged",
        conflicts=conflicts,
        replays=replays,
        derived_price_replays=derived_price_replays,
        conflict_report=conflict_report,
        conflict_summary=conflict_summary,
        unchanged=len(replays),
    )
