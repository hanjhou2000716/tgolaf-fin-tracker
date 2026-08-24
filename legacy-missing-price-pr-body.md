## 修復舊版交易首次補上估算價格的假衝突

### 私有鑑識結果

PR #148 的正式摘要確認 7 筆資料皆為：

- 動作：`BUY -> BUY`
- 相容標記：`legacy_mixed_form_row -> legacy_mixed_form_row`
- 唯一差異：`price`
- 價格來源：`missing -> derived`

也就是舊 immutable ledger 只有交易核心資料，沒有成交價；本次執行第一次補上結算行情估算價。不是使用者修改成交價，也不是新交易重複入帳。

### 修復

- 舊版混合表單 BUY／SELL 在「既有價格缺失、本次價格為系統估算」時分類為 `REPLAY_DERIVED_PRICE`。
- 保留既有 immutable ledger，不寫入、不覆寫、不刪除歷史 payload。
- 仍嚴格要求標的、動作、數量、日期、幣別與單位完全一致。
- 明確成交價變更、非買賣動作或任何其他核心差異仍維持 `CONFLICT`。
- 私有鑑識摘要將價格來源明確區分為 `missing`、`derived`、`explicit`。

### 驗證

- `python -m pytest -q`：212 passed。
- 新增「missing → derived」重播測試。

### 正式驗收

合併後重跑正式 Actions，預期 7 筆衝突降為 0；確認 Growth Dashboard、Supabase 私有快照正常，再以 `force_telegram=true` 發送驗收通知。
