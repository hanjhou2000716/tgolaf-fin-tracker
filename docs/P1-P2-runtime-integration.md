# Runtime integration

`runtime_extensions.py` 將已驗證的績效、Regime、目標機率、追繳機率、Data Health 與 Advisor 契約接到同一個私有 snapshot。它不改變公開 Demo，也不會產生自動交易指令；歷史不足、資料過期或對帳失敗時只回傳明確的不可用/Guardrail 狀態。
