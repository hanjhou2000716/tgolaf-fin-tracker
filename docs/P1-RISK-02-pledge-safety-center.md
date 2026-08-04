# P1-RISK-02 質押安全中心

`risk_center.py` 將現值維持率、警戒線/追繳線、折扣後擔保品、壓力後維持率、跌幅距離與補繳需求統一成 Risk Center 契約。所有輸出都是風險估計；`leverageIncreaseAllowed` 固定需要其它複合 Guardrail 通過，且本模組不會因維持率良好而直接建議加槓桿。
