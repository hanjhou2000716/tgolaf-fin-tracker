# P1-ALERT-01 統一告警引擎

私有 payload 新增 `alerts`，規則統一分為 INFO、WATCH、WARNING、CRITICAL，涵蓋
維持率、壓力後維持率、單一公司曝險、現金安全墊、行情過期與帳本對帳。引擎支援
冷卻時間、重複告警抑制、恢復通知與 acknowledgment；每筆結果保留 `triggered`、
`send`、`recovered` 與 `acknowledged` 狀態。
