# P0-OPS-01 可重現建置與最小權限

- Python runtime 使用 `requirements.lock`，Build 與 watchdog 不再依賴未鎖定的套件版本。
- 所有 GitHub Actions 均固定到 commit SHA。
- Build job 僅有 `contents: read`，產生並上傳已驗證的 public-site artifact。
- Deploy job 才取得 `contents: write`，只下載 artifact 並發布 GitHub Pages。
- 已啟用 CodeQL 與 Dependabot；public Demo 的敏感資料掃描仍在 Build job 執行。
