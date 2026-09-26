---
name: long-call-selection-skill
description: 負責 Barchart Long Call Options Screener 官方數據清洗聚合、14天冷卻期物理排除，並呼叫 Gemini AI 精選出唯一最佳買權 (Long Call) 合約。
---

# Long Call Selection Skill (Long Call 選擇權選股與量化分析技能)

## 📌 職責說明
`Long Call Selection Skill` 負責處理來自 `https://www.barchart.com/options/long-call-options-screener` 的官方精選買權表格數據。它能計算真金白銀流入規模，落實【資料層物理剔除】與【Prompt 風控約束】雙重防重複機制，並調用 Gemini AI 大模型進行多因子量化評估，精選出唯一最佳 ITM/ATM Long Call 合約。

## ⚙️ 核心流程
1. **數據清洗與聚合 (`clean_and_aggregate`)**：
   - 清理 Strike, DTE, Ask, Volume, Open Int, IV, Delta, Profit Prob 等數據。
   - 計算每口建倉成本 (`Ask * 100`) 與單日成交金額規模 (`Ask * Volume * 100`)。
   - **【物理過濾】**：接收 `Memory Skill` 排除名單，直接從 DataFrame 移除冷卻中 (14天) 或黑名單標的。
2. **AI 提示詞生成 (`build_prompt`)**：
   - 評估大單真金白銀注入量、未平倉量流動性、Delta (0.70~0.95 深價內現貨替代)、DTE (14~45天) 及隱含波動率。
   - 明確禁止大模型重複選擇冷卻清單中的標的。
3. **最佳合約決策 (`select_best_contract`)**：
   - 呼叫 Gemini AI 進行多因子評分。
   - 解析結構化 Markdown，提取合約細節 (Symbol, Strike, Exp Date, Ask, Delta 等)。
   - 附帶備援候選列表 (`top_candidates`)，供後續 IBKR 智能下單模組進行即時合約驗證與容錯切換。
