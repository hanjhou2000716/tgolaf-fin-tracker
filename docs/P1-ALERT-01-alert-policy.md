# P1-ALERT-01 統一告警政策

`alert_policy.py` 固定 INFO/WATCH/WARNING/CRITICAL 的政策名稱與門檻，並把情境實驗室的 `stressRatio`、行情時間戳轉成現有 AlertEngine 可處理的契約。引擎保留去重、冷卻、恢復與 acknowledgment；告警結果可直接供 Telegram 與 Growth Dashboard 共用。
