# PRStK Growth Dashboard

PRStK Growth Dashboard 是資產結算、投資組合風險與長期成長追蹤系統。它以 Google Sheets/Form 作為資產異動來源，透過 Python 產生靜態儀表板、公開 JSON 資料與 Telegram 結算通知，再部署到 GitHub Pages。

本文件也說明同一套資產追蹤架構中的 Skynet Monitoring。兩個專案分工如下：

| 系統 | 職責 | 正式頁面 |
| --- | --- | --- |
| Growth Dashboard | 資產結算、淨資產、配置、曝險、槓桿、壓力測試、成長軌跡與 Telegram 結算通知 | <https://hanjhou2000716.github.io/tgolaf-fin-tracker/> |
| Skynet Monitoring | 台股加權指數、200MA、VIX、006208 回撤與市場監控沙盒 | <https://hanjhou2000716.github.io/skynet-monitoring/> |

## 1. 系統架構

```text
Google Form / Google Sheets
          │
          ▼
Growth main.py ── 報價、匯率、風控公式、History 快照
          │
          ├── index.html / data.json / status.json
          ├── GitHub Pages（Growth Dashboard）
          └── Telegram 結算通知

外部 Cornjob ── repository_dispatch: trigger_update ──► GitHub Actions

Skynet deploy.yml ── tg_bot_optimized.py ──► market data JSON ──► React UI

health-watchdog.yml ──讀取兩個 status.json ──► 資料過期或來源異常時 Telegram 告警
```

Growth 負責「投資組合事實與風控」；Skynet 不再讀取 Google Sheets，也不重複呈現 Growth 的資產配置與槓桿資料，避免兩套帳務來源不一致。

## 2. Growth Dashboard 資料流程

每次執行 `main.py` 時，系統依序：

1. 使用 `GCP_CREDENTIALS` 服務帳戶讀取 Google Sheets。
2. 找到名稱含 `PRStK` 或 `Growth` 的活頁簿。
3. 從表單回覆／異動工作表累積目前資產；從 `History`、`歷史` 或 `紀錄` 工作表讀取歷史快照。
4. 驗證資產欄位、History 欄位與所有市場報價。
5. 取得台股、美股、美元兌台幣、TAIEX、Nasdaq、VIX 與 006208 資料。
6. 計算資產、負債、淨資產、曝險、槓桿、維持率、每日變化與壓力測試。
7. 將當日快照寫回 History；同一台灣日期只保留一列並更新該列。
8. 產生靜態網頁與 JSON 資料。
9. 依目前結算時段發送 Telegram，並以 History 記錄避免重複通知。

輸出檔案：

- `index.html`：Growth Dashboard 靜態頁面。
- `data.json`、`public/data.json`：儀表板資料契約。
- `status.json`、`public/status.json`：健康狀態與來源診斷。

## 3. Google Sheets / Form 輸入規格

### 資產分類

表單中的資產類型需能對應下列八類：

| 分類 | 內容 |
| --- | --- |
| `台股` | 台股持有股數，例如 2330、006208、00685L |
| `美股` | 美股持有股數，例如 NVDA、TSM、QQQM、QQQ、SPYG、VOO、VTI |
| `基金` | 基金或其他以台幣記錄的持倉金額 |
| `現金_TWD` | 台幣現金 |
| `現金_USD` | 美元現金，結算時換算為台幣 |
| `質押負債` | 目前質押借款本金 |
| `質押利率` | 年利率（百分比） |
| `擔保品` | 質押帳戶中的擔保品股數，例如 006208 |

### 異動模式

程式辨識 `買入`、`存入`、`賣出`、`提領`、`取代`、`覆蓋`、`更新` 等模式；若沒有填模式，預設以「取代」處理。數字可帶逗號、美元符號，以及 `股`、`張`、`萬`、`元`、`塊`、`%` 單位。

程式也會正規化常見代號：`6208 → 006208`、`886 → 00886`、`895 → 00895`、`878 → 00878`、`685L → 00685L`。

### History 必要欄位

最少必須有：

