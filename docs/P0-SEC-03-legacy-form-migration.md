# P0-SEC-03 舊版表單遷移橋接

目前 Google Form 回覆分頁仍是舊版資產快照欄位。正式 workflow 以
`FORM_SCHEMA_STRICT=true` 驗證新交易 Schema；在表單欄位遷移完成前，才由 workflow
明確設定 `FORM_SCHEMA_LEGACY_COMPAT=true` 暫時保留既有資產列。每次使用相容模式都會
在 `.private-build/transaction_audit.json` 記錄 `legacy_schema_compat`，不會將舊列誤記為
已核准交易或寫入 immutable ledger。

完成 Form 遷移後，請移除 `FORM_SCHEMA_LEGACY_COMPAT` secret（或設為 `false`），讓缺少
固定欄位的分頁直接 fail closed。
