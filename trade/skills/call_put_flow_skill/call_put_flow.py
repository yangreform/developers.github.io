#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Call/Put Flow Skill: 雙向期權大單流向與最佳 Put/Call 決選技能 (trade/skills/call_put_flow_skill/call_put_flow.py)
================================================================================
職責：
  1. 接收 Barchart AI 之建議 1 (個股) 與建議 2 (ETF)
  2. 同時支援 CALL 與 PUT 大單數據清洗與機構多空力道量化
  3. 統計各標的之 Ask 主動買方金額、Put/Call 權利金比例與量倉比
  4. 構建專業期權橫向對比提示詞，調用 Gemini AI
  5. 解讀 Smart Money 意圖，決選出最佳 1 檔波段 Put 或 Call 進行下單 (Strike, Exp Date, Action, Right)
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


class CallPutFlowSkill:
    """
    雙向期權大單流向分析與最佳 Put/Call 決選技能
    """

    def __init__(self, api_key=None, env_path=None):
        self.env_path = env_path or os.path.join(BASE_DIR, ".env")
        self.api_key = api_key or (load_gemini_api_key(self.env_path) if load_gemini_api_key else None)

    def clean_and_prepare_flow(self, csv_path, symbol, target_type=None, min_dte=7, top_count=35):
        """
        清洗目標標的的 Options Flow 數據。
        支援 target_type="CALL", "PUT" 或 None (兩者皆保留)。
        過濾 0DTE 雜訊，統計多空大單規模與買賣方主動性。
        """
        if not csv_path or not os.path.exists(csv_path):
            return "", pd.DataFrame(), {}

        sym_clean = symbol.strip().upper()
        print(f"[INFO] [CallPutFlowSkill] 讀取 {sym_clean} Options Flow: {os.path.basename(csv_path)}")
        try:
            df = pd.read_csv(csv_path)
        except Exception as e:
            print(f"[WARN] [CallPutFlowSkill] 讀取 CSV 失敗: {e}")
            return "", pd.DataFrame(), {}

        # 移除無效與 footer 行
        df = df[df["Symbol"].notna() & (~df["Symbol"].astype(str).str.contains("Downloaded", case=False, na=False))]
        if df.empty or "Type" not in df.columns:
            print(f"[WARN] [CallPutFlowSkill] {sym_clean} Options Flow 表格為空或無有效交易數據。")
            return "", pd.DataFrame(), {}

        # 數值型態清理
        numeric_cols = ["Price~", "Strike", "DTE", "Trade", "Size", "Premium", "Volume", "Open Int", "Delta"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col].astype(str).str.replace(",", "").str.replace("$", ""), errors="coerce")

        # 統計全標的多空大單整體規模
        call_mask = df["Type"].astype(str).str.upper() == "CALL"
        put_mask = df["Type"].astype(str).str.upper() == "PUT"

        total_call_prem = float(df.loc[call_mask, "Premium"].sum() or 0.0) if "Premium" in df.columns else 0.0
        total_put_prem = float(df.loc[put_mask, "Premium"].sum() or 0.0) if "Premium" in df.columns else 0.0
        total_prem = total_call_prem + total_put_prem

        side_col = df["Side"].astype(str).str.lower() if "Side" in df.columns else pd.Series([""] * len(df))
        ask_mask = side_col.str.contains("ask")
        bid_mask = side_col.str.contains("bid")

        ask_prem = float(df.loc[ask_mask, "Premium"].sum() or 0.0) if "Premium" in df.columns else 0.0
        bid_prem = float(df.loc[bid_mask, "Premium"].sum() or 0.0) if "Premium" in df.columns else 0.0

        stats = {
            "symbol": sym_clean,
            "total_trades": len(df),
            "call_trades": int(call_mask.sum()),
            "put_trades": int(put_mask.sum()),
            "total_call_prem": total_call_prem,
            "total_put_prem": total_put_prem,
            "call_prem_pct": round(total_call_prem / total_prem * 100, 1) if total_prem > 0 else 50.0,
            "put_prem_pct": round(total_put_prem / total_prem * 100, 1) if total_prem > 0 else 50.0,
            "ask_side_prem": ask_prem,
            "bid_side_prem": bid_prem,
            "buyer_aggression_pct": round(ask_prem / (ask_prem + bid_prem) * 100, 1) if (ask_prem + bid_prem) > 0 else 50.0,
        }

        # 依 target_type 篩選
        if target_type:
            t_norm = target_type.strip().upper()
            filtered_df = df[df["Type"].astype(str).str.upper() == t_norm].copy()
        else:
            filtered_df = df.copy()

        if filtered_df.empty:
            print(f"[WARN] [CallPutFlowSkill] 在 {os.path.basename(csv_path)} 中未發現任何 {target_type or '期權'} 數據。")
            return "", pd.DataFrame(), stats

        # 過濾 0DTE (優先保留 DTE >= min_dte)
        df_dte = filtered_df[filtered_df["DTE"] >= min_dte]
        if df_dte.empty:
            df_dte = filtered_df[filtered_df["DTE"] >= 1]
        if df_dte.empty:
            df_dte = filtered_df

        # 依成交權利金 (Premium) 降序排序
        if "Premium" in df_dte.columns:
            df_dte = df_dte.sort_values(by="Premium", ascending=False)

        top_trades = df_dte.head(top_count)
        print(f"[INFO] [CallPutFlowSkill] {sym_clean} ({target_type or 'ALL'}) 清洗完成：代表性大單 {len(top_trades)} 筆 (Call權利金: ${total_call_prem:,.0f} | Put權利金: ${total_put_prem:,.0f})")

        cols_to_show = [c for c in ["Symbol", "Price~", "Exp Date", "Type", "Strike", "DTE", "Trade", "Size", "Side", "Premium", "Volume", "Open Int", "IV", "Delta", "Code", "Time"] if c in top_trades.columns]
        data_str = top_trades[cols_to_show].to_string(index=False)
        return data_str, top_trades, stats

    def build_comparison_prompt(self, cand1, cand2, flow_str1, stats1, flow_str2, stats2):
        """
        組裝雙標的 Whale Options Flow 決選提示詞
        """
        prompt = f"""你是一名華爾街頂級期權造市商（Market Maker）與主力期權大單（Whale Options Flow）量化專家。
在先前初步的 Barchart 異常期權掃描中，我們獲得了以下兩項推薦候選標的：
- **候選 1 (建議 1 - 個股)**: {cand1.get('symbol')} 【{cand1.get('strategy')}】，預估履約價: ${cand1.get('strike')}，到期日: {cand1.get('exp_date')}，參考價: ${cand1.get('ref_price')}
- **候選 2 (建議 2 - ETF)**: {cand2.get('symbol')} 【{cand2.get('strategy')}】，預估履約價: ${cand2.get('strike')}，到期日: {cand2.get('exp_date')}，參考價: ${cand2.get('ref_price')}

不同於一般的單向選股系統，我們的策略**同時支援做多 (BUY CALL) 與做空/避險 (BUY PUT)**！
現在，我們已經分別抓取了這兩個標的在市場上的最新微觀期權大單流向 (Whale Options Flow)。請你深入對比雙方的主力資金意圖，**從兩者之中決選出「最好、勝率最高、機構資金最堅決的一檔 Put 或 Call」**進行送單。

================================================================================
【候選 1：{cand1.get('symbol')} ({cand1.get('strategy')}) 籌碼統計與期權大單】
• 多空權利金分佈: Call 權利金 ${stats1.get('total_call_prem', 0):,.0f} ({stats1.get('call_prem_pct', 0)}%) vs Put 權利金 ${stats1.get('total_put_prem', 0):,.0f} ({stats1.get('put_prem_pct', 0)}%)
• 主力買盤主動性 (Ask%): {stats1.get('buyer_aggression_pct', 0)}% (Ask權利金: ${stats1.get('ask_side_prem', 0):,.0f})
• 代表性大單明細:
{flow_str1 or '(無特定期權大單數據)'}

================================================================================
【候選 2：{cand2.get('symbol')} ({cand2.get('strategy')}) 籌碼統計與期權大單】
• 多空權利金分佈: Call 權利金 ${stats2.get('total_call_prem', 0):,.0f} ({stats2.get('call_prem_pct', 0)}%) vs Put 權利金 ${stats2.get('total_put_prem', 0):,.0f} ({stats2.get('put_prem_pct', 0)}%)
• 主力買盤主動性 (Ask%): {stats2.get('buyer_aggression_pct', 0)}% (Ask權利金: ${stats2.get('ask_side_prem', 0):,.0f})
• 代表性大單明細:
{flow_str2 or '(無特定期權大單數據)'}

================================================================================
【評比與決選核心原則】
1. **機構真實金流規模 (Whale Institutional Size)**：哪一個標的的大單資金體量更大？是否出現不計成本敲進的百萬美元級大單？
2. **主動吃單方向 (Ask Aggression)**：優先選擇在 Ask / Mid 積極追價吃單（顯示機構急迫進場建倉），排除在 Bid 側賣出倒貨的假象。
3. **開倉真實性 (ToOpen vs Vol/OI)**：成交量（Volume）是否顯著大於未平倉量（Open Int）或標註 ToOpen，代表主力新開倉發動而非平倉離場。
4. **行使價與期限最適性 (Moneyness & DTE)**：Delta 位於合理區間（Call: 0.30~0.70 / Put: -0.30~-0.70），DTE 給予 7~90 天足夠波段空間，拒絕 0DTE 樂透噪聲。
5. **多空方向決斷 (Directional Conviction)**：無論是 BUY CALL 還是 BUY PUT，只要其大單信號更純粹、勝率更高、盈虧比更優，即判定為優勝者！

【輸出格式規範】
請務必嚴格依循下列 Markdown 格式輸出，合約參數必須明確以利系統自動解析下單：

#### 🏆 【Options Flow 決選結果：最佳 Put/Call 投資建議】
- **獲勝建議 (Winner)**: [請填寫 建議 1 或 建議 2]
- **推薦標的代號 (Symbol)**: [例如: {cand1.get('symbol')} 或 {cand2.get('symbol')}]
- **建議策略 (Strategy)**: [請填寫 Buy Call 或 Buy Put]
- **期權類型 (Right)**: [請填寫 Call 或 Put]
- **履約價 (Strike)**: $[請填寫數值，例如: 152.50]
- **到期日 (Exp Date)**: [請填寫 YYYY-MM-DD，例如: 2026-09-25]
- **到期天數 (DTE)**: [XX] 天
- **參考價格 / 權利金 (Reference Price)**: $[請填寫數值，例如: 5.40]
- **Delta**: [請填寫數值，例如: 0.4864]
- **決選理由與期權大單流向深度對比分析**:
  1. [獲勝標的主力大單掃貨特徵：機構成交金額、買方主動性 Side、量倉比與急迫性]
  2. [落選標的相對劣勢分析：大單分歧、主力賣單壓制、或量能規模不如獲勝者之原因]
  3. [履約價挑選、行使空間與波段盈虧比深度評估]
"""
        return prompt

    def parse_decision(self, report_text, cand1, cand2):
        """
        自決選報告中精準解析出獲勝合約參數
        """
        # 1. 判斷勝出者
        winner_id = 1
        m_win = re.search(r'獲勝建議[^\n:]*[：:]\s*[\*`]*\s*建議\s*([12一二])', report_text)
        if m_win:
            val = m_win.group(1).strip()
            winner_id = 1 if val in ("1", "一") else 2
        else:
            # 依 symbol 判定
            sym1 = cand1.get("symbol", "").upper()
            sym2 = cand2.get("symbol", "").upper()
            m_sym = re.search(r'推薦標的代號[^\n:]*[：:]\s*[\*`]*\s*([A-Za-z0-9]+)', report_text)
            if m_sym:
                chosen_sym = m_sym.group(1).strip().upper()
                if chosen_sym == sym2:
                    winner_id = 2
                elif chosen_sym == sym1:
                    winner_id = 1

        base_cand = cand1 if winner_id == 1 else cand2
        default_sym = base_cand.get("symbol", "SPY").upper()

        # 2. 標的代號
        sym_match = re.search(r'推薦標的代號[^\n:]*[：:]\s*[\*`]*\s*([A-Za-z0-9]+)', report_text)
        symbol = sym_match.group(1).strip().upper() if sym_match else default_sym
        if symbol in ("BUY", "CALL", "PUT", "NONE", "N/A", "SYMBOL"):
            symbol = default_sym

        # 3. 策略與方向
        strat_match = re.search(r'建議策略[^\n:]*[：:]\s*[\*`]*\s*([A-Za-z\s]+)', report_text)
        raw_strat = strat_match.group(1).strip() if strat_match else base_cand.get("strategy", "Buy Call")

        is_put = "PUT" in raw_strat.upper() or "PUT" in report_text[:300].upper() if "建議策略" not in report_text else "PUT" in raw_strat.upper()
        # 期權類型欄位交叉確認
        right_match = re.search(r'期權類型[^\n:]*[：:]\s*[\*`]*\s*([A-Za-z]+)', report_text)
        if right_match:
            r_val = right_match.group(1).strip().upper()
            if "PUT" in r_val:
                is_put = True
            elif "CALL" in r_val:
                is_put = False

        right = "P" if is_put else "C"
        strategy = f"Buy {'Put' if is_put else 'Call'}"

        # 4. 履約價
        strike_match = re.search(r'(?:履約價|Strike)[^\n:]*[：:]\s*[\*\$]*\s*(\d+\.?\d*)', report_text, re.IGNORECASE)
        strike = float(strike_match.group(1)) if strike_match else base_cand.get("strike")

        # 5. 到期日
        exp_match = re.search(r'到期日[^\d]*(\d{4}[-/]\d{2}[-/]\d{2})', report_text)
        exp_date = exp_match.group(1).replace("-", "").replace("/", "") if exp_match else base_cand.get("exp_date")

        # 6. 參考價格
        ref_match = re.search(r'(?:參考價格|權利金|Reference Price)[^\$\d\n]*\$?(\d+\.?\d*)', report_text, re.IGNORECASE)
        ref_price = float(ref_match.group(1)) if ref_match else (base_cand.get("ref_price") or 1.0)

        # 7. Delta
        delta_match = re.search(r'Delta[^\d\n\-+]*([+-]?\d+\.?\d*)', report_text, re.IGNORECASE)
        delta = float(delta_match.group(1)) if delta_match else None

        winner_contract = {
            "winner_id": winner_id,
            "symbol": symbol,
            "strategy": strategy,
            "action": "BUY",
            "right": right,
            "strike": strike,
            "exp_date": exp_date,
            "ref_price": ref_price,
            "delta": delta,
            "type": "single_option",
            "original_suggestion": base_cand,
        }
        return winner_contract

    def evaluate_best_put_call(self, candidate1, candidate2, csv_path1, csv_path2, max_retries=3):
        """
        全流程執行：清洗雙標的 Options Flow，調用 Gemini 橫向對比，決選出最佳 1 檔 Put/Call。
        """
        sym1 = candidate1.get("symbol", "").upper()
        sym2 = candidate2.get("symbol", "").upper()
        strat1 = candidate1.get("strategy", "Buy Call")
        strat2 = candidate2.get("strategy", "Buy Put")

        type1 = "PUT" if "PUT" in strat1.upper() else "CALL"
        type2 = "PUT" if "PUT" in strat2.upper() else "CALL"

        print(f"\n" + "=" * 65)
        print(f"🐋 [CallPutFlowSkill] 啟動雙向期權大單流向評決")
        print(f"  • 候選 1 (建議 1): {sym1} [{strat1}] (期權大單檔: {os.path.basename(csv_path1) if csv_path1 else '無'})")
        print(f"  • 候選 2 (建議 2): {sym2} [{strat2}] (期權大單檔: {os.path.basename(csv_path2) if csv_path2 else '無'})")
        print("=" * 65)

        # 1. 雙向清洗
        flow_str1, top_df1, stats1 = self.clean_and_prepare_flow(csv_path1, sym1, target_type=type1)
        flow_str2, top_df2, stats2 = self.clean_and_prepare_flow(csv_path2, sym2, target_type=type2)

        # 情況 A: 雙方皆無大單數據
        if top_df1.empty and top_df2.empty:
            print("[WARN] [CallPutFlowSkill] 兩個標的皆無有效 Options Flow 數據，自動預設候選 1 勝出。")
            winner_contract = {
                "winner_id": 1,
                "symbol": sym1,
                "strategy": strat1,
                "action": "BUY",
                "right": "P" if "PUT" in strat1.upper() else "C",
                "strike": candidate1.get("strike"),
                "exp_date": candidate1.get("exp_date"),
                "ref_price": candidate1.get("ref_price", 1.0),
                "type": "single_option",
                "original_suggestion": candidate1,
            }
            fallback_report = f"⚠️ 雙標的皆無足夠期權大單數據，依原始評分維持建議 1 ({sym1} {strat1}) 勝出。"
            return {
                "status": "fallback_no_data",
                "winner": winner_contract,
                "winner_id": 1,
                "report_text": fallback_report,
                "stats1": stats1,
                "stats2": stats2,
            }

        # 情況 B: 僅一方有大單數據
        if top_df1.empty and not top_df2.empty:
            print(f"[INFO] [CallPutFlowSkill] 候選 1 ({sym1}) 無大單，候選 2 ({sym2}) 具備主力大單，由候選 2 直接勝出！")
            winner_contract = {
                "winner_id": 2,
                "symbol": sym2,
                "strategy": strat2,
                "action": "BUY",
                "right": "P" if "PUT" in strat2.upper() else "C",
                "strike": candidate2.get("strike"),
                "exp_date": candidate2.get("exp_date"),
                "ref_price": candidate2.get("ref_price", 1.0),
                "type": "single_option",
                "original_suggestion": candidate2,
            }
            rep = f"🏆 經期權大單流向過濾：標的 {sym2} 具備顯著主力 {type2} 大單支持，而 {sym1} 無有效大單，裁定建議 2 ({sym2} {strat2}) 勝出！"
            return {
                "status": "ok",
                "winner": winner_contract,
                "winner_id": 2,
                "report_text": rep,
                "stats1": stats1,
                "stats2": stats2,
            }
        elif not top_df1.empty and top_df2.empty:
            print(f"[INFO] [CallPutFlowSkill] 候選 2 ({sym2}) 無大單，候選 1 ({sym1}) 具備主力大單，由候選 1 直接勝出！")
            winner_contract = {
                "winner_id": 1,
                "symbol": sym1,
                "strategy": strat1,
                "action": "BUY",
                "right": "P" if "PUT" in strat1.upper() else "C",
                "strike": candidate1.get("strike"),
                "exp_date": candidate1.get("exp_date"),
                "ref_price": candidate1.get("ref_price", 1.0),
                "type": "single_option",
                "original_suggestion": candidate1,
            }
            rep = f"🏆 經期權大單流向過濾：標的 {sym1} 具備顯著主力 {type1} 大單支持，而 {sym2} 無有效大單，裁定建議 1 ({sym1} {strat1}) 勝出！"
            return {
                "status": "ok",
                "winner": winner_contract,
                "winner_id": 1,
                "report_text": rep,
                "stats1": stats1,
                "stats2": stats2,
            }

        # 情況 C: 雙方皆具備大單，調用 Gemini 橫向深度量化對比
        prompt = self.build_comparison_prompt(candidate1, candidate2, flow_str1, stats1, flow_str2, stats2)
        decision_report = None

        if self.api_key and call_gemini_for_skill:
            for attempt in range(1, max_retries + 1):
                print(f"[INFO] [CallPutFlowSkill] 正在向 Gemini 請求多空大單橫向評決 (嘗試第 {attempt}/{max_retries} 次)...")
                try:
                    res = call_gemini_for_skill(prompt, self.api_key)
                    if res and len(res.strip()) > 150:
                        parsed = self.parse_decision(res, candidate1, candidate2)
                        if parsed.get("strike") and parsed.get("exp_date"):
                            decision_report = res
                            print(f"[SUCCESS] [CallPutFlowSkill] ✅ 第 {attempt} 次成功完成決選！勝出者: 建議 {parsed['winner_id']} ({parsed['symbol']} {parsed['strategy']} @ Strike ${parsed['strike']})")
                            break
                except Exception as e:
                    print(f"[WARN] [CallPutFlowSkill] 第 {attempt} 次請求異常: {e}")
                time.sleep(2)

        # 備援機制：若 AI 呼叫未果，依據 Ask-side 主力主動成交金額量化裁定
        if not decision_report:
            print("[WARN] [CallPutFlowSkill] AI 決選未果，啟動規則型主力買盤金額量化裁定...")
            ask_prem1 = stats1.get("ask_side_prem", 0.0)
            ask_prem2 = stats2.get("ask_side_prem", 0.0)

            if ask_prem2 > ask_prem1:
                chosen_id = 2
                chosen_cand = candidate2
                reason = f"候選 2 ({sym2} {strat2}) 的主動性 Ask 買盤權利金 (${ask_prem2:,.0f}) 超越候選 1 (${ask_prem1:,.0f})。"
            else:
                chosen_id = 1
                chosen_cand = candidate1
                reason = f"候選 1 ({sym1} {strat1}) 的主動性 Ask 買盤權利金 (${ask_prem1:,.0f}) 領先候選 2 (${ask_prem2:,.0f})。"

            winner_contract = {
                "winner_id": chosen_id,
                "symbol": chosen_cand.get("symbol"),
                "strategy": chosen_cand.get("strategy"),
                "action": "BUY",
                "right": "P" if "PUT" in chosen_cand.get("strategy", "").upper() else "C",
                "strike": chosen_cand.get("strike"),
                "exp_date": chosen_cand.get("exp_date"),
                "ref_price": chosen_cand.get("ref_price", 1.0),
                "type": "single_option",
                "original_suggestion": chosen_cand,
            }
            decision_report = f"#### 🏆 【Options Flow 決選結果（量化備援模式）】\n- **獲勝建議**: 建議 {chosen_id}\n- **推薦標的代號**: {winner_contract['symbol']}\n- **建議策略**: {winner_contract['strategy']}\n- **決選理由**: {reason}"
        else:
            winner_contract = self.parse_decision(decision_report, candidate1, candidate2)

        return {
            "status": "ok",
            "winner": winner_contract,
            "winner_id": winner_contract["winner_id"],
            "report_text": decision_report,
            "stats1": stats1,
            "stats2": stats2,
        }


