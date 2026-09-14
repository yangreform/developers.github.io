#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Flow Skill: 期權大單流向分析與最佳 CALL 挑選技能 (trade/skills/flow_skill/flow.py)
================================================================================
職責：
  1. 清洗目標標的之 Options Flow 大單數據，過濾 0DTE 極短線雜訊
  2. 依成交權利金 (Premium) 與主力吃單方向 (Ask / Mid) 排序最具代表性大單
  3. 構建專業期權量化提示詞，呼叫 Gemini AI
  4. 解讀 Smart Money 意圖，精選出最佳 1 檔波段 CALL 合約（Strike, Exp Date, Delta）
================================================================================
"""

import os
import sys
import re
import time
import datetime
import pandas as pd

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 路徑設定
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
for p in [BASE_DIR, PROJECT_ROOT]:
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    from barchart_analysis import load_gemini_api_key
except ImportError:
    load_gemini_api_key = None

try:
    from trade.skills.gemini_helper import call_gemini_for_skill
except ImportError:
    try:
        from skills.gemini_helper import call_gemini_for_skill
    except ImportError:
        call_gemini_for_skill = None


class OptionsFlowSkill:
    """
    期權大單流向分析與合約挑選技能
    """

    def __init__(self, api_key=None, env_path=None):
        self.env_path = env_path or os.path.join(BASE_DIR, ".env")
        self.api_key = api_key or (load_gemini_api_key(self.env_path) if load_gemini_api_key else None)

    def clean_and_prepare_flow(self, csv_path, symbol, min_dte=7, top_count=40):
        """
        清洗目標股票的 Options Flow 數據，專注於 CALL 買權大單，過濾 0DTE 雜訊。
        """
        if not os.path.exists(csv_path):
            raise FileNotFoundError(f"找不到期權大單 CSV: {csv_path}")

        sym_clean = symbol.strip().upper()
        print(f"[INFO] [FlowSkill] 讀取 {sym_clean} Options Flow 數據: {os.path.basename(csv_path)}")
        df = pd.read_csv(csv_path)

        # 移除無效與 footer 行
        df = df[df["Symbol"].notna() & (~df["Symbol"].astype(str).str.contains("Downloaded", case=False, na=False))]

        # 數值型態清理
        numeric_cols = ["Price~", "Strike", "DTE", "Trade", "Size", "Premium", "Volume", "Open Int", "Delta"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col].astype(str).str.replace(",", "").str.replace("$", ""), errors="coerce")

        # 專注於 CALL 買權
        df_call = df[df["Type"].astype(str).str.upper() == "CALL"].copy()
        if df_call.empty:
            raise ValueError(f"[FlowSkill] 在 {os.path.basename(csv_path)} 中找不到任何 CALL 買權數據。")

        # 過濾 0DTE (優先保留 DTE >= min_dte)
        df_call_filtered = df_call[df_call["DTE"] >= min_dte]
        if df_call_filtered.empty:
            df_call_filtered = df_call[df_call["DTE"] >= 1]
        if df_call_filtered.empty:
            df_call_filtered = df_call

        # 依成交權利金 (Premium) 降序排序
        if "Premium" in df_call_filtered.columns:
            df_call_filtered = df_call_filtered.sort_values(by="Premium", ascending=False)

        top_calls = df_call_filtered.head(top_count)
        print(f"[INFO] [FlowSkill] {sym_clean} Options Flow 清洗完成：Call 交易 {len(df_call)} 筆，篩選代表性大單 {len(top_calls)} 筆")

        cols_to_show = [c for c in ["Symbol", "Price~", "Exp Date", "Type", "Strike", "DTE", "Trade", "Size", "Side", "Premium", "Volume", "Open Int", "IV", "Delta", "Time"] if c in top_calls.columns]
        data_str = top_calls[cols_to_show].to_string(index=False)
        return data_str, top_calls

    def build_prompt(self, symbol, flow_data_str):
        """
        組裝 Whale Options Flow 量化提示詞
        """
        sym_clean = symbol.strip().upper()
        prompt = f"""你是一名華爾街期權造市商與主力大單（Whale Options Flow）量化專家。
