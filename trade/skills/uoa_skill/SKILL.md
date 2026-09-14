# UoaAnalysisSkill (異常期權與垂直價差量化分析技能)

## 📌 技能簡介
本 Skill 專門負責 Barchart 選擇權數據之量化清洗、智能降噪、垂直價差備援與多模型鏈式 AI 解讀。

## 🎯 核心職責
1. **多源期權數據清洗**：
   - 個股異常期權 (Stock UOA) 與 ETF 異常期權 (ETF UOA)：自動過濾 0DTE 極短線雜訊（`7 <= DTE <= 120`），依量倉比 (`Vol/OI`) 降序排序。
   - Bull Put 垂直價差：過濾歷史過期合約，自動結合歷史 `old/` 目錄備援，依跌破機率由低至高、最大報酬率由高至低篩選。
2. **共享之 Gemini Helper 調度**：
   - 共享 `trade/skills/gemini_helper.py`。
   - 優先調用經實測最為穩定高速之 `gemini-3.6-flash` (~3s)，次選 `gemini-3.8-flash`，最後備援 `gemini-3.7-flash`。
   - 杜絕舊版因 `gemini-3.7-flash` 頻繁高負載 503 與 90 秒連線超時所造成的系統凍結。
3. **報告完整性自動校驗 (`is_report_complete`)**：
   - 嚴格校驗回傳文本長度與三大投資建議（個股突破、ETF 趨勢、Bull Put 垂直價差），未達標自動容錯重試。
4. **結構化落盤與手機推播**：
   - 儲存時間戳記歸檔報告與各層級最新指標文字檔 (`latest_ai_analysis.txt`)。
   - 智慧擷取速覽摘要，透過 `notifier.py` 推播至用戶手機 LINE。
