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

季度候選使用上一季截止日前的 point-in-time 研究價格：μ 最高 8%，σ 最低 18%，`halfKelly = μ / (2 × σ²)`。候選先寫入私有 artifact，人工核准後才可成為下一季正式參數。

Beta 容量 `<95%` 為綠色，`95%–<115%` 為黃色，`>=115%` 為紅色。正式加槓桿 Gate 要求容量低於 115%、維持率至少 167%（或無負債）、資料健康且 Beta 覆蓋通過。

## 相容欄位

`effectiveLeverage`、`kellyLimit`、`betaCapacity` 與 `betaStatus` 暫時保留。前端正式讀取 `risk.beta.navBeta`；舊欄位僅供相容與影子比較，未經獨立移除計畫不得改義。
