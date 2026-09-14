#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Selection Skill: 內部人籌碼數據清洗與 AI 選股技能 (trade/skills/selection_skill/selection.py)
================================================================================
職責：
  1. 清洗全市場內部人交易活動 (Insider Trading Activity) CSV 數據
  2. 聚合內部人真金白銀淨買入 (Net Buy)、筆數與關鍵高管持股動向
  3. 【雙重防重複機制】：
     - 資料層：從 DataFrame 直接剔除冷卻中或黑名單之標的
     - 提示詞層：在 Prompt 中明確禁止 AI 重複推薦歷史標的
  4. 呼叫 Gemini AI 進行深度量化分析，選出「唯一最佳標的」
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


class InsiderSelectionSkill:
    """
    內部人交易籌碼分析與選股技能
    """

    def __init__(self, api_key=None, env_path=None):
        self.env_path = env_path or os.path.join(BASE_DIR, ".env")
        self.api_key = api_key or (load_gemini_api_key(self.env_path) if load_gemini_api_key else None)

    def clean_and_aggregate(self, csv_path, excluded_symbols=None, top_buys_count=40, top_volume_count=25):
        """
        清洗內部人交易數據，聚合個股淨買入與高管明細，主動過濾排除名單。
        """
        if not os.path.exists(csv_path):
            raise FileNotFoundError(f"找不到內部人交易 CSV: {csv_path}")

        print(f"[INFO] [SelectionSkill] 讀取內部人交易數據: {os.path.basename(csv_path)}")
        df = pd.read_csv(csv_path)

        # 移除無效與 footer 行
        df = df[df["Symbol"].notna() & (~df["Symbol"].astype(str).str.contains("Downloaded", case=False, na=False))]

        # 數值型態轉換
        df["Shares"] = pd.to_numeric(df["Shares"].astype(str).str.replace(",", ""), errors="coerce")
        df["@Price"] = pd.to_numeric(df["@Price"].astype(str).str.replace(",", "").str.replace("$", ""), errors="coerce")
        df["Trans Total"] = pd.to_numeric(df["Trans Total"].astype(str).str.replace(",", "").str.replace("$", ""), errors="coerce")
        df["Shares After"] = pd.to_numeric(df["Shares After"].astype(str).str.replace(",", ""), errors="coerce")

        # 符號大寫化
        df["Symbol"] = df["Symbol"].astype(str).str.strip().str.upper()

        # 【資料層物理過濾】：排除冷卻中與黑名單標的
        excluded_set = {s.upper() for s in (excluded_symbols or [])}
        if excluded_set:
            original_len = len(df)
            df = df[~df["Symbol"].isin(excluded_set)]
            filtered_count = original_len - len(df)
            print(f"[INFO] [SelectionSkill] 資料層已物理剔除 {filtered_count} 筆排除標的數據 (排除名單: {sorted(list(excluded_set))})")

        # 篩選真實買盤 (Buy, Contract Buy)
        buys_mask = df["Transaction"].astype(str).str.upper().str.contains("BUY")
        df_buys = df[buys_mask].sort_values(by="Trans Total", ascending=False)

        # 篩選賣盤 (Sell, Sale Post-exercise)
        sells_mask = df["Transaction"].astype(str).str.upper().str.contains("SELL|SALE")
        df_sells = df[sells_mask].sort_values(by="Trans Total", ascending=False)

        # 聚合買入
        buy_agg = df[buys_mask].groupby("Symbol").agg(
            Buy_Total=("Trans Total", "sum"),
            Buy_Shares=("Shares", "sum"),
            Buy_Trades=("Transaction", "count"),
            Company_Name=("Name", "first"),
            Insiders=("Insider Name", lambda s: ", ".join(s.dropna().unique()[:3])),
            Titles=("Title", lambda s: ", ".join(s.dropna().unique()[:3])),
        ).reset_index()

        # 聚合賣出
        sell_agg = df[sells_mask].groupby("Symbol").agg(
            Sell_Total=("Trans Total", "sum"),
            Sell_Trades=("Transaction", "count"),
        ).reset_index()

        # 合併計算淨買入總額
        merged = pd.merge(buy_agg, sell_agg, on="Symbol", how="left").fillna(0)
        merged["Net_Buy_Total"] = merged["Buy_Total"] - merged["Sell_Total"]
        merged = merged.sort_values(by="Net_Buy_Total", ascending=False)

        # 排除淨買入 <= 0 的標的 (淨賣出標的直接剔除)
        merged = merged[merged["Net_Buy_Total"] > 0]

        top_summary = merged.head(top_volume_count)
        top_buys_detail = df_buys.head(top_buys_count)[
            ["Symbol", "Name", "Insider Name", "Title", "Transaction", "Shares", "@Price", "Trans Total", "Date"]
        ]

        print(f"[INFO] [SelectionSkill] 數據清洗完成：候選買盤 {len(df_buys)} 筆，淨買入合格標的 {len(merged)} 檔")

        summary_text = f"""【標的內部人淨買入總額排行榜 (Top {len(top_summary)})】
{top_summary.to_string(index=False)}

【頂尖單筆內部人買入明細 (Top {len(top_buys_detail)})】
{top_buys_detail.to_string(index=False)}
"""
        return summary_text, top_summary, df_buys

    def build_prompt(self, data_summary_text, excluded_symbols=None):
        """
        組裝高階內部人量化提示詞，附帶排除標的警告
        """
        excluded_note = ""
        if excluded_symbols:
            excluded_str = ", ".join(sorted(list(set(excluded_symbols))))
            excluded_note = f"""
⚠️ 【重要風控約束：已持有/歷史推薦冷卻中標的】
以下標的已在投資組合中或剛完成推薦，【嚴禁再次推薦】：
[ {excluded_str} ]
請務必從上述清單【以外】的其他合格標的中，挑選出唯一最強的一檔股票！
"""

        prompt = f"""你是一名華爾街頂級股權籌碼與內部人交易（Insider Trading）量化研究主管。
以下是 Barchart 提供的最新全市場內部人交易活動（Insider Trading Activity）清洗與聚合數據：

{data_summary_text}
{excluded_note}
【分析要求與評估維度】
1. **內部人真金白銀持股信念 (Conviction)**：
   - 聚焦高階主管（CEO、CFO、董事會主席、核心董事、10% 大股東）直接在公開市場斥資「Buy / Contract Buy」的行為，過濾掉單純期權行權轉出（Transfer Out/Sale Post-exercise）。
   - 買入金額越大、多位內部人協同買入（Cluster Buying）或相較於過往持股顯著倍增者為最核心加分項。
2. **基本面與產業景氣循環**：
   - 評估該標的是否處於產業轉折點、AI/伺服器科技循環、景氣防禦、或價值重估階段。
3. **選出「唯一最好一檔商品」**：
   - 綜合以上內部人掃貨力道與勝率，在所有候選標的中挑選出最具上漲爆發力、機構跟進意願最高、性價比最佳的「唯一一檔商品」。
4. **期權流動性與可交易性 (Optionability)**：
   - 我們的後續步驟為挑選該標的之 CALL 期權大單並下單。因此請優先挑選知名度高、中大型市值、具有熱絡期權鏈（Options Chain）的活躍標的，避免無期權成交的大單死水標的。
5. **嚴格白名單約束**：
   - 推薦的標的代號 (Symbol) 必須 100% 出現於上述【標的內部人淨買入總額排行榜】之中，嚴禁推薦清單以外的股票！

【輸出格式規範】
請務必嚴格依循下列 Markdown 結構輸出，代號必須清晰以便程式正則擷取：

#### 📌 【最佳內部人交易個股建議】
- **推薦標的代號 (Symbol)**: [請填寫大寫代號，例如: DELL 或 RSG 或 UBER]
- **公司名稱**: [完整名稱]
- **內部人交易核心數據**:
  - 增持總金額: $[XX,XXX,XXX]
  - 增持股數 / 成交均價: [股數] 股 @ $[均價]
  - 關鍵內部人姓名與職位: [姓名 (職稱)]
  - 近期內部人買賣態勢: [淨買入 / 集團掃貨說明]
- **進場推薦理由與籌碼深度分析**:
  1. [內部人持股動機與真金白銀信念解析]
  2. [產業景氣與催化劑 Catalyst 展望]
  3. [機構資金追捧潛力與目標上漲空間評估]
- **短中期持有策略與風控提醒**:
  - [建議進場時機與觀察重點]
"""
        return prompt

    def parse_symbol_from_report(self, report_text, excluded_symbols=None, valid_symbols=None):
        """
        自分析報告中提取標的代號，若命中了排除名單或不在候選名單中則判定為無效
        """
        patterns = [
            r'推薦標的代號[^\n:]*[：:]\s*[\*`]*([A-Za-z0-9]+)',
            r'Symbol[^\n:]*[：:]\s*[\*`]*([A-Za-z0-9]+)',
            r'標的代號[^\n:]*[：:]\s*[\*`]*([A-Za-z0-9]+)',
        ]
        excluded_set = {s.upper() for s in (excluded_symbols or [])}
        valid_set = {s.upper() for s in (valid_symbols or [])} if valid_symbols else None

        for p in patterns:
            m = re.search(p, report_text, re.IGNORECASE)
            if m:
                sym = m.group(1).strip().upper()
                if sym not in ("BUY", "CALL", "PUT", "STOCK", "NONE", "N/A", "SYMBOL"):
                    if sym in excluded_set:
                        print(f"[WARN] [SelectionSkill] AI 推薦的標的 {sym} 位於排除名單中！")
                        return None
                    if valid_set and sym not in valid_set:
                        print(f"[WARN] [SelectionSkill] AI 推薦的標的 {sym} 未出現在本次內部人數據白名單中（可能為模型幻覺），判定無效！")
                        return None
                    return sym
        return None

    def select_best_symbol(self, csv_path, memory_skill=None, custom_excluded=None, max_retries=3):
        """
        全流程執行內部人選股：
          1. 取得排除清單
          2. 清洗聚合數據
          3. 呼叫 Gemini AI
          4. 提取標的代號（具備自動降級與重試機制）
        """
        if not self.api_key:
            raise ValueError("[SelectionSkill] 未提供 Gemini API Key，請檢查 trade/.env")

        # 1. 取得排除名單
        excluded_symbols = list(custom_excluded or [])
        if memory_skill:
            excluded_symbols = list(set(excluded_symbols + memory_skill.get_excluded_symbols()))

        if excluded_symbols:
            print(f"[INFO] [SelectionSkill] 目前需排除的標的清單: {sorted(excluded_symbols)}")

        # 2. 清洗與過濾數據
        summary_text, top_summary, df_buys = self.clean_and_aggregate(csv_path, excluded_symbols=excluded_symbols)
        if top_summary.empty:
            raise ValueError("[SelectionSkill] 過濾排除標的後，無任何剩餘的內部人淨買入標的。")

        # 3. 構建 Prompt
        prompt = self.build_prompt(summary_text, excluded_symbols=excluded_symbols)

        # 4. 呼叫 Gemini AI (支援自動重試)
        insider_report = None
        selected_symbol = None

        valid_symbols = top_summary["Symbol"].tolist() if not top_summary.empty else []

        for attempt in range(1, max_retries + 1):
            print(f"[INFO] [SelectionSkill] 正在向 Gemini 請求內部人量化分析報告 (嘗試第 {attempt}/{max_retries} 次)...")
            try:
                report = call_gemini_for_skill(prompt, self.api_key) if call_gemini_for_skill else None
                if report and len(report.strip()) > 100:
                    sym = self.parse_symbol_from_report(report, excluded_symbols=excluded_symbols, valid_symbols=valid_symbols)
                    if sym:
                        insider_report = report
                        selected_symbol = sym
                        print(f"[SUCCESS] [SelectionSkill] 第 {attempt} 次嘗試成功選出標的: {selected_symbol}")
                        break
                    else:
                        print(f"[WARN] [SelectionSkill] 第 {attempt} 次 AI 未產出合法未重複之標的代號，重試中...")
            except Exception as e:
                print(f"[WARN] [SelectionSkill] 第 {attempt} 次請求異常: {e}")
            time.sleep(2)

        # 若 AI 未能成功給出合法代號，啟動安全降級（直接取排行榜榜首）
        if not selected_symbol:
            fallback_sym = str(top_summary.iloc[0]["Symbol"]).strip().upper()
            print(f"[WARN] [SelectionSkill] AI 產出解析失敗，啟動備援機制：自動選取淨買入榜首 {fallback_sym}")
            selected_symbol = fallback_sym

        # 取得標的詳細資訊
        meta = top_summary[top_summary["Symbol"] == selected_symbol]
        company_name = meta["Company_Name"].values[0] if not meta.empty else ""
        net_buy = float(meta["Net_Buy_Total"].values[0]) if not meta.empty else 0.0

        if not insider_report:
            insider_report = f"""#### 📌 【最佳內部人交易個股建議 (量化備援推薦)】
- **推薦標的代號 (Symbol)**: {selected_symbol}
- **公司名稱**: {company_name}
- **內部人交易核心數據**:
  - 增持總金額: ${net_buy:,.2f}
  - 狀態: 排除歷史推薦名單後之內部人淨買盤全市場第一名
- **進場推薦理由與籌碼深度分析**:
  1. 系統量化自動篩選：該標的在近期內部人真金白銀增持排行榜名列前茅，且不在 14 天冷卻期名單中。
  2. 避開已持有或近期重複標的，具備高度主力建倉確定性。
"""

        return {
            "status": "ok",
            "symbol": selected_symbol,
            "company_name": company_name,
            "insider_net_buy": net_buy,
            "report_text": insider_report,
            "excluded_symbols": excluded_symbols,
            "top_candidates": top_summary["Symbol"].tolist()[:5],
        }


