---
name: call-put-flow-skill
description: 專職於 Barchart 建議 1 (個股) 與建議 2 (ETF) 之期權大單流向 (Options Flow) 雙向清洗與多空對比評選，由 Gemini AI 精選出勝率最高的一檔 Put 或 Call 進行下單。
---

# Call/Put Flow Skill (雙向期權大單流向與最佳 Put/Call 決選技能)

## 📌 職責說明
`CallPutFlowSkill` 是專為 `barchart_auto.py` 量化管道所設計的高階決策技能。
不同於 `trade/insider.py` 的 `flow_skill` 僅鎖定單一標的之 CALL 買權，本技能具備：
1. **多空雙向期權大單解析**：同時支援 **BUY CALL** 與 **BUY PUT** 大單數據清洗與評估。
2. **多標的資金意圖橫向對比**：接收來自異常期權 (UOA) 分析之「建議 1（個股）」與「建議 2（ETF）」，深入各標的的微觀 Options Flow。
3. **Smart Money 意圖決選**：透過成交金額（Premium）、主動吃單方向（Ask / Mid vs Bid）、量倉比（Volume > Open Int）與 Moneyness，決選出最佳勝率的 1 檔 Put 或 Call。

## ⚙️ 核心流程
1. **雙向數據清洗 (`clean_and_prepare_flow`)**：
   - 支援過濾指定方向（CALL 或 PUT）或全向保留。
   - 過濾 0DTE 極短線雜訊（優先保留 DTE >= 7 天）。
   - 統計多空權利金總額比（Put/Call Premium Ratio）、主動買方吃單比例（Ask-side Buyer Aggression）。
2. **橫向對比 Prompt 構建 (`build_comparison_prompt`)**：
   - 彙整 Candidate 1 與 Candidate 2 的合約基礎、量化指標與 Whale Flow 排序。
   - 要求 Gemini AI 扮演資深衍生品造市商，全方位對比機構資金實力與風險收益比。
3. **決選最佳合約 (`evaluate_best_put_call`)**：
   - 結構化輸出 WINNER（建議 1 或 建議 2），確立最終送單標的、履約價、到期日與參考價。
   - 內建安全降級（Fallback）：若 AI 服務超時或無回應，以主力主動性買盤（Ask-side Premium）最大者自動勝出。

## 🚀 API 快速導覽
```python
from trade.skills.call_put_flow_skill import CallPutFlowSkill

skill = CallPutFlowSkill()

result = skill.evaluate_best_put_call(
    candidate1={"id": 1, "symbol": "MSTR", "strategy": "Buy Call", "strike": 152.5, "exp_date": "20260925", "ref_price": 5.4},
    candidate2={"id": 2, "symbol": "IWM", "strategy": "Buy Put", "strike": 279.0, "exp_date": "20261009", "ref_price": 2.75},
    csv_path1="trade/Barchart/mstr-options-flow.csv",
    csv_path2="trade/Barchart/iwm-options-flow.csv",
)

winner = result["winner"]
print(winner["symbol"])      # e.g. "MSTR"
print(winner["strategy"])    # e.g. "Buy Call"
print(winner["strike"])      # e.g. 152.5
print(winner["exp_date"])    # e.g. "20260925"
print(winner["action"])      # "BUY"
print(winner["right"])       # "C" or "P"
```
