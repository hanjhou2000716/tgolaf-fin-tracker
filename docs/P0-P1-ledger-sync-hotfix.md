# Ledger sync result hotfix

資產載入器現在明確回傳 `(inventory, history_sheet, accepted_transactions, ledger_sync_result)`，主流程使用同一個結果寫入私有 snapshot。無交易資料的 legacy/空資料路徑也維持相同回傳契約，避免正式 workflow 在輸出階段因未定義變數中止。
# Legacy reconciliation replay: derived delta

The immutable `SET_BALANCE` command is identified by its source row, action,
cash symbol/currency, target quantity, unit, reversal reference, and
transaction date. `reconciliation_delta` is deliberately not part of that
identity: it is derived from the balance reconstructed immediately before the
command. Restoring historical cash-flow rows can therefore change the delta
without changing the user's command or target balance. A replay keeps the
persisted transaction row immutable while recording the current derived
adjustment in the private audit payload.
