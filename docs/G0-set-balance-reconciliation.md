# G0：SET_BALANCE 與現金對帳

本階段把「現金帳戶目前應有多少」和「外部存入／提領」分開。`SET_BALANCE`
只接受 `TWD`／`USD` 現金資產及非負 `target_balance`，透過 append-only
ledger event 將餘額設為目標值。重播相同 UUID 不會重複套用；同 UUID 的內容
不同則直接拒絕。

## 狀態與相容層

- `APPLIED`：明確的 SET_BALANCE 命令已套用。
- `PENDING`：表單保留 `approved=false`，不進入持倉計算。
- `REJECTED`：缺少目標、幣別錯誤、負值、非現金或模糊命令。
- `APPLIED_WITH_COMPATIBILITY`：僅針對舊表單的「現金／餘額」列，把舊 `price`
  欄轉成目標值，並標記 `legacy_target_from_price_field`。

私有快照的 `transactionIngestion` 以 `summary` 與 `recent` 契約保存狀態；
`recent` 保留最近五筆，MiniApp 只顯示最近三筆來源列、命令、幣別、目標值與
原因，不輸出提交者 Email。

## 損益邊界

對帳事件的 delta 進入 `reconciliationAdjustment`，不列為 external cash flow、
financing cash flow 或 market P&L。所有事件仍透過 immutable UUID ledger replay，
因此可重建任意日期的現金餘額。

## 驗收

```text
python -m unittest discover -s tests -q
153 tests passed
```

外部 Google Form 的實際題目／欄位仍需在 Form 端依四欄規格完成；程式已保留
舊列相容層，不會因 Form 尚未切換而猜測非現金交易。
