---
name: flow-skill
description: 負責目標標的期權大單流向 (Options Flow) 的清洗排序，並由 Gemini AI 解讀 Smart Money 意圖，精選出最佳 1 檔 CALL 買權合約。
---

# Flow Skill (期權大單流向分析與最佳 CALL 挑選技能)

## 📌 職責說明
`Flow Skill` 專門負責在確認內部人選股標的後，深入市場微觀期權大單（Whale Options Flow），過濾 0DTE 極短線噪聲，針對主力吃單方向（Ask / Mid）、成交規模（Premium）與量倉比（Volume > Open Int）進行多維度綜合評分，挑選出最適合波段持倉的 CALL 買權合約。

## ⚙️ 核心流程
1. **數據清洗與排序 (`clean_and_prepare_flow`)**：
   - 篩選 Type 為 CALL 的買權交易。
   - 過濾 0DTE（優先保留 DTE >= 7 天波段時間價值）。
   - 依據成交權利金（Premium）由大至小排序，鎖定機構巨鯨動向。
2. **AI 提示詞構建 (`build_prompt`)**：
   - 設定履約價 Moneyness（Delta 0.30 ~ 0.70 兼顧槓桿與抗衰退能力）。
   - 考量 DTE 14 ~ 90 天，給予內部人利多催化劑足夠的發酵空間。
3. **合約規格結構化解析 (`select_best_call`)**：
   - 呼叫 Gemini AI 模型鏈，精準提取履約價 (Strike)、到期日 (Exp Date YYYYMMDD)、參考價格。
   - 內建安全降級機制（若 AI 回傳異常，自動選取最高金額大單之規格）。

## 🚀 API 快速導覽
```python
from trade.skills.flow_skill import OptionsFlowSkill

skill = OptionsFlowSkill()

result = skill.select_best_call(
    csv_path="trade/Barchart/uber-options-flow-09-13-2026.csv",
    symbol="UBER",
    max_retries=3
)

contract = result["contract"]
print(contract["symbol"])     # e.g. "UBER"
print(contract["strike"])     # e.g. 75.0
print(contract["exp_date"])   # e.g. "20261120"
print(contract["ref_price"])  # e.g. 4.15
```

## 🧪 獨立測試指令
```powershell
python trade/skills/flow_skill/flow.py
```