if __name__ == "__main__":
    print("=" * 60)
    print(" Selection Skill 獨立單元測試")
    print("=" * 60)

    from trade.skills.memory_skill import SelectionMemory
    import glob

    memory = SelectionMemory()
    excluded = memory.get_excluded_symbols()
    print(f"記憶庫排除標的: {excluded}")

    # 尋找現存最新內部人 CSV
    csv_candidates = glob.glob(os.path.join(BASE_DIR, "Barchart", "*insider*.csv"))
    if not csv_candidates:
        csv_candidates = glob.glob(os.path.join(BASE_DIR, "Barchart", "old", "*insider*.csv"))

    if not csv_candidates:
        print("[ERROR] 找不到任何內部人 CSV 測試檔")
        sys.exit(1)

    csv_candidates.sort(key=os.path.getmtime, reverse=True)
    test_csv = csv_candidates[0]
    print(f"使用測試 CSV: {os.path.basename(test_csv)}")

    skill = InsiderSelectionSkill()
    summary, top_df, _ = skill.clean_and_aggregate(test_csv, excluded_symbols=excluded)
    print("\n過濾重複後之 Top 5 標的:")
    print(top_df[["Symbol", "Company_Name", "Net_Buy_Total", "Buy_Trades"]].head(5).to_string(index=False))
