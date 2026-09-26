#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Long Call Selection Skill: Barchart Long Call 選擇權選股與 AI 量化挑選技能
================================================================================
職責：
  1. 清洗 Barchart Long Call Options Screener 官方表格數據
  2. 計算真金白銀權利金成交規模 (Est_Premium = Ask * Volume * 100) 與持倉資金
  3. 【雙重防重複機制】：
     - 資料層物理剔除：從 DataFrame 中直接排除歷史 14 天冷卻中或黑名單標的
     - 提示詞風控約束：在 Prompt 中明確列出禁止推薦的歷史排除名單
  4. 構建專業期權量化提示詞，呼叫 Gemini AI 進行深度量化決策
  5. 精選出唯一最佳合約（Symbol, Strike, Exp Date, Ask, Delta, DTE 等），附帶結構化備援候選列表
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
    from ..gemini_helper import call_gemini_for_skill, load_gemini_api_key
except Exception:
    try:
        from trade.skills.gemini_helper import call_gemini_for_skill, load_gemini_api_key
    except Exception:
        try:
            from skills.gemini_helper import call_gemini_for_skill, load_gemini_api_key
        except Exception:
            call_gemini_for_skill = None
            load_gemini_api_key = None


def normalize_exp_date(date_val):
    """
    將到期日統一轉換為 IBKR 格式之 YYYYMMDD 字串 (例如 20261016)
    """
    if pd.isna(date_val):
        return ""
    s = str(date_val).strip()
    # 移除時間部分
    if " " in s:
        s = s.split(" ")[0]
    s = s.replace("/", "-")
    parts = s.split("-")
    if len(parts) == 3:
        if len(parts[0]) == 4:  # YYYY-MM-DD
            return f"{parts[0]}{parts[1].zfill(2)}{parts[2].zfill(2)}"
        elif len(parts[2]) == 4:  # MM-DD-YYYY
            return f"{parts[2]}{parts[0].zfill(2)}{parts[1].zfill(2)}"
        elif len(parts[2]) == 2:  # MM-DD-YY
            return f"20{parts[2]}{parts[0].zfill(2)}{parts[1].zfill(2)}"
    cleaned = re.sub(r"\D", "", s)
    if len(cleaned) == 8:
        return cleaned
    return s


def format_exp_date_display(exp_date_yyyymmdd):
    """將 YYYYMMDD 格式化為易讀之 YYYY-MM-DD"""
    s = str(exp_date_yyyymmdd).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


