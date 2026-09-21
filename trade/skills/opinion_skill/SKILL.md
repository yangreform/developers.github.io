# BarchartOpinionSkill (Barchart 技術指標與方向性分析技能)

## 📌 技能簡介
本 Skill 專門負責自 Barchart 擷取期貨或股票標的之技術觀點 (Opinion) 頁面數據，特別鎖定並結構化解析「7 Day Average Directional Indicator」方向性指標（BUY / SELL），做為波段趨勢對沖與自動調倉之核心依據。

## 🎯 核心職責
1. **網址與標的代號解析**：
   - 支援完整 Barchart Opinion 網址（如 `https://www.barchart.com/futures/quotes/TAV26/opinion`）或單純標的代碼（如 `TAV26`）。
   - 自動自網址路徑精準萃取合約標的名稱（如 `TAV26`）。

2. **頁面結構化定位與解析**：
   - 透過瀏覽器驅動載入頁面，自動關閉彈跳視窗及 Cookie 提示。
   - 定位 `indicator-item-title` 為 `7 Day Average Directional Indicator` 的表格列 (`<tr>`)。
   - 從右側 `indicator-item-signal` 精準提取訊號值（`BUY`、`SELL` 或 `HOLD`）。
   - 備份提取整體 Opinion 概要（例如 `Overall Opinion`、`100% BUY` 等），以供決策輔助。

3. **容錯與重試機制**：
   - 若網路延遲或元件未渲染，自動進行重試等待。
   - 回傳標準結構化字典，供後續下單技能無縫串接。
