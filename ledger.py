"""Immutable transaction ledger primitives.

The ledger is append-only: replaying the same transaction UUID is a no-op,
while reusing a UUID for different content is a hard conflict. Corrections
must be represented by a REVERSAL transaction that points to an existing ID.
"""

from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal

from transaction_schema import Action, Transaction


class LedgerConflictError(ValueError):
    """Raised when a transaction ID is reused with different content."""


def transaction_payload(transaction: Transaction) -> dict:
    """Return a stable JSON-compatible representation of a transaction."""
    payload = asdict(transaction)
    payload["action"] = transaction.action.value
    payload["quantity"] = str(transaction.quantity)
    if transaction.price is not None:
        payload["price"] = str(transaction.price)
    if transaction.reconciliation_delta is not None:
        payload["reconciliation_delta"] = str(transaction.reconciliation_delta)
    if transaction.compatibility_used:
        payload["compatibility_used"] = transaction.compatibility_used
    payload["transaction_date"] = transaction.transaction_date.isoformat()
    return payload


def _canonical(value):
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


class ImmutableLedger:
    def __init__(self, entries=None):
        self.entries = []
        self._by_id = {}
        for entry in entries or []:
            self._append_payload(dict(entry))

    def _append_payload(self, payload):
        transaction_id = str(payload.get("transaction_id", "")).strip()
        if not transaction_id:
            raise ValueError("ledger entry requires transaction_id")
        existing = self._by_id.get(transaction_id)
        if existing is not None:
            if _canonical(existing) != _canonical(payload):
                raise LedgerConflictError(f"transaction_id reused with different content: {transaction_id}")
            return False
        self.entries.append(payload)
        self._by_id[transaction_id] = payload
        return True

    def append(self, transaction: Transaction) -> bool:
        payload = transaction_payload(transaction)
        if transaction.action == Action.REVERSAL:
            if not transaction.reversal_of:
                raise ValueError("REVERSAL requires reversal_of")
            if transaction.reversal_of not in self._by_id:
                raise ValueError("REVERSAL must reference an existing transaction")
        return self._append_payload(payload)

    def apply(self, transactions) -> int:
        added = 0
        for transaction in transactions:
            added += int(self.append(transaction))
        return added

    def as_dicts(self):
        return [dict(entry) for entry in self.entries]