```text
Date, Total_Asset, Net_Asset
```

程式會自動補齊分析欄位：

```text
TW_Stock_Value
US_Stock_Value
Cash_Value
Fund_Value
NVDA_QQQM_Weight
NVDA_SPYG_Weight
NVDA_VOO_Weight
Settlement_Notification_Sent_At
```

最後一欄現在以 JSON 記錄 `us` 與 `tw` 兩個 Telegram 結算時段，讓同一天的 05:40 與 14:45 都能各發送一次。

## 4. 報價與資料來源

### 台股

`quotes.py` 的優先順序：

1. FinMind TaiwanStockPrice（若有 `FINMIND_TOKEN`，失敗會重試一次）。
2. Yahoo Finance Chart API。
3. yfinance。

一般台股先嘗試 `.TW`；場外 ETF `00886` 先嘗試 `.TWO`，再嘗試另一個尾碼。所有來源都必須回傳大於零的數字，否則視為報價失敗。

### 美股與匯率

美股與 `TWD=X` 先使用 Yahoo Chart API，失敗後使用 yfinance。美元兌台幣若兩個來源都失敗，程式使用 32.5 作為保守備援值；正式環境仍應觀察 `status.json` 的來源狀態。

### NVDA ETF 穿透曝險

目前會嘗試從 yfinance ETF holdings 取得 NVDA 權重，失敗時依序使用 History 權重，再使用保守備援值：

| ETF | 備援 NVDA 權重 |
| --- | ---: |
| QQQM | 9.5% |
| SPYG | 7.5% |
| VOO | 7.0% |

NVDA 曝險 = 直接持有 NVDA + QQQM/SPYG/VOO 內含 NVDA 的台幣價值。市場組成圓環圖則另外將 QQQ、VTI 列入「美股市值型」，但目前沒有為 QQQ、VTI 建立 NVDA 穿透權重。

## 5. Growth 計算邏輯

### 資產與負債

```text
美股台幣價值 = 美股美元市值 × USD/TWD
總現金       = 台幣現金 + 美元現金換算台幣
總資產       = 台股 + 美股台幣價值 + 總現金 + 基金
累積利息     = 自 2026-06-10 起，逐日本金 × 年利率 ÷ 365
總負債       = 質押本金 + 累積利息
淨資產       = 總資產 - 總負債
```

### 槓桿與 Beta 容量

```text
投入資產 = 台股 + 美股台幣價值 + 基金
有效槓桿 = (投入資產 + 00685L 市值) ÷ 淨資產
半凱利上限 = 0.08 ÷ (2 × 0.18²) ≈ 1.23 倍
Beta 容量 = 有效槓桿 ÷ 半凱利上限 × 100%
```

燈號規則：

| Beta 容量 | 狀態 | 意義 |
| ---: | --- | --- |
| >115% | 🔴 加原型補現金 | 槓桿超過安全邊界 |
| 95%–115% | 🟡 Beta 維持 | 維持目前槓桿 |
| <95% | 🟢 可加槓桿 | 仍在半凱利安全區內 |

### 質押維持率

```text
質押維持率 = 擔保品市值 ÷ 總負債 × 100%
```

| 維持率 | 狀態 |
| ---: | --- |
| ≥190% | 🟢 可加槓桿 |
| 150%–<190% | 🟡 注意槓桿 |
| <150% | 🔴 補擔保品 |
| 無借款 | ✅ 無借款 |

### TSMC / NVDA 曝險

目前程式採用的 TSMC look-through 權重：

- 2330：100%
- TSM ADR：100%
- 006208：59.4%
- 00685L：72.8%

NVDA 則依上一節的直接持股與 ETF 權重計算。曝險百分比的分母為總資產。需要注意：目前 TSMC 計算尚未納入美股 ETF 內含的 TSMC 權重；若要做到完整穿透，需再建立 QQQM、QQQ、SPYG、VOO、VTI 的 TSMC 權重來源。

### 集中度與壓力測試

