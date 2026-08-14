# Luna G4–G12 證據帳本

本文件記錄可重現的測試、正式 Actions、Supabase RLS、公開頁面脫敏與通知驗收結果。金融資料只在受保護的 Supabase 私有資料路徑使用；公開 GitHub Pages 僅提供 Demo。

## Evidence ledger

| ID | 驗收項目 | 結果 | 證據 |
| --- | --- | --- | --- |
| E-001 | 交易 Schema、單位、冪等與 Form V2 測試 | PASS | `python -m unittest discover -s tests -q`：162 tests passed（正式 Actions run `31776066730`） |
| E-002 | `SET_BALANCE` 與精確餘額重建 | PASS | `test_explicit_set_balance_is_applied`、`test_reconciliation_sets_exact_balance_and_is_idempotent` |
| E-003 | 舊表單「取代台幣現金金額」相容解析 | PASS | `test_legacy_form_replace_cash_row_becomes_set_balance`、`test_original_cash_snapshot_description_uses_legacy_price_target` |
| E-004 | reconciliation 不混入外部現金流或市場損益 | PASS | `test_reconciliation_is_not_external_flow_or_market_pnl`、`test_legacy_inventory_and_canonical_event_share_final_cash` |
| E-005 | ingestion 摘要、拒絕原因與近期狀態 | PASS | `test_ingestion_contract_has_summary_and_actionable_rejection` |
| E-006 | MiniApp 資料健康與交易狀態元件 | PASS | `tests/test_data_health_layout.py`；`renderIngestion`／近期交易狀態契約通過 |
| E-007 | Supabase RLS | PASS | Supabase SQL 唯讀檢查：`goal_ladder_states`、`portfolio_snapshots`、`portfolio_transactions` 均啟用 RLS；未授權 API 仍回 401 |
| E-008 | 公開頁面脫敏 | PASS | run `31776066730` 的 `Verify generated public Demo is sanitized` 通過；公開頁面 200，未含個人持倉與金額標記 |
| E-009 | GitHub Actions Build／Deploy | PASS | run `31776066730`：build 46s、deploy 12s，兩個 job 全部成功 |
| E-010 | 強制 Telegram 通知 | PASS | run `31776066730` log：`Telegram notification sent; window=manual, forced=True` |
| E-011 | Supabase 私有同步與 immutable replay | PASS | run `31776066730` log：`Supabase private snapshot uploaded`、`Supabase transactions already synchronized`、`Supabase legacy reconciliation replay accepted; immutable row preserved` |
| E-012 | 雙 Cron 備援排程 | PASS | `.github/workflows/cron.yml`：UTC `40 21 * * 1-5`（台灣 05:40）與 `45 6 * * 1-5`（台灣 14:45）；近期 schedule runs `31749760663`、`31680517185` 成功 |
| E-013 | Data Freshness Watchdog | PASS | `health-watchdog.yml` 近期 schedule runs `31750470338`、`31688525224` 成功 |
| E-014 | Google Form V2 固定欄位與舊資料相容 | PASS | Form V2 adapter、重複欄位選取、legacy label 與拒絕路徑測試通過；PR #124–#128 已合併 |

## 原始台幣現金事件

Supabase SQL 唯讀查詢確認原始來源列 `表單回覆 3:23` 仍以 immutable `SET_BALANCE` 事件保存：

- `transaction_id`: `308079ca-4f56-5cfc-a1ee-0c28275812c3`
- `source_row_id`: `表單回覆 3:23`
- `symbol`／`currency`: `TWD`
- legacy price-field compatibility：`legacy_target_from_price_field`
- reconciliation target：`150000`

正式 run 同步時接受語義等價 replay，保留既有 immutable row，不覆寫歷史交易。

## Completion debt

1. 仍需從已登入的私有 MiniApp／Supabase API 做一次唯讀畫面驗證，確認最新私有快照的現金元件直接顯示 `150000`；Actions 已證明私有快照上傳成功，交易列也已確認。
2. 仍需保留一筆新的 Form V2 實際提交回應作為端到端人工證據；既有舊列的相容解析與重複提交測試已通過。
3. Cron 與 watchdog 已有近期成功 run；若要做時刻級驗收，需在下一個 05:40／14:45 視窗補一筆新的 schedule run ID。

## Reproduction gate

```text
python -m unittest discover -s tests -q
162 tests passed
```

## Latest verification note（2026-08-14）

- PR #124–#128 已合併至 `main`；最新 main SHA：`cfb369766e63af64896995cbeaa449f2aefdd467`。
- 正式 run `31776066730` 由 `workflow_dispatch(force_telegram=true)` 觸發，Build／Deploy、資料驗證、公開 Demo 脫敏、Supabase 私有同步與 Telegram 強制通知全部成功。
- Yahoo 行情來源在該次執行出現 fallback 訊息，但未造成部署失敗；資料品質與 stale guardrail 仍由測試與 runtime 狀態保護。
- 公開入口回傳 Demo；私有入口未授權時只回傳登入殼層，不嵌入金額或 service-role key。