我們已透過內部人交易（Insider Trading）篩選出最強標的：{sym_clean}。
現在請針對該標的在市場上的最新期權大單流向（Options Flow）進行深度解讀：

【{sym_clean} 期權大單流向 (CALL 買權清單)】
{flow_data_str}

【篩選原則與目標】
請在以上清單中，挑選出「最好、勝率最高的一檔 CALL 合約」：
1. **主力主動掃貨 (Aggressive Whale Flow)**：優先考慮在 Ask 或 Above Ask 成交的大額買單（Side: ask / mid），成交權利金（Premium）龐大，顯示機構急於建倉。
2. **期限最適性 (Horizon / DTE)**：避免當天或極短線到期的期權，優先選擇 14 ~ 90 天到期（DTE: 14~90天）的波段合約，給予內部人利多發酵足夠時間。
3. **行使價與 Delta (Moneyness)**：Delta 位於 0.30 ~ 0.70 之間（ATM 或近價外 OTM），兼顧槓桿爆發力與抗時間價值流失能力。
4. **流動性與未平倉量**：成交量（Volume）超越未平倉（Open Int），代表發動型新倉（ToOpen）。

【輸出格式規範】
請務必嚴格依循下列 Markdown 結構輸出，數據（履約價、到期日、現價）必須清晰以利程式解析下單：

#### 📌 【最佳期權 CALL 投資建議】
- **推薦標的代號 (Symbol)**: {sym_clean}
- **建議策略**: Buy Call
- **期權類型**: Call
- **履約價 (Strike)**: $[請填寫數值，例如: 550.0]
- **到期日 (Exp Date)**: [請填寫 YYYY-MM-DD，例如: 2026-10-16]
- **到期天數 (DTE)**: [XX] 天
- **參考價格 / 權利金 (Reference Price)**: $[請填寫數值，例如: 49.00]
- **Delta**: [0.XXXX]
- **進場理由與大單流向深度分析**:
  1. [機構大單掃貨細節：成交金額、買方主動性 Side、量倉比]
  2. [履約價挑選邏輯與技術面目標價配合程度]
  3. [盈虧比與波段潛力解析]
