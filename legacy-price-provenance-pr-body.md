## 根治舊版估算價格衝突

### 根因

正式 Actions 在 PR #146 後仍看到 7 筆 `price` 衝突，且前後相容標記皆為 `legacy_mixed_form_row`。舊版混合表單的買賣事件原本沒有成交價，後續由結算行情補價，但標記沒有改成新版 `settlement_quote_estimate:*`，因此先前判定器沒有穩定辨識其來源。

### 修復

- 統一 canonical 化相容標記與買賣動作，容忍舊版大小寫、空白與中文／英文格式。
- 將帶有 `legacy_mixed_form_row` 的買賣價格視為系統估算價格；明確成交價走原本嚴格路徑。
- 只有標的、動作、數量、日期、幣別與單位完全相同、唯一差異為價格時，才分類為 `REPLAY_DERIVED_PRICE`。
- 不修改或刪除 Supabase immutable ledger；非價格核心差異仍隔離為 `CONFLICT`。

### 驗證

- `python -m pytest -q`：210 passed。
- 覆蓋舊版標記變體、中文／英文動作、價格重播與核心欄位衝突。

### 正式驗收

合併後重跑正式 Actions，確認 7 筆不再列入 conflict、估算價格重播被接受、Telegram 不再顯示資料輸入異常。既有 ledger 與公開 Demo 不做破壞性修改。