- 台股最大單一標的：台股持倉市值最高者 ÷ 總資產。
- 美股最大單一標的：美股持倉市值最高者 ÷ 總資產。
- `>=20%`：觀察。
- `>=35%`：警示。
- 壓力情境：006208 下跌 10% 與 20%。
- 淨資產影響：以台股現貨 006208 市值乘以下跌幅度。
- 維持率影響：從擔保品市值扣除質押帳戶中的 006208 下跌損失後重新計算。

## 6. Growth Dashboard 使用指南

頁面以三個捷徑區分：

### 配置

- 資產配置：台股、美股、現金、基金及各類別日漲跌。
- 雙層圓環圖：內圈為完整總資產板塊（現貨台股、質押台股、現貨美股、現金／基金／其它），外圈為投資市值組成（台股市值型、美股市值型、台積電、台股槓桿型、其它）。
- 圖例移除，游標移過內外圈會顯示不透明高對比提示框，包含分類、台幣金額與該圈占比。

### 風險

- 槓桿：有效 Beta、半凱利安全邊界、容量、質押借款與維持率。
- 曝險：TSMC 與 NVDA 綜合曝險。
- 集中度：台股／美股最大單一標的。
- 壓力測試：006208 下跌 10%／20%。

### 成長

- 成長軌跡：近一月、近一季、近一年、近三年淨資產報酬。
- 目標進度：以 1,000 萬台幣為主要目標，並顯示里程碑。
- 近期資產軌跡：總資產、淨資產與月線／季線／年線。
- 可切換 1M、3M、1Y、全部；支援滾輪／雙指縮放與拖曳回看。

價格與漲跌呈現規則：上漲為紅色、下跌為綠色；類別日漲跌符號為 `📈`（>1%）、`📉`（<-1%）、`🟰`（-1% 至 1%）。

## 7. Telegram 推送規則

### Growth 結算通知

Growth workflow 會接受 `repository_dispatch` 的 `trigger_update`。`main.py` 在以下台灣時間視為通知窗口：

| 窗口 | 時間 | 用途 |
| --- | --- | --- |
| `us` | 05:00–06:59 | 美股結算完成後 |
| `tw` | 14:00–16:59 | 台股結算完成後 |

目前外部 Cornjob 建議在：

```cron
# Asia/Taipei timezone
40 5 * * 2-6
45 14 * * 1-5
```

推送內容為當日淨資產損益，並附上唯一的 Growth Dashboard Web App 按鈕。通知會寫入 History 的 `Settlement_Notification_Sent_At`；同一日期同一窗口重試不會重複發送，但早上 `us` 與下午 `tw` 各會發送一次。

### 手動測試通知

GitHub Actions 的 `workflow_dispatch` 提供 `force_telegram` 勾選項。勾選後會忽略時間窗口與去重標記，立即發送一次測試通知；未勾選時，手動執行仍遵守正常的 05:00–06:59／14:00–16:59 規則。

### 健康告警

`health-watchdog.yml` 會在台灣時間約 06:00 與 17:00 檢查 Growth、Skynet 的 `status.json`。以下任一條件成立就透過 Telegram 告警：

- `status` 不是 `ok`。
- `generatedAt` 超過各自 `staleAfterHours`（目前 18 小時）。
- 任一資料來源狀態不是 `ok`。
- endpoint 無法取得或 JSON 無效。

健康告警的按鈕也只連到 Growth Dashboard；Skynet 的資產配置／槓桿資訊已移除，帳務以 Growth 為唯一來源。

## 8. GitHub Actions 與排程

### Growth `cron.yml`

- `repository_dispatch`：`trigger_update`，由外部 Cornjob 觸發。
- `workflow_dispatch`：GitHub 手動執行；可勾選 `force_telegram` 測試 Telegram。
- 目前沒有啟用內建 schedule。
- 流程：安裝 Python → 執行 14 項測試 → 執行 `main.py` → 驗證 JSON → 發佈 `gh-pages`。
- `concurrency` 使用 `growth-dashboard` 且不取消前一個執行，避免兩次結算同時寫入 Google Sheets。

### Skynet `deploy.yml`

