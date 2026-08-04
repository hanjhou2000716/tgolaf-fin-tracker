# P1-PERF-01 績效與基準比較

`performance_report.py` 將既有 TWR、XIRR、年化波動、Sharpe、Sortino、Calmar、最大回撤及恢復時間統一成可交換的報告契約，並可與 006208、VT、QQQ 等同期間基準比較。

每份報告都帶有 `scope`：是否含現金、負債、匯率與費用。基準序列長度不一致時直接拒絕，避免不同期間的績效被誤比。
