# P1-RISK-03 情境實驗室

`scenario_experiment.py` 讓 UI 以台股、美股、NVDA、TSMC、USD/TWD 與利率百分比調整情境，並回傳新資產、淨資產、維持率、回撤與補繳需求。每次結果都同時帶 baseline 與 Guardrail 狀態，資料不足或超出合理範圍會拒絕執行。
