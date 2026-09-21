# PositionOrderSkill (IBKR 未平倉校驗與 Adaptive Patient 定位下單技能)

## 📌 技能簡介
本 Skill 參考 `trade/barchart_placeOrder.py` 的下單與合約處理機制，專門負責連接 IBKR，檢驗指定標的（如期貨 `TAV26`、`ES`、`NQ` 等或美股標的）的當前未平倉口數，並根據多空訊號（`BUY` 目標為 `+1`，`SELL` 目標為 `-1`）執行自適應耐心單 (Adaptive Patient) 精準校正持倉。

## 🎯 核心職責
1. **期貨與標的合約智能解析**：
   - 支援 Barchart 代號（如 `TAV26`）至 IBKR 合約（如 CME `MET` 202610）之自動別名對應與合約驗證 (`qualifyContracts`)。
   - 同時支援常規期貨月分代碼（`F`, `G`, `H`, `J`, `K`, `M`, `N`, `Q`, `U`, `V`, `X`, `Z`）與股票合約。

2. **精確倉位查詢與多空目標判斷**：
   - 連接 IBKR 帳戶，過濾查詢目標標的當前未平倉淨部位 (`current_pos`)。
   - **BUY 訊號**：
     - 若 `current_pos == +1`：持倉已達標，不執行重複送單。
     - 若 `current_pos != +1`：計算所需差額（如從 0 買進 1 口、從 -1 買進 2 口反轉為 +1），下達 BUY 委託。
   - **SELL 訊號**：
     - 若 `current_pos == -1`：持倉已達標，不執行重複送單。
     - 若 `current_pos != -1`：計算所需差額（如從 0 賣出 1 口、從 +1 賣出 2 口反轉為 -1），下達 SELL 委託。

3. **IBKR Adaptive Patient 演算法送單**：
   - 預查即時最佳買賣報價 (Bid / Ask / MarketPrice)。
   - 套用 `Adaptive` 演算法與 `Patient` 優先權參數，兼顧成交速度與滑點控制。
   - 支援 `--dry-run` 模擬模式。

4. **手機 LINE 狀態推播**：
   - 整合 `trade/notifier.py`，於持倉符合或送出調倉單後即時推播詳細明細至用戶手機。
