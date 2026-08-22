## 精準修復列號碰撞造成的 immutable conflict

### 正式 Actions 鑑識結果

合併 PR #144 後的正式 Actions 已成功完成 Build／Deploy。私有衝突摘要顯示固定 29 筆的相同 compatibility marker 為 `legacy_mixed_form_row`，但差異集中在核心欄位（symbol、quantity、asset_type、unit、currency、transaction_date 與部分 price），不是可安全忽略的格式差異。

Google Form 回覆表在歷史列中插入新回覆後，舊有以 `工作表:列號` 產生的 UUID5 會被重新使用於另一筆事件，造成同一 transaction_id 對應到不同金融事件。

### 修復

- 當列號型 transaction_id 發生內容衝突時，先用完整 `source_fingerprint` 尋找另一筆既有 immutable ledger row。
- 只有完整金融事件指紋相同、且匹配到不同既有 transaction_id 時，才分類為 `REPLAY_SOURCE_FINGERPRINT`。
- 保留原 immutable row，不新增、不覆寫、不放行任何核心欄位不同的事件。
- 找不到完全指紋匹配時，仍維持 `CONFLICT` 隔離與告警。
- 私有摘要新增 `sourceFingerprintReplayCount`；公開 Demo、Supabase schema、Telegram 格式不變。

### 驗證

- `python -m unittest discover -s tests -q`：207 tests passed。
- 新增列號碰撞重播測試，確認不寫入、不覆寫且保留真正核心衝突隔離。
- 保留既有估算價格重播、legacy 相容與跨日 Telegram digest 去重測試。

### 部署後驗收

合併後執行正式 Actions，確認 29 筆中只有能以完整指紋匹配的列號碰撞轉為 `REPLAY_SOURCE_FINGERPRINT`；剩餘真正核心差異仍會被隔離。
