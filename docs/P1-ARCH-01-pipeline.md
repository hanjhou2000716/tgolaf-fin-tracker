# P1-ARCH-01 主流程分層

`main.py` 現在只保留相容啟動入口；既有資料讀取、行情、估值、風控、輸出與通知流程移到 `dashboard_pipeline.py`，所以 GitHub Actions 與本機既有的 `python main.py` 呼叫不需要改變。後續領域 PR 可再從 pipeline 抽出獨立模組，而不必繼續堆疊入口檔。