if __name__ == "__main__":
    print("=" * 60)
    print(" CallPutFlowSkill 獨立單元測試")
    print("=" * 60)

    import glob

    skill = CallPutFlowSkill()
    flow_files = glob.glob(os.path.join(BASE_DIR, "Barchart", "*options-flow*.csv")) + glob.glob(os.path.join(BASE_DIR, "Barchart", "old", "*options-flow*.csv"))
    if not flow_files:
        print("[ERROR] 找不到任何 Options Flow CSV 檔案進行測試。")
        sys.exit(1)

    # 模擬 2 個候選建議
    f1 = flow_files[0]
    f2 = flow_files[1] if len(flow_files) > 1 else flow_files[0]

    c1 = {"id": 1, "symbol": "MSTR", "strategy": "Buy Call", "strike": 152.5, "exp_date": "20260925", "ref_price": 5.4}
    c2 = {"id": 2, "symbol": "IWM", "strategy": "Buy Put", "strike": 279.0, "exp_date": "20261009", "ref_price": 2.75}

    print(f"測試檔 1: {os.path.basename(f1)}")
    print(f"測試檔 2: {os.path.basename(f2)}")

    res = skill.evaluate_best_put_call(c1, c2, f1, f2, max_retries=1)
    print("\n決選成果:")
    print("Winner:", res["winner"])
    print("\nReport Snippet:\n", res["report_text"][:400])
