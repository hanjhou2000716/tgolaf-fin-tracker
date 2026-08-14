## Mixed Form response compatibility repair

### Root cause

The live `表單回覆 3` sheet contains the original five-column transaction branch
and the newer Form V2 branch side by side. The parser selected the first duplicate
`交易類型` column, so historical BUY/SELL rows were incorrectly routed through
the strict V2 path and disappeared from the inventory stream.

### Fix

- Detect the mixed response-sheet shape without weakening strict V2 validation.
- Use the V2 transaction type column as the branch discriminator.
- Preserve legacy rows as canonical accepted transactions with an auditable
  compatibility marker and deterministic UUID/source-row identity.
- Select the separate legacy symbol column instead of reusing the asset-type
  column when headers are duplicated.
- Keep the existing exact legacy cash `SET_BALANCE` compatibility path intact.

### Verification

- `python -m unittest discover -s tests -q` — 163 tests passed.
- Added regression coverage for a historical 2330 BUY row in the mixed sheet.
- Existing Form V2, SET_BALANCE, duplicate-ID, lot conversion, and cash-flow
  tests remain green.

This PR must be merged before the next production Actions run; post-merge
verification must confirm that historical positions are present in the private
Supabase snapshot rather than only the cash correction.
