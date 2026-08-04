# P1-RISK-01 完整曝險矩陣

曝險矩陣現在同時輸出公司、產業、國家、市場、幣別、發行人與槓桿產品維度。ETF 先按 look-through 權重展開到公司曝險，直接持倉與穿透持倉在同一公司鍵上累計，避免重複呈現；缺少 metadata 時明確歸入 `unknown`。

每個維度項目均包含市值 `value` 與相對總資產的 `percent`，可直接供 Growth Dashboard 與 Risk Center 使用。
