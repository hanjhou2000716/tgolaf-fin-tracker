# P0-DATA-04 History 寫入安全性

History 快照以欄位名稱建立 key-based upsert，不依賴欄位順序，也不再使用
`chr(64 + column)` 推算欄位。A1 轉換支援 `Z` 之後的欄位（例如 `AA`、`BA`）。

更新既有日期時只寫入本次快照提供的欄位，因此 `Settlement_Notification_Sent_At`
等 Telegram marker 不會被清空；新增日期則依現有標頭建立完整列。重複標頭會直接拒絕，
避免資料寫入到不明欄位。
