## Legacy snapshot baseline preservation

### Why

The compatibility adapter was excluding every `SET_BALANCE` transaction from
the legacy inventory stream. That was safe for cash reconciliation but silently
dropped historical securities, fund, and pledge-debt snapshot rows. It also
made the reconciliation layer attempt to treat a pledge-debt row as cash.

### Change

- Route only explicit cash `SET_BALANCE` rows through reconciliation events.
- Preserve non-cash legacy snapshot rows in the compatibility inventory stream.
- Add regression tests for holdings/debt preservation and cash-only routing.
- Append the production evidence and exact 150000 acceptance audit.

### Verification

- `python -m unittest discover -s tests -q` → 167 tests passed.
- No existing Buy/Sell/Deposit/Withdrawal behavior changed.
- No public payload or authentication behavior changed.

This PR is intentionally limited to the legacy compatibility/accounting gate.
