# P1-MD-01 統一行情資料契約

台股行情透過 `MarketDataService` 回傳固定 `Quote` contract：`price`、`currency`、
`source`、`as_of`、`fetched_at`、`is_stale`、`fallback_used` 與 `quality`。

服務提供 TTL cache 與 stale-while-revalidate：供應商失敗但仍有快取時會明確標記
`quality=stale`，不可把舊價格誤當即時行情；完全沒有可用價格時則直接拋錯。現有
FinMind → Yahoo → yfinance fallback chain 保留，Growth 計算已透過此契約讀取台股價格。
