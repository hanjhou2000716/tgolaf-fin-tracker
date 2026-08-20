# 資產輸入可靠性與部署降級

Growth Dashboard 的交易輸入遵循固定邊界：Google Form 回應先由已知 schema
adapter 轉成 canonical transaction，再經單位、動作、行情與冪等性驗證，最後才
寫入 immutable ledger。

## Schema 邊界

- `CURRENT`：三欄簡化表單（交易類型、交易單位、交易數量）。
- `FORM_V2`：既有固定欄位表單。
- `LEGACY`／`LEGACY_COMPACT`：只透過明確 header mapping 的相容 adapter 讀取。
- `UNKNOWN`：不猜欄位、不把原始列直接送進資產 reducer；列為 `schema_drift` 拒絕。

## Row isolation

每筆交易都有獨立的 accepted、pending 或 rejected 狀態。行情缺失、過期、單位
不合法、重複來源列與 schema drift 都會保留 source row ID 與原因，不會默默計入
正式帳本。

若 Supabase immutable ledger 已有相同 `transaction_id` 但 payload 不一致，該列
會標成 `immutable_ledger_conflict` 並隔離；相同內容仍視為冪等重播，其他有效列
照常上傳。Build 會完成並在 Data Health／Telegram 顯示 DEGRADED 根因。

## 驗收

```text
python -m unittest discover -s tests -q
195 tests passed
```

這個降級策略不會覆寫既有 ledger，也不會把不完整資料當成新的投資交易。
