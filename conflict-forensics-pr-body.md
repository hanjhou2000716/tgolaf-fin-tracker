## 安全鑑識與告警去重

### 變更

- 新增不含金額的 immutable ledger 衝突摘要：核心欄位差異、來源列漂移、相容標記類別與欄位統計。
- 完整舊／新 payload 仍只保留在私有 `.private-build/ledger_conflicts.json` 與 Supabase，不寫入公開 Demo。
- 對已匹配交易 ID 的非金融序列化差異分類為 `REPLAY_METADATA`；核心欄位與明確成交價變更仍隔離為 `CONFLICT`。
- 將 ledger conflict Telegram marker 改為跨日期 digest 去重，同一批衝突只通知一次，衝突集合變更才重新通知。

### 驗證

- `python -c "import tempfile,unittest; tempfile.tempdir=...; ..."`：206 tests passed。
- 新增 metadata replay、無敏感值衝突摘要、跨日期 digest 去重測試。
- 未修改 Supabase schema、既有 immutable ledger、Google Form 回覆或公開 Demo。

### 後續驗收

合併後執行一次正式 Actions，讀取私有衝突摘要；僅依實際差異建立後續 normalization PR，不一次性放行既有 29 筆衝突。