- `repository_dispatch`：`trigger_update`。
- `push` 到 `main`：建置並部署。
- `workflow_dispatch`：手動執行。
- 目前另有備援 GitHub schedule：`14:10` 與 `05:40`（台灣時間）。如果外部台股 Cornjob 改為 `14:45`，Skynet 仍可能在 14:10 多跑一次；要完全避免重複，需另行將 Skynet 備援排程同步為 14:45 或移除該備援。

### 外部 Cornjob 的 dispatch 介面

外部服務需呼叫兩個 repository 的 GitHub API：

```http
POST https://api.github.com/repos/hanjhou2000716/tgolaf-fin-tracker/dispatches
POST https://api.github.com/repos/hanjhou2000716/skynet-monitoring/dispatches
Content-Type: application/json
Authorization: Bearer <GitHub token>

{"event_type":"trigger_update"}
```

不要把 GitHub token、Telegram token 或 GCP JSON 寫入 repository；應放在 Cornjob 的安全變數或 GitHub Secrets。

## 9. GitHub Secrets / 執行環境

Growth Actions 使用：

| Secret | 用途 |
| --- | --- |
| `GCP_CREDENTIALS` | Google service account JSON |
| `FINMIND_TOKEN` | FinMind 台股報價 |
| `TELEGRAM_TOKEN` | Telegram Bot token |
| `TELEGRAM_CHAT_ID` | Telegram 接收者 chat ID |

Skynet Actions 不需要 Google Sheets 或 Telegram 憑證；它只安裝 `yfinance`，產生市場資料後以 Node 18 建置 React。

## 10. 驗證、故障排查與日常操作

### 本地驗證

```powershell
python -m py_compile main.py
python -m unittest discover -s tests -v
git diff --check
```

Skynet：

```powershell
pip install yfinance
python tg_bot_optimized.py
npm install
npm run build
```

### 建議排查順序

1. 先看 GitHub Actions run 是否由 `repository_dispatch` 觸發，以及 `build_and_deploy`／`build` 是否成功。
2. 查看正式頁面的 `status.json`，確認 `status`、`generatedAt`、`snapshotResult` 與 `sources`。
3. 確認 Google Sheets 的工作表名稱、必要欄位與最新異動列。
4. 若台股報價失敗，先看 FinMind token，再確認 Yahoo 尾碼（尤其 `00886.TWO`）。
5. 若 Telegram 未推送，檢查執行時間是否落在 05:00–06:59 或 14:00–16:59、Secrets 是否存在，以及 History 的窗口 marker 是否已存在。
6. 若資料過期，先確認外部 Cornjob 是否成功呼叫兩個 repository 的 dispatch API，再檢查 GitHub Actions 權限與 Pages deployment。

## 11. 已知限制與維護注意事項

- History 一天一列；05:40 與 14:45 會更新同一日期的最新資產快照，不會保留兩列獨立結算紀錄。
- Telegram 早晚通知使用同一份當日損益訊息，若要區分「美股損益」與「台股損益」，需要另做市場別損益欄位。
- TSMC ETF 穿透權重尚未完整納入所有美股 ETF；目前只有直接持股與台股 ETF 固定權重。
- USD/TWD 的 32.5 是報價完全失敗時的備援值，應透過 `status.json` 監控來源狀態。
- Skynet 的 GitHub schedule 是備援排程，與外部 Cornjob 同時啟用時可能產生重複建置；Growth 則沒有啟用內建 schedule。
- GitHub Actions、Google service account、FinMind、Telegram 憑證都應定期輪替，且不可提交到 Git。

## 12. 相關連結

- Growth Dashboard：<https://hanjhou2000716.github.io/tgolaf-fin-tracker/>
- Skynet Monitoring：<https://hanjhou2000716.github.io/skynet-monitoring/>
- 資產輸入表單：<https://forms.gle/9ZEJawwNRGfiXQiV8>
- Growth repository：<https://github.com/hanjhou2000716/tgolaf-fin-tracker>
- Skynet repository：<https://github.com/hanjhou2000716/skynet-monitoring>
