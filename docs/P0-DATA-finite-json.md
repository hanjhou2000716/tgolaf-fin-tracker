# Finite JSON guard

私有 snapshot 與 Supabase 寫入前會遞迴檢查浮點數；`NaN`、`Infinity`、`-Infinity` 轉為 JSON `null`，避免行情 provider fallback 造成 `requests` 的 `InvalidJSONError`。原始資料品質仍由 Data Health 與 quote quality 欄位呈現。
