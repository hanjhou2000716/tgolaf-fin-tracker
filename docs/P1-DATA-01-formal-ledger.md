# P1-DATA-01 正式交易帳本

`transaction_schema.py` 負責驗證表單欄位；`portfolio_ledger.py` 負責把通過驗證的交易事件重建成帳戶狀態。

支援的事件包括：

- `BUY`／`SELL`
- `DEPOSIT`／`WITHDRAWAL`
- `DIVIDEND`／`INTEREST`／`FEE`／`TAX`
- `BORROW`／`REPAY`
- `SPLIT`、`SPIN_OFF`、`TRANSFER`
- `FX_CONVERSION`
- `REVERSAL`

每筆事件必須使用 UUID `transaction_id`。相同 UUID 重播會被視為冪等；同一 UUID 搭配不同內容會直接拒絕。更正交易只能透過 `REVERSAL` 指向原交易，不能修改既有事件。

特殊欄位格式：

- `SPLIT`：`quantity` 為拆股倍率。
- `TRANSFER`／`SPIN_OFF`：`symbol` 使用 `SOURCE->TARGET`。
- `FX_CONVERSION`：`symbol` 使用 `SOURCE/TARGET`，`price` 為匯率。

這個模組只負責可重現的帳本狀態，不自行取得行情或估算市值；行情估值仍由 `main.py` 的市場資料服務處理。
