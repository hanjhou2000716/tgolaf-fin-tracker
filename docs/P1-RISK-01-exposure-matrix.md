# P1-RISK-01 曝險矩陣

私有 portfolio payload 新增 `exposureMatrix`，提供公司、市場、幣別與發行人四種聚合。
ETF 會依已知穿透權重分配到成分股；直接持股與 ETF 間接持股會先合併再計算比例，
未知 metadata 明確標成 `unknown`，不會把缺漏資料假設成某個市場或幣別。
