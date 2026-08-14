# Luna 任務證據與完成債務帳

本文件區分程式測試、正式 Actions 與外部介面驗收；沒有證據的項目不標記為完成。

## Evidence ledger

| ID | 任務 | 狀態 | 證據 |
| --- | --- | --- | --- |
| E-001 | 固定交易欄位與拒絕原因 | PASS | 159 項 Python unittest 全通過，包含 Form V2 與 `test_header_order_is_explicit_and_quantity_lot_is_normalized` |
| E-002 | `SET_BALANCE` 明確命令 | PASS | `test_explicit_set_balance_is_applied`、`test_reconciliation_sets_exact_balance_and_is_idempotent` |
| E-003 | 舊表單現金快照相容 | PASS | `test_legacy_form_replace_cash_row_becomes_set_balance`、`test_original_cash_snapshot_description_uses_legacy_price_target` |
| E-004 | 對帳 delta 與損益邊界 | PASS | `test_reconciliation_is_not_external_flow_or_market_pnl`、`test_legacy_inventory_and_canonical_event_share_final_cash` |
| E-005 | ingestion 狀態契約 | PASS | `test_ingestion_contract_has_summary_and_actionable_rejection`；私有快照含 `summary`／`recent` |
| E-006 | MiniApp 狀態呈現 | PASS（靜態） | `tests/test_data_health_layout.py`；私有頁含 `renderIngestion`，公開 Demo 掃描通過 |
| E-007 | Supabase RLS | PASS | Supabase SQL Editor 於 2026-08-14 確認三表 `relrowsecurity=true`；每表 1 列、匿名寫入不在客戶端開放 |
| E-008 | 公開站去除真實資料 | PASS | 正式 run `31760413143` 的 `Verify generated public Demo is sanitized` 通過 |
| E-009 | GitHub Actions build/deploy | PASS | 正式 run `31760413143`：build 51s、deploy 13s，均成功 |
| E-010 | 強制 Telegram | PASS | 正式 run 日誌：`Telegram notification sent; window=manual, forced=True` |
| E-011 | Supabase 私有同步 | PASS | 同一正式 run 日誌：`Supabase private snapshot uploaded`；控制台 RLS／snapshot 已讀回 |
| E-012 | 備援排程 | PASS（workflow evidence） | Deployment workflow 定義 `40 21 * * 1-5`（台灣 05:40）與 `45 6 * * 1-5`（台灣 14:45）；近期 schedule runs `31749760663`、`31680517185` 成功 |
| E-013 | Watchdog 排程 | PASS | Watchdog schedule runs `31750470338`、`31688525224` 成功 |
| E-014 | Google Form V2 題目與相容解析 | PASS | 已取得編輯權限並完成五段式表單結構；程式新增 V2 adapter、混合歷史列相容解析與原始現金校正列保留。 |

## Completion debt

1. 正式來源列的 production cash=NT$150,000 仍需在實際表單資料完成後讀回快照驗證。
2. 表單 V2 已完成，仍需送出一筆非破壞性的測試回覆並確認回應列由 Actions 解析為正確狀態。
3. GitHub schedule 觸發受平台排程延遲影響；workflow 定義與近期成功 run 已驗證，仍需持續觀測準點率。
4. 任何資料過期、缺漏或對帳失敗時仍只能提示，不得產生交易建議；此規則由測試與 runtime guardrail 維持。

## Reproduction gate

```text
python -m unittest discover -s tests -q
160 tests passed
```

## Latest verification note (2026-08-14)

- Form V2 duplicate branch headers are resolved by selecting the first non-empty
  value for repeated currency, date, note, and amount columns.
- `tests.test_form_v2` covers a stock row with repeated Google Form headers;
  the full suite is 160 tests passing.
- PR #123 remains open for review; post-merge production acceptance is pending
  until the PR is merged.
