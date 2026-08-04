# P1-PERF-02 損益歸因

私有 payload 新增 `pnlAttribution`，欄位包含台股價格、美股價格、匯率、股息、費用、
質押利息、外部現金流與其它誤差。`other` 是明確的對帳 residual，不會把未解釋的
變化靜默吞掉；所有欄位加總必須等於同日淨資產變化，並以 `reconciled` 驗證。
