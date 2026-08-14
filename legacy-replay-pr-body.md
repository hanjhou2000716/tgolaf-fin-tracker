## Immutable replay compatibility for historical snapshots

### Root cause

The first production run after PR #133 correctly restored historical
non-cash `SET_BALANCE` baselines. Existing Supabase rows from the earlier
adapter contained a derived `reconciliation_delta`; the restored replay did
not, so strict UUID comparison raised an immutable conflict for a legacy row.

### Fix

- Permit derived-field differences only when the stored row has an explicit
  `legacy_target_from_price_field` or `legacy_mixed_form_row` compatibility
  marker.
- Keep source, action, asset, symbol, currency, unit, quantity, price,
  reversal, and date comparisons strict.
- Keep all ordinary UUID conflicts fail-closed.
- Add a regression test for non-cash pledge-debt snapshot replay.

### Verification

- Full suite: `python -m unittest discover -s tests -q` → 168 tests passed.
- The failed production run was `31797868178`; this PR addresses its exact
  immutable conflict before the next formal run.
