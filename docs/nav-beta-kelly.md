# NAV Beta 與季度凱利契約

Growth 的風險卡以 `risk.beta.navBeta` 為正式主值。市場價值在進入計算器前必須先去重；擔保品只記錄所有權與維持率，不另加一份資產。

```text
NAV = totalAsset - totalDebt
betaExposureTwd = Σ(valueTwd × assetBeta)
assetBeta = betaExposureTwd / totalAsset
grossLeverage = totalAsset / NAV
navBeta = betaExposureTwd / NAV
```

現金與負債的 Beta 為 0；006208 固定為 1.0；00685L 固定為 2.0。其他標的必須由已驗證的季度 Beta 覆蓋；不可把缺值、過期值或 fallback 價格轉成 0。

Beta 係數與 Kelly 參數每季以截止日前的 point-in-time 研究價格自動估計。Beta 使用最近三年、至少 104 組連續完成週報酬；Kelly 的 μ 使用五年總報酬 CAGR（最高 8%），σ 使用最近三年週報酬波動率（最低 18%），`halfKelly = μ / (2 × σ²)`。資料來源、公司行動回應、估計區間及內容雜湊通過自動驗證後，政策自動生效，不需要人工季度核准。

NAV Beta 每次結算使用當次持倉與負債重算。季度 Beta 候選必須再通過當次組合的覆蓋率至少 95%、且沒有市值達 NAV 1% 的未建模持倉，才寫入正式政策。候選與原始公開行情證據保存在只從成功 `main` Actions 還原的私有 artifact；PR 及其他分支產物不會成為正式參數。

來源失敗時沿用尚有效的季度政策；跨季新政策未通過時，上一季參數最多顯示一季的灰色參考值並禁止增加風險。參考期結束或其他資料品質不合格時，正式值顯示不可用。舊版 `APPROVED` 政策仍可供相容讀取與短期參考；新版本使用 `AUTO_VALIDATED`，不偽裝成人工核准。

資訊卡的「容量」是有正負號的剩餘 Beta 空間：`halfKellyLimit - navBeta`；「使用率」是 `navBeta / halfKellyLimit × 100%`。負容量代表已超過半凱利邊界，不能顯示成零或省略。

Beta 容量 `<95%` 為綠色，`95%–<115%` 為黃色，`>=115%` 為紅色。正式加槓桿 Gate 要求容量低於 115%、維持率至少 167%（或無負債）、資料健康且 Beta 覆蓋通過。

## 相容欄位

`effectiveLeverage`、`kellyLimit`、`betaCapacity` 與 `betaStatus` 暫時保留。前端正式讀取 `risk.beta.navBeta`；舊欄位僅供相容與影子比較，未經獨立移除計畫不得改義。