class LongCallSelectionSkill:
    """
    Long Call 選擇權選股與量化分析技能
    """

    def __init__(self, api_key=None, env_path=None):
        self.env_path = env_path or os.path.join(BASE_DIR, ".env")
        self.api_key = api_key or (load_gemini_api_key(self.env_path) if load_gemini_api_key else None)

    def clean_and_aggregate(self, csv_path, excluded_symbols=None, top_count=30):
        """
        清洗 Long Call Options Screener 數據，專注於 CALL 買權大單與高勝率合約，主動過濾排除名單。
        """
        if not os.path.exists(csv_path):
            raise FileNotFoundError(f"找不到 Long Call Screener CSV: {csv_path}")

        print(f"[INFO] [LongCallSkill] 讀取 Long Call Screener 數據: {os.path.basename(csv_path)}")
        df = pd.read_csv(csv_path)

        # 1. 移除無效與 footer 行 (例如 Downloaded from Barchart.com as of...)
        df = df[df["Symbol"].notna() & (~df["Symbol"].astype(str).str.contains("Downloaded", case=False, na=False))]
        if df.empty or "Strike" not in df.columns:
            print(f"[WARN] [LongCallSkill] {os.path.basename(csv_path)} 為空或無有效期權欄位。")
            return pd.DataFrame(), []

        # 2. 符號清理與大寫化
        df["Symbol"] = df["Symbol"].astype(str).str.strip().str.upper()

        # 3. 數值型態清理
        def clean_num(col):
            if col in df.columns:
                return pd.to_numeric(
                    df[col].astype(str).str.replace(",", "").str.replace("$", "").str.replace("%", "").str.replace("+", ""),
                    errors="coerce",
                )
            return pd.Series(0.0, index=df.index)

        df["Price"] = clean_num("Price~")
        df["Strike"] = clean_num("Strike")
        df["DTE"] = clean_num("DTE").fillna(0).astype(int)
        df["Ask"] = clean_num("Ask")
        df["Volume"] = clean_num("Volume").fillna(0).astype(int)
        df["Open_Int"] = clean_num("Open Int").fillna(0).astype(int)
        df["IV_Rank"] = clean_num("IV Rank")
        df["IV"] = clean_num("IV")
        df["Delta"] = clean_num("Delta")
        df["Profit_Prob"] = clean_num("Profit Prob")
        df["Moneyness"] = clean_num("Moneyness")
        df["BE_Ask"] = clean_num("BE (Ask)")
        df["BE_Pct"] = clean_num("%BE (Ask)")

        # 4. 衍生真金白銀指標
        # 每口權利金成本 (USD) = Ask * 100
        df["Cost_Per_Contract"] = df["Ask"] * 100.0
        # 成交金額規模 (USD) = Ask * Volume * 100
        df["Est_Premium"] = df["Ask"] * df["Volume"] * 100.0
        # 未平倉沉澱資金 (USD) = Ask * Open_Int * 100
        df["Est_OI_Capital"] = df["Ask"] * df["Open_Int"] * 100.0

        # 標準化到期日 (IBKR 格式 YYYYMMDD)
        if "Exp Date" in df.columns:
            df["Exp_Date_Norm"] = df["Exp Date"].apply(normalize_exp_date)
            df["Exp_Date_Disp"] = df["Exp_Date_Norm"].apply(format_exp_date_display)
        else:
            df["Exp_Date_Norm"] = ""
            df["Exp_Date_Disp"] = ""

        # 5. 【非期權標的物理過濾】：剔除特別股、權證、單位、無期權美股代號
        KNOWN_NON_OPTIONABLE = {"NYAX", "KBDC", "BRPSF", "MXF"}

        def _is_non_opt(sym_str):
            s = str(sym_str).strip().upper()
            if s in KNOWN_NON_OPTIONABLE:
                return True
            if re.search(r"[\.\-\+\/\^]", s):
                return True
            if len(s) >= 5 and (s.endswith(("P", "W", "R", "U")) or "PR" in s):
                return True
            return False

        non_opt_mask = df["Symbol"].apply(_is_non_opt)
        if non_opt_mask.any():
            filtered_non_opt = df[non_opt_mask]["Symbol"].unique().tolist()
            df = df[~non_opt_mask]
            print(f"[INFO] [LongCallSkill] 已剔除非期權標的 (特別股/權證/非標準代碼共 {len(filtered_non_opt)} 檔): {filtered_non_opt[:6]}")

        # 6. 【資料層物理過濾】：嚴格排除冷卻中與黑名單標的
        excluded_set = {s.upper() for s in (excluded_symbols or [])}
        if excluded_set:
            original_len = len(df)
            df = df[~df["Symbol"].isin(excluded_set)]
            filtered_count = original_len - len(df)
            print(f"[INFO] [LongCallSkill] 資料層已物理剔除 {filtered_count} 筆排除標的數據 (排除名單: {sorted(list(excluded_set))})")

        if df.empty:
            print("[WARN] [LongCallSkill] 過濾排除名單後，已無可用 Long Call 合約數據。")
            return pd.DataFrame(), []

        # 7. 排序與精選候選名單：
        # 依綜合權利金注入規模 (Est_Premium) 與成交量 (Volume) 由大到小排序
        df["Score"] = df["Est_Premium"] + (df["Open_Int"] * df["Ask"] * 20.0)
        df_sorted = df.sort_values(by=["Score", "Volume", "Est_Premium"], ascending=[False, False, False]).copy()

        # 取前 top_count 筆代表性合約
        top_df = df_sorted.head(top_count).copy()
        unique_syms = top_df["Symbol"].unique().tolist()

        print(f"[INFO] [LongCallSkill] 數據清洗完成：候選合約 {len(df_sorted)} 筆，精選前 {len(top_df)} 筆代表性合約 (涵蓋 {len(unique_syms)} 檔獨立股票)")
        return top_df, unique_syms

    def build_prompt(self, candidates_df, excluded_symbols=None):
        """
        構建專為 Gemini AI 設計之 Long Call 深度量化提示詞
        """
        cols_to_display = [
            "Symbol",
            "Price",
            "Exp_Date_Disp",
            "DTE",
            "Strike",
            "Ask",
            "Delta",
            "Volume",
            "Open_Int",
            "IV_Rank",
            "IV",
            "Profit_Prob",
            "Est_Premium",
        ]
        display_df = candidates_df[[c for c in cols_to_display if c in candidates_df.columns]].copy()
        display_df = display_df.rename(
            columns={
                "Price": "Underlying_Price",
                "Exp_Date_Disp": "Exp_Date",
                "Open_Int": "OI",
                "IV_Rank": "IV_Rank_%",
                "IV": "IV_%",
                "Profit_Prob": "Profit_Prob_%",
                "Est_Premium": "Est_Premium_USD",
            }
        )

        candidates_table_str = display_df.to_string(index=False)
        excluded_str = ", ".join(sorted(list(set(excluded_symbols)))) if excluded_symbols else "無"

        prompt = f"""你是一位華爾街頂級量化選擇權交易主管兼避險基金經理人。
請根據以下來自 Barchart Long Call Options Screener 官方即時篩選出的精選買權（Long Call）合約清單，進行深度的多因子量化與籌碼分析，並精選出「唯一最佳且具備最高非對稱獲利潛力」的 1 檔 Long Call 合約。

======================================================================
【待評估之 Long Call 候選合約清單】
======================================================================
{candidates_table_str}

======================================================================
【風控核心約束（強制執行）】
======================================================================
1. 嚴格排除重複標的（歷史推薦冷卻中）：
   - ⚠️ 禁止挑選以下清單中的任何標的代號：[{excluded_str}]
   - 請務必從候選清單中尋找完全不在上述排除名單內的優質股票！

2. 核心量化選約標準：
   - **大單動能與真金白銀流入 (Smart Money Volume & Premium)**：
     優先選擇單日成交量 (Volume) 與成交總權利金 (Est_Premium) 顯著放量、主力真金白銀重金買入的合約。
   - **流動性充足與防滑價 (Liquidity & Open Interest)**：
     未平倉量 (OI) 充足，防範未來平倉時因流動性不足產生過度買賣價差（Bid-Ask Spread）滑價損失。
   - **Delta 甜蜜點與高勝率 (Delta & Stock Replacement)**：
     強烈傾向選擇 Delta 介於 0.70 至 0.95 之間的深度價內 (ITM) 或平價偏價內合約。高 Delta 具備高達 70%~95% 的現貨替代效應，且時間價值衰退 (Theta Decay) 相對較低，獲利機率 (Profit Prob) 明顯更高。
   - **到期時間窗口 (Expiration Horizon & DTE)**：
     優先挑選到期天數 DTE 介於 14 至 45 天（次月或季度波段合約），避免 0~7 DTE 即期合約的極端 Gamma 劇烈震盪與快速歸零風險。
   - **隱含波動率與盈虧比 (IV & Risk-Reward)**：
     IV Rank 處於合理偏低或適中區間，無過度溢價，提供非對稱獲利空間。

======================================================================
【輸出格式規範】
======================================================================
請務必嚴格依循下列 Markdown 格式輸出，不得擅自修改欄位名稱：

#### 📌 【最佳 Long Call 期權合約建議】
- **推薦標的代號 (Symbol)**: [標的代號，如 AMZN]
- **標的現價 (Underlying Price)**: $[現價，如 261.06]
- **建議策略**: Long Call (Buy Call)
- **履約價 (Strike)**: $[履約價，如 240.0]
- **到期日 (Exp Date)**: [YYYY-MM-DD，如 2026-09-18]
- **到期天數 (DTE)**: [數字] 天
- **參考權利金 (Ask Price)**: $[Ask價格，如 23.60]
- **Delta**: [數值，如 0.86]
- **未平倉量 (OI)**: [數值]
- **成交量 (Volume)**: [數值]
- **隱含波動率 (IV / IV Rank)**: [數值，如 32.36% (IV Rank: 23.0%)]
- **獲利機率 (Profit Prob)**: [數值，如 46.72%]
- **進場推薦理由與深度量化分析**:
  1. **主力資金流向與建倉規模**: [深度分析主力買入權利金規模與市場熱度]
  2. **行使價選位與 Delta 甜蜜點**: [分析 Delta、價內程度 Moneyness 與現貨替代優勢]
  3. **盈虧比與期限波段優勢**: [分析 DTE 緩衝期、時間價值衰退風險控制與預估上漲爆發力]
"""
        return prompt

    def parse_contract_from_report(self, report_text, candidates_df, excluded_symbols=None):
        """
        從 Gemini 產出的報告文本中解析推薦合約結構化資訊
        """
        if not report_text:
            return None

        # 1. 解析 Symbol (支援 Markdown **粗體**、代號或 Symbol 等多種格式)
        sym_match = re.search(r"(?:推薦標的代號|標的代號|Symbol).*?[:：]\s*[*_`]*([A-Za-z0-9\.\-]+)", report_text, re.IGNORECASE)
        if not sym_match:
            sym_match = re.search(r"\*\*Symbol\*\*\s*[:：]\s*[*_`]*([A-Za-z0-9\.\-]+)", report_text, re.IGNORECASE)
        if not sym_match:
            sym_match = re.search(r"Symbol\s*[:：]\s*[*_`]*([A-Za-z0-9\.\-]+)", report_text, re.IGNORECASE)

        if not sym_match:
            return None

        symbol = sym_match.group(1).strip().upper().replace("*", "").replace("`", "")

        # 檢查是否違反排除清單
        excluded_set = {s.upper() for s in (excluded_symbols or [])}
        if symbol in excluded_set:
            print(f"[WARN] [LongCallSkill] AI 推薦之標的 {symbol} 處於排除名單中，判定為違規解析。")
            return None

        # 2. 解析 Strike
        strike = None
        strike_match = re.search(r"(?:履約價|Strike).*?[:：]\s*[*_`]*\$?([0-9\.]+)", report_text, re.IGNORECASE)
        if strike_match:
            try:
                strike = float(strike_match.group(1))
            except ValueError:
                pass

        # 3. 解析 Exp Date
        exp_date_norm = None
        exp_match = re.search(r"(?:到期日|Exp(?:iration)?\s*(?:Date)?).*?[:：]\s*[*_`]*([0-9\-\/]{8,10})", report_text, re.IGNORECASE)
        if exp_match:
            exp_date_norm = normalize_exp_date(exp_match.group(1))

        # 4. 解析 Ask / Ref Price
        ref_price = None
        ref_match = re.search(r"(?:參考權利金|權利金|Ask\s*(?:Price)?).*?[:：]\s*[*_`]*\$?([0-9\.]+)", report_text, re.IGNORECASE)
        if ref_match:
            try:
                ref_price = float(ref_match.group(1))
            except ValueError:
                pass

        # 5. 解析 Delta
        delta = None
        delta_match = re.search(r"Delta.*?[:：]\s*[*_`]*([0-9\.\-]+)", report_text, re.IGNORECASE)
        if delta_match:
            try:
                delta = float(delta_match.group(1))
            except ValueError:
                pass

        # 6. 解析 DTE
        dte = None
        dte_match = re.search(r"(?:到期天數|DTE).*?[:：]\s*[*_`]*([0-9]+)", report_text, re.IGNORECASE)
        if dte_match:
            try:
                dte = int(dte_match.group(1))
            except ValueError:
                pass

        # 嘗試從 candidates_df 補完缺失或對齊精確數值
        sym_rows = candidates_df[candidates_df["Symbol"] == symbol]
        if not sym_rows.empty:
            matched_row = sym_rows.iloc[0]
            if strike is not None:
                exact_strike = sym_rows[sym_rows["Strike"] == strike]
                if not exact_strike.empty:
                    matched_row = exact_strike.iloc[0]

            strike = strike if strike is not None else float(matched_row["Strike"])
            exp_date_norm = exp_date_norm if exp_date_norm else matched_row["Exp_Date_Norm"]
            ref_price = ref_price if ref_price is not None else float(matched_row["Ask"])
            delta = delta if delta is not None else float(matched_row["Delta"])
            dte = dte if dte is not None else int(matched_row["DTE"])
            underlying_price = float(matched_row["Price"])
            volume = int(matched_row["Volume"])
            open_int = int(matched_row["Open_Int"])
        else:
            underlying_price = 0.0
            volume = 0
            open_int = 0

        if not symbol or strike is None or not exp_date_norm:
            return None

        contract_info = {
            "symbol": symbol,
            "strike": strike,
            "exp_date": exp_date_norm,
            "exp_date_disp": format_exp_date_display(exp_date_norm),
            "ref_price": ref_price or 1.0,
            "ask": ref_price or 1.0,
            "delta": delta or 0.75,
            "dte": dte or 30,
            "underlying_price": underlying_price,
            "volume": volume,
            "open_int": open_int,
            "right": "C",
            "action": "BUY",
            "strategy": "Long Call",
        }
        return contract_info

    def select_best_contract(self, csv_path, memory_skill=None, symbol_override=None, max_retries=3):
        """
        核心公開方法：清洗數據、結合 Memory 排除名單、呼叫 Gemini 分析並回傳最佳合約與備援名單
        """
        excluded_symbols = []
        if memory_skill and hasattr(memory_skill, "get_excluded_symbols"):
            excluded_symbols = memory_skill.get_excluded_symbols()

        top_df, unique_syms = self.clean_and_aggregate(
            csv_path=csv_path, excluded_symbols=excluded_symbols, top_count=30
        )

        if top_df.empty:
            return {
                "status": "empty",
                "message": "無符合條件且非冷卻中之 Long Call 合約數據",
                "contract": None,
                "report_text": "",
                "top_candidates": [],
            }

        # 若使用者指定了手動標的
        if symbol_override:
            sym_clean = symbol_override.strip().upper()
            sym_df = top_df[top_df["Symbol"] == sym_clean]
            if not sym_df.empty:
                top_df = sym_df.copy()
            else:
                print(f"[WARN] [LongCallSkill] 手動指定標的 {sym_clean} 未在篩選清單中，將由整體清單繼續分析。")

        # 構建 Prompt
        prompt = self.build_prompt(candidates_df=top_df, excluded_symbols=excluded_symbols)

        contract_info = None
        report_text = ""

        # 嘗試呼叫 Gemini
        for attempt in range(1, max_retries + 1):
            print(f"[INFO] [LongCallSkill] 正在向 Gemini 請求 Long Call 深度量化分析 (嘗試第 {attempt}/{max_retries} 次)...")
            try:
                report = call_gemini_for_skill(prompt=prompt, api_key=self.api_key)
                if report:
                    parsed = self.parse_contract_from_report(
                        report_text=report, candidates_df=top_df, excluded_symbols=excluded_symbols
                    )
                    if parsed:
                        contract_info = parsed
                        report_text = report
                        print(f"[SUCCESS] [LongCallSkill] 第 {attempt} 次嘗試成功選出合約: {contract_info['symbol']} Strike=${contract_info['strike']} Exp={contract_info['exp_date']}")
                        break
                    else:
                        print(f"[WARN] [LongCallSkill] 第 {attempt} 次 AI 未產出合法格式或包含排除標的，重新請求...")
            except Exception as e:
                print(f"[WARN] [LongCallSkill] 第 {attempt} 次呼叫異常: {e}")
            time.sleep(2)

        # 備援安全降級機制：若 AI 失敗，直接取綜合評分最高的第一筆合約
        if not contract_info:
            top_row = top_df.iloc[0]
            fallback_sym = str(top_row["Symbol"]).strip().upper()
            fallback_strike = float(top_row["Strike"])
            fallback_exp = str(top_row["Exp_Date_Norm"])
            fallback_ask = float(top_row["Ask"])
            fallback_delta = float(top_row["Delta"])
            fallback_dte = int(top_row["DTE"])
            fallback_price = float(top_row["Price"])

            print(f"[WARN] [LongCallSkill] AI 解析失敗，啟動備援機制：自動選取排行榜首位 {fallback_sym} Strike=${fallback_strike} Exp={fallback_exp}")
            contract_info = {
                "symbol": fallback_sym,
                "strike": fallback_strike,
                "exp_date": fallback_exp,
                "exp_date_disp": format_exp_date_display(fallback_exp),
                "ref_price": fallback_ask,
                "ask": fallback_ask,
                "delta": fallback_delta,
                "dte": fallback_dte,
                "underlying_price": fallback_price,
                "volume": int(top_row["Volume"]),
                "open_int": int(top_row["Open_Int"]),
                "right": "C",
                "action": "BUY",
                "strategy": "Long Call",
            }
            report_text = f"""#### 📌 【最佳 Long Call 期權合約建議 (量化備援推薦)】
- **推薦標的代號 (Symbol)**: {fallback_sym}
- **標的現價 (Underlying Price)**: ${fallback_price:.2f}
- **建議策略**: Long Call (Buy Call)
- **履約價 (Strike)**: ${fallback_strike:.2f}
- **到期日 (Exp Date)**: {format_exp_date_display(fallback_exp)}
- **到期天數 (DTE)**: {fallback_dte} 天
- **參考權利金 (Ask Price)**: ${fallback_ask:.2f}
- **Delta**: {fallback_delta:.4f}
- **未平倉量 (OI)**: {int(top_row['Open_Int']):,}
- **成交量 (Volume)**: {int(top_row['Volume']):,}
- **進場推薦理由與量化深度分析**:
  1. 系統量化自動篩選：該合約在 Long Call 選股排行榜名列首位，且不在 14 天冷卻期名單中。
  2. 具備充沛成交權利金與未平倉量，Delta={fallback_delta:.2f} 享有深價內高現貨替代率與極佳勝率。
"""

        # 整理備援候選名單 (供 IBKR 合約預檢失敗時依序自動容錯切換)
        candidates_list = []
        for _, row in top_df.iterrows():
            c_sym = str(row["Symbol"]).strip().upper()
            candidates_list.append(
                {
                    "symbol": c_sym,
                    "strike": float(row["Strike"]),
                    "exp_date": str(row["Exp_Date_Norm"]),
                    "exp_date_disp": format_exp_date_display(str(row["Exp_Date_Norm"])),
                    "ref_price": float(row["Ask"]),
                    "ask": float(row["Ask"]),
                    "delta": float(row["Delta"]),
                    "dte": int(row["DTE"]),
                    "underlying_price": float(row["Price"]),
                    "volume": int(row["Volume"]),
                    "open_int": int(row["Open_Int"]),
                    "right": "C",
                    "action": "BUY",
                    "strategy": "Long Call",
                }
            )

        return {
            "status": "ok",
            "contract": contract_info,
            "report_text": report_text,
            "top_candidates": candidates_list,
            "excluded_symbols": excluded_symbols,
        }


if __name__ == "__main__":
    print("=" * 60)
    print(" Long Call Selection Skill 單元測試")
    print("=" * 60)

    import glob
    test_csvs = glob.glob(os.path.join(BASE_DIR, "Barchart", "*long-call*.csv"))
    if not test_csvs:
        test_csvs = glob.glob(os.path.join(BASE_DIR, "Barchart", "old", "*long-call*.csv"))

    if not test_csvs:
        print("[ERROR] 找不到任何 Long Call Screener CSV 檔案供測試")
        sys.exit(1)

    test_csvs.sort(key=os.path.getmtime, reverse=True)
    test_csv = test_csvs[0]
    print(f"使用測試檔案: {os.path.basename(test_csv)}")

    skill = LongCallSelectionSkill()
    res = skill.select_best_contract(csv_path=test_csv, max_retries=1)
    print(f"\n測試結果: status={res['status']}")
    if res.get("contract"):
        c = res["contract"]
        print(f"選定合約: {c['symbol']} Strike=${c['strike']} Exp={c['exp_date']} Ask=${c['ask']} Delta={c['delta']}")
