# Luna 任務證據與完成債務帳

本文件是 G0／P0 實作的可追溯狀態，不把「程式已具備」誤寫成「正式環境已驗收」。
每個項目都要有測試、部署或外部系統證據，才可移入完成區。

## Evidence ledger

| ID | 任務 | 狀態 | 證據 |
| --- | --- | --- | --- |
| E-001 | 固定交易欄位與拒絕原因 | PASS | `tests/test_balance_command.py`、`tests/test_transaction_schema.py`；154 tests 全過 |
| E-002 | `SET_BALANCE` 明確命令 | PASS | `test_explicit_set_balance_is_applied`、`test_reconciliation_sets_exact_balance_and_is_idempotent` |
| E-003 | 舊表單現金快照相容 | PASS | `test_legacy_form_replace_cash_row_becomes_set_balance`、`test_original_cash_snapshot_description_uses_legacy_price_target` |
| E-004 | 對帳 delta 與損益邊界 | PASS | `test_reconciliation_is_not_external_flow_or_market_pnl`、`test_legacy_inventory_and_canonical_event_share_final_cash` |
| E-005 | ingestion 狀態契約 | PASS | `test_ingestion_contract_has_summary_and_actionable_rejection`；私有快照含 `summary`／`recent` |
| E-006 | MiniApp 狀態呈現 | PASS（靜態） | `tests/test_data_health_layout.py`；私有頁面含 `renderIngestion`，公開頁不含私有資料 |
| E-007 | Supabase RLS | PASS（控制台讀取證據） | `goal_ladder_states`、`portfolio_snapshots`、`portfolio_transactions` 均已確認 RLS；私有列已有 owner user id |
| E-008 | 公開站去除真實資料 | PASS（程式／靜態） | public payload security tests；公開頁只保留 Demo |
| E-009 | Google Form 題目切換 | BLOCKED（外部介面） | 程式已提供四欄規格與 legacy adapter；尚未取得 Form 編輯權限，不能宣稱題目已切換 |
| E-010 | GitHub Actions PR | IN PROGRESS | PR #121：`G0: add SET_BALANCE reconciliation and transaction status`；需使用者合併後再做正式部署驗收 |
| E-011 | 正式 cash=150000 | PENDING | 已有 deterministic fixture；尚待合併後以正式來源列重跑並讀回快照 |
| E-012 | 雙 Cron／Telegram／Supabase post-merge | PENDING | 需 PR 合併後執行正式 Actions、05:40／14:45 及 Telegram 強制發送驗證 |

## Progress ledger

- G0 parser／ledger／performance／attribution：完成，保留現有相容層。
- 私有 ingestion contract 與 MiniApp：完成靜態整合。
- Supabase migration：本分支未新增 migration；現有三張表與 RLS 已在控制台確認。
- 外部 Form：保留明確阻塞，不繞過權限、不把未完成的外部操作標成完成。
- PR／正式環境：等待使用者合併後才能執行 post-merge gates。

## Completion debt

1. PR #121 合併與 Actions build/deploy 綠燈。
2. 正式私有頁面讀回 `SET_BALANCE` 後 cash 準確為 NT$150,000。
3. Supabase 401／RLS、Telegram mini app、05:40／14:45 雙 Cron 實測。
4. Google Form 四欄題目與帳號限制需在表單端完成；若仍無編輯權限，維持 E-009 BLOCKED。
5. 驗收證據回填本表後，才可宣告 Luna G0 完成。

## Reproduction gate

```text
python -m unittest discover -s tests -q
154 tests passed
```

