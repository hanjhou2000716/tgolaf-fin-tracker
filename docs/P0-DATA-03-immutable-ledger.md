# P0-DATA-03 Immutable transaction ledger

已核准交易會以 `transaction_id`、`source_row_id` 與完整 payload 保存到私有
Supabase `portfolio_transactions`。資料表只開放登入使用者讀取，寫入由 GitHub
Actions 的 server-side service role 執行；匿名與一般 authenticated client 均無法寫入。

同一 UUID 重跑時會被視為冪等 replay，不會重複計算。若同一 UUID 的內容不同，
同步會 fail closed，避免覆蓋歷史。更正交易必須使用 `REVERSAL` 並指定
`reversal_of`，不可直接修改原交易。

## Legacy compatibility replay

During the Form V2 migration, a failed production build may already have
persisted a cash `SET_BALANCE` row using the legacy target-from-price-field
adapter. A later canonical replay is accepted only when its source row,
currency, target, date, action, and reconciliation delta are identical. The
original immutable row is retained and no replacement write is issued. Any
other payload difference still fails closed as an immutable-ledger conflict.
