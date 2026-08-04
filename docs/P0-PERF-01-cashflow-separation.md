# P0-PERF-01 現金流與市場損益分離

每日淨資產變化現在拆成：市場損益、外部現金流、融資現金流、股息／利息收入與
費用／稅。單純存入或借款不會被 Telegram 誤報成投資獲利；所有分項加總會回到
同日淨資產變化，並以 `reconciled` 標記是否完成對帳。

Telegram 結算通知會額外顯示市場損益、外部現金流與融資現金流，私有 JSON 亦提供
相同的 `portfolio.performance` 欄位供 Growth Dashboard 使用。
