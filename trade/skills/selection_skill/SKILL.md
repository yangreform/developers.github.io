---
name: selection-skill
description: 負責內部人籌碼數據 (Insider Trading Activity) 的清洗聚合、重複標的物理排除，並呼叫 Gemini AI 選拔出唯一最佳標的。
---

# Selection Skill (內部人選股與防重複過濾技能)

## 📌 職責說明
`Selection Skill` 負責處理高層主管與董事會成員在公開市場的真金白銀「Buy / Contract Buy」籌碼數據。它能有效排除期權轉移等雜訊，並在【資料層】與【AI 提示詞層】落實雙重防重複過濾，確保 AI 永遠只在當前未持有的新標的中挑選出最具爆發力的標的。

## ⚙️ 核心流程
1. **數據清洗與聚合 (`clean_and_aggregate`)**：
   - 篩選 Transaction 為 BUY 的真實增持交易。
   - 計算各標的內部人淨買入總額（`Net_Buy_Total = Buy_Total - Sell_Total`）。
   - **【物理過濾】**：接收 `Memory Skill` 提供之排除清單，直接從 DataFrame 移除冷卻中標的。
2. **AI 提示詞生成 (`build_prompt`)**：
   - 針對高階管理層信念（Conviction）、多高管協同掃貨（Cluster Buying）進行評分。
   - 明確標示近期已推薦清單，禁止大模型二度選擇。
3. **最佳標的決策 (`select_best_symbol`)**：
   - 呼叫 Gemini AI 模型鏈（優先使用 `gemini-3.7-flash` 與 `gemini-3.6-flash`）。
   - 解析結構化 Markdown，提取推薦標的代號。
   - 支援安全降級備援（Fallback to Top Net Buy Candidate）。

## 🚀 API 快速導覽
```python
from trade.skills.memory_skill import SelectionMemory
from trade.skills.selection_skill import InsiderSelectionSkill

memory = SelectionMemory()
skill = InsiderSelectionSkill()

result = skill.select_best_symbol(
    csv_path="trade/Barchart/insider-trading-activity-09-13-2026.csv",
    memory_skill=memory,
    max_retries=3
)

print(result["symbol"])           # e.g. "DELL"
print(result["company_name"])     # e.g. "Dell Technologies Inc"
print(result["insider_net_buy"])  # e.g. 25000000.0
print(result["report_text"])      # 完整 AI 分析報告
```

## 🧪 獨立測試指令
```powershell
python trade/skills/selection_skill/selection.py
```
