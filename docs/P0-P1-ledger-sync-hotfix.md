# Ledger sync result hotfix

資產載入器現在明確回傳 `(inventory, history_sheet, accepted_transactions, ledger_sync_result)`，主流程使用同一個結果寫入私有 snapshot。無交易資料的 legacy/空資料路徑也維持相同回傳契約，避免正式 workflow 在輸出階段因未定義變數中止。
