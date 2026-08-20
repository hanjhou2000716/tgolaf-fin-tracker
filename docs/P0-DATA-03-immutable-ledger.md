# P0-DATA-03：Immutable Ledger 重播與衝突處理

## 行為

- 每筆私有交易 payload 會產生 `source_fingerprint`。指紋使用交易日期、標的、動作、數量、單位、幣別、價格與提交時間，不包含工作表列號或交易 UUID。
- 同一金融事實再次出現時標記為 `REPLAY`，保留原 Supabase row，不重複入帳，也不覆寫 immutable ledger。
- 核心欄位不同時標記為 `CONFLICT`，新資料不寫入正式帳本。
- 每次執行會在 `.private-build/ledger_conflicts.json` 留下私有稽核資料：交易 ID、來源列、舊／新 payload、差異欄位與處理結果。該檔案不會部署到 GitHub Pages。

## Telegram 告警

真正的 `CONFLICT` 第一次發生時才會加入資料輸入異常告警。成功發送後，History 只保存衝突集合的雜湊標記；相同衝突在後續 Cron 重跑不會重複推播。新的核心欄位變更會產生新的雜湊並再次告警。

## 保留原則

既有 Supabase ledger、Google Form 回覆與交易稽核紀錄不會被刪除或更新。相容重播僅是讀取比對與稽核分類，不是資料修正。