"""
        return prompt

    def parse_call_contract(self, report_text, default_symbol="DELL"):
        """
        自期權分析報告中解析期權合約規格：symbol, strike, exp_date (YYYYMMDD), ref_price
        """
        sym_clean = default_symbol.strip().upper()

        # 標的代號
        patterns = [
            r'推薦標的代號[^\n:]*[：:]\s*[\*`]*([A-Za-z0-9]+)',
            r'Symbol[^\n:]*[：:]\s*[\*`]*([A-Za-z0-9]+)',
            r'標的代號[^\n:]*[：:]\s*[\*`]*([A-Za-z0-9]+)',
        ]
        for p in patterns:
            m = re.search(p, report_text, re.IGNORECASE)
            if m:
                s = m.group(1).strip().upper()
                if s not in ("BUY", "CALL", "PUT", "STOCK", "NONE", "N/A", "SYMBOL"):
                    sym_clean = s
                    break

        # 履約價
        strike_match = re.search(r'(?:履約價|Strike)[^\n:]*[：:]\s*[\*\$]*\s*(\d+\.?\d*)', report_text, re.IGNORECASE)
        strike = float(strike_match.group(1)) if strike_match else None
        if not strike:
            m_st = re.search(r'Strike\s*[:\*\$]*\s*(\d+\.?\d*)', report_text, re.IGNORECASE)
            if m_st:
                strike = float(m_st.group(1))

        # 到期日
        exp_match = re.search(r'到期日[^\d]*(\d{4}[-/]\d{2}[-/]\d{2})', report_text)
        exp_date = exp_match.group(1).replace("-", "").replace("/", "") if exp_match else None

        # 參考價格 / 權利金
        ref_match = re.search(r'(?:參考價格|權利金|Trade|Ask)[^\$\d\n]*\$?(\d+\.?\d*)', report_text, re.IGNORECASE)
        ref_price = float(ref_match.group(1)) if ref_match else 1.0

        return {
            "symbol": sym_clean,
            "strike": strike,
            "exp_date": exp_date,
            "ref_price": ref_price,
            "strategy": "Buy Call",
            "right": "C",
            "action": "BUY",
        }

    def select_best_call(self, csv_path, symbol, max_retries=3):
        """
        全流程執行期權大單解讀並挑選最佳 CALL 合約
        """
        if not self.api_key:
            raise ValueError("[FlowSkill] 未提供 Gemini API Key，請檢查 trade/.env")

        sym_clean = symbol.strip().upper()

        # 1. 清洗數據
        flow_data_str, top_calls = self.clean_and_prepare_flow(csv_path, sym_clean)

        # 2. 構建 Prompt
        prompt = self.build_prompt(sym_clean, flow_data_str)

        # 3. 呼叫 Gemini AI
        call_report = None
        for attempt in range(1, max_retries + 1):
            print(f"[INFO] [FlowSkill] 正在向 Gemini 請求 {sym_clean} 期權大單分析 (嘗試第 {attempt}/{max_retries} 次)...")
            try:
                report = call_gemini_for_skill(prompt, self.api_key) if call_gemini_for_skill else None
                if report and len(report.strip()) > 100:
                    parsed = self.parse_call_contract(report, default_symbol=sym_clean)
                    if parsed["strike"] and parsed["exp_date"]:
                        call_report = report
                        print(f"[SUCCESS] [FlowSkill] 第 {attempt} 次嘗試成功解析最佳 CALL: Strike={parsed['strike']}, Exp={parsed['exp_date']}, RefPrice=${parsed['ref_price']}")
                        break
                    else:
                        print(f"[WARN] [FlowSkill] 第 {attempt} 次報告未包含完整合約參數，重試中...")
            except Exception as e:
                print(f"[WARN] [FlowSkill] 第 {attempt} 次請求異常: {e}")
            time.sleep(2)

        # 備援：若 AI 產出解析失敗，自最高權利金的大單中自動提取合約規格
        if not call_report:
            print(f"[WARN] [FlowSkill] AI 報告取得或解析失敗，啟動備援機制：自成交金額最高之大單直接取樣...")
            top_row = top_calls.iloc[0]
            strike = float(top_row["Strike"])
            exp_raw = str(top_row["Exp Date"])
            exp_date = exp_raw.replace("-", "").replace("/", "")
            ref_price = float(top_row.get("Trade", top_row.get("Price~", 1.0)))
            contract = {
                "symbol": sym_clean,
                "strike": strike,
                "exp_date": exp_date,
                "ref_price": ref_price,
                "strategy": "Buy Call",
                "right": "C",
                "action": "BUY",
            }
            call_report = f"（備援模式）選自最高權利金大單：{sym_clean} Strike {strike} Exp {exp_date} Ref ${ref_price}"
        else:
            contract = self.parse_call_contract(call_report, default_symbol=sym_clean)

        return {
            "status": "ok",
            "symbol": sym_clean,
            "contract": contract,
            "report_text": call_report,
            "top_calls_count": len(top_calls),
        }


if __name__ == "__main__":
    print("=" * 60)
    print(" Flow Skill 獨立單元測試")
    print("=" * 60)

    import glob

    skill = OptionsFlowSkill()

    # 尋找現存任意 options flow CSV
    flow_files = glob.glob(os.path.join(BASE_DIR, "Barchart", "*options-flow*.csv")) + glob.glob(os.path.join(BASE_DIR, "Barchart", "old", "*options-flow*.csv"))
    if not flow_files:
        print("[ERROR] 找不到任何 Options Flow CSV 測試檔")
        sys.exit(1)

    flow_files.sort(key=os.path.getmtime, reverse=True)
    test_file = flow_files[0]
    sym = "UBER"
    for cand in ["UBER", "GME", "DELL"]:
        if cand.lower() in os.path.basename(test_file).lower():
            sym = cand
            break

    print(f"使用測試標的: {sym}, 測試檔: {os.path.basename(test_file)}")
    data_str, df_calls = skill.clean_and_prepare_flow(test_file, sym)
    print(f"Top 3 代表性大單:")
    print(df_calls[["Symbol", "Type", "Strike", "Exp Date", "DTE", "Premium", "Delta"]].head(3).to_string(index=False))
