# P1-DATA-03 Data Health

`data_health.py` 統一輸出最後同步、資料年齡、行情來源品質、fallback、缺漏欄位、待核准交易與帳本對帳狀態。當缺漏或對帳失敗時狀態為 critical；資料過期時只能提示 stale，不會產生交易建議。
