#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UOA Analysis Skill: 異常期權大單深度量化分析技能 (trade/skills/uoa_skill/uoa.py)
================================================================================
職責：
  1. 清洗與篩選二份 Barchart 數據源：個股 UOA、ETF UOA
  2. 自動過濾 0DTE 極短線雜訊 (7 <= DTE <= 120)，按 Vol/OI 與勝率排序
  3. 整合共享之 Gemini Helper，各選出 BUY CALL 與 BUY PUT，得到共 4 檔預選 CALL 或 PUT：
     - 預選一：個股突破交易 (BUY CALL)
     - 預選二：個股做空/避險 (BUY PUT)
     - 預選三：ETF 順勢佈局 (BUY CALL)
     - 預選四：ETF 宏觀對沖 (BUY PUT)
  4. 產出結構化文字檔 (latest_ai_analysis.txt) 供看板與下單模組使用，並推播 LINE 速覽
================================================================================
"""

import os
import sys
import glob
import time
import re
import datetime
import pandas as pd

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
for p in [BASE_DIR, PROJECT_ROOT]:
    if p not in sys.path:
        sys.path.insert(0, p)

BARCHART_DIR = os.path.join(BASE_DIR, "Barchart")
OLD_DIR = os.path.join(BARCHART_DIR, "old")
REPORTS_DIR = os.path.join(BARCHART_DIR, "reports")

os.makedirs(BARCHART_DIR, exist_ok=True)
os.makedirs(OLD_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)

try:
    from trade.skills.gemini_helper import call_gemini_for_skill, load_gemini_api_key
except ImportError:
    try:
        from skills.gemini_helper import call_gemini_for_skill, load_gemini_api_key
    except ImportError:
        call_gemini_for_skill = None
        load_gemini_api_key = None

try:
    from notifier import send_push_message
except ImportError:
    send_push_message = None


def normalize_exp_date(date_val):
    """將到期日統一轉換為 IBKR 格式之 YYYYMMDD 字串 (如 20261016)"""
    if pd.isna(date_val):
        return ""
    s = str(date_val).strip()
    if " " in s:
        s = s.split(" ")[0]
    s = s.replace("/", "-")
    parts = s.split("-")
    if len(parts) == 3:
        if len(parts[0]) == 4:
            return f"{parts[0]}{parts[1].zfill(2)}{parts[2].zfill(2)}"
        elif len(parts[2]) == 4:
            return f"{parts[2]}{parts[0].zfill(2)}{parts[1].zfill(2)}"
        elif len(parts[2]) == 2:
            return f"20{parts[2]}{parts[0].zfill(2)}{parts[1].zfill(2)}"
    cleaned = re.sub(r"\D", "", s)
    return cleaned if len(cleaned) == 8 else s


def format_exp_date_display(exp_date_val):
    """將 YYYYMMDD 格式化為 YYYY-MM-DD"""
    s = normalize_exp_date(exp_date_val)
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return str(exp_date_val)


class UoaAnalysisSkill:
    """
    Barchart 個股與 ETF 異常期權大單 AI 深度量化分析技能
    """

    def __init__(self, api_key=None, env_path=None):
        self.env_path = env_path or os.path.join(BASE_DIR, ".env")
        self.api_key = api_key or (load_gemini_api_key(self.env_path) if load_gemini_api_key else None)
        self.barchart_dir = BARCHART_DIR
        self.old_dir = OLD_DIR
        self.reports_dir = REPORTS_DIR

    def is_valid_csv_file(self, file_path, min_rows=1):
        """檢查 CSV 檔案是否存在、非空且含有實際數據行"""
        if not file_path or not os.path.exists(file_path):
            return False
        if os.path.getsize(file_path) < 350:
            return False
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                valid_lines = [
                    l.strip()
                    for l in f
                    if l.strip() and not l.strip().startswith('"Downloaded') and not l.strip().startswith("Downloaded")
                ]
            return len(valid_lines) >= (min_rows + 1)
        except Exception:
            return False

    def get_latest_csv_file(self, pattern, directory=None):
        """尋找指定目錄或 old/ 目錄下最新且含有實際數據的 CSV 檔案"""
        search_dir = directory or self.barchart_dir
        matches = [f for f in glob.glob(os.path.join(search_dir, pattern)) if self.is_valid_csv_file(f)]
        if not matches:
            sub_old = os.path.join(search_dir, "old")
            if os.path.exists(sub_old):
                matches = [f for f in glob.glob(os.path.join(sub_old, pattern)) if self.is_valid_csv_file(f)]

        if not matches:
            all_m = glob.glob(os.path.join(search_dir, pattern))
            if not all_m:
                sub_old = os.path.join(search_dir, "old")
                if os.path.exists(sub_old):
                    all_m = glob.glob(os.path.join(sub_old, pattern))
            if all_m:
                all_m.sort(key=os.path.getmtime, reverse=True)
                return all_m[0]
            return None

        matches.sort(key=os.path.getmtime, reverse=True)
        return matches[0]

    def clean_and_prepare_data(self, stock_csv, etf_csv, top_n_stocks=60, top_n_etfs=40):
        """
        清洗並篩選二份 Barchart 數據源：個股 UOA、ETF UOA。
        過濾 0DTE 極短線雜訊 (7 <= DTE <= 120)，按 Vol/OI 降序排列。
        """
        if not stock_csv or not os.path.exists(stock_csv):
            raise FileNotFoundError(f"[UoaSkill] 找不到個股 CSV: {stock_csv}")
        if not etf_csv or not os.path.exists(etf_csv):
            raise FileNotFoundError(f"[UoaSkill] 找不到 ETF CSV: {etf_csv}")

        print(f"[INFO] [UoaSkill] 讀取個股 CSV: {os.path.basename(stock_csv)}")
        df_stock = pd.read_csv(stock_csv)
        df_stock = df_stock[df_stock["Symbol"].notna() & (~df_stock["Symbol"].astype(str).str.contains("Downloaded", case=False, na=False))]
        df_stock["DTE"] = pd.to_numeric(df_stock.get("DTE"), errors="coerce")
        if "Vol/OI" in df_stock.columns:
            df_stock["Vol/OI"] = pd.to_numeric(df_stock["Vol/OI"].astype(str).str.replace(",", ""), errors="coerce")

        df_stock_filtered = df_stock[(df_stock["DTE"] >= 7) & (df_stock["DTE"] <= 120)].dropna(subset=["Symbol", "Strike"])
        if "Vol/OI" in df_stock_filtered.columns:
            df_stock_filtered = df_stock_filtered.sort_values(by="Vol/OI", ascending=False)
        if top_n_stocks and len(df_stock_filtered) > top_n_stocks:
            df_stock_filtered = df_stock_filtered.head(top_n_stocks)

        print(f"[INFO] [UoaSkill] 讀取 ETF CSV: {os.path.basename(etf_csv)}")
        df_etf = pd.read_csv(etf_csv)
        df_etf = df_etf[df_etf["Symbol"].notna() & (~df_etf["Symbol"].astype(str).str.contains("Downloaded", case=False, na=False))]
        df_etf["DTE"] = pd.to_numeric(df_etf.get("DTE"), errors="coerce")
        if "Vol/OI" in df_etf.columns:
            df_etf["Vol/OI"] = pd.to_numeric(df_etf["Vol/OI"].astype(str).str.replace(",", ""), errors="coerce")

        df_etf_filtered = df_etf[(df_etf["DTE"] >= 7) & (df_etf["DTE"] <= 120)].dropna(subset=["Symbol", "Strike"])
        if "Vol/OI" in df_etf_filtered.columns:
            df_etf_filtered = df_etf_filtered.sort_values(by="Vol/OI", ascending=False)
        if top_n_etfs and len(df_etf_filtered) > top_n_etfs:
            df_etf_filtered = df_etf_filtered.head(top_n_etfs)

        print(f"[INFO] [UoaSkill] 數據準備完成：個股篩選 {len(df_stock_filtered)} 筆，ETF 篩選 {len(df_etf_filtered)} 筆 (均排除 0DTE 雜訊)")

        cols = [c for c in ["Symbol", "Price~", "Exp Date", "DTE", "Type", "Strike", "Bid", "Ask", "Volume", "Open Int", "Vol/OI", "Delta"] if c in df_stock_filtered.columns]
        data_str = (
            "【個股異常期權數據 (Stocks UOA)】\n"
            + df_stock_filtered[cols].to_string(index=False)
            + "\n\n【ETF異常期權數據 (ETFs UOA)】\n"
            + df_etf_filtered[cols].to_string(index=False)
        )

        return data_str, df_stock_filtered, df_etf_filtered

    def build_analysis_prompt(self, data_str):
        """
        組裝華爾街頂級量化期權分析提示詞，要求產出 4 檔預選合約：
          1. 個股 BUY CALL
          2. 個股 BUY PUT
          3. ETF BUY CALL
          4. ETF BUY PUT
        """
        return f"""你是一位華爾街頂級的量化期權基金經理人與波動率大單分析主管。
以下提供兩份來自 Barchart 官方的異常期權大單掃描數據（個股異常期權 Stocks UOA、ETF 異常期權 ETFs UOA）。
請進行深度的多因子籌碼與量化分析，分別針對個股與 ETF，各精選出「1檔最佳 BUY CALL」與「1檔最佳 BUY PUT」，共計精選出【4 檔預選期權標的】：

======================================================================
【精選目標】
1. 預選一：個股突破交易 (BUY CALL) - 來自個股 UOA
2. 預選二：個股做空/避險 (BUY PUT) - 來自個股 UOA
3. 預選三：ETF 順勢佈局 (BUY CALL) - 來自 ETF UOA
4. 預選四：ETF 宏觀對沖 (BUY PUT) - 來自 ETF UOA

======================================================================
【量化選約標準】
- 排除 0DTE 極短線噪聲，聚焦 7 至 120 天波段大單。
- 尋求 Vol/OI 倍數顯著爆量、主力主動進場建倉之合約。
- Delta 位於健康區間 (Call: 0.35~0.85 / Put: -0.35~-0.85)，具備高勝率與優良盈虧比。

======================================================================
【輸出格式規範】
請嚴格依據下列 Markdown 結構輸出，不得遺漏任一項：

【手機速覽摘要】
1. 個股 BUY CALL 首選: [標的代號] 履約價 $[Strike] 到期日 [YYYY-MM-DD]，核心看多理由
2. 個股 BUY PUT 首選: [標的代號] 履約價 $[Strike] 到期日 [YYYY-MM-DD]，核心看空理由
3. ETF BUY CALL 首選: [標的代號] 履約價 $[Strike] 到期日 [YYYY-MM-DD]，板塊/宏觀順勢邏輯
4. ETF BUY PUT 首選: [標的代號] 履約價 $[Strike] 到期日 [YYYY-MM-DD]，大盤對沖/避險邏輯

---

### 📊 詳細深度量化分析報告

#### 📌 【預選一：個股突破交易 (BUY CALL)】
- **推薦標的代號**: [例如 AAPL]
- **建議策略**: BUY CALL
- **合約規格**: 履約價 $[Strike]、到期日 [YYYY-MM-DD]、DTE [數字]天、Delta [數值]、Vol/OI [數值]
- **進場理由與量化籌碼**: [分析主力真金白銀爆量掃貨、突破動能與波動率]

#### 📌 【預選二：個股做空/避險 (BUY PUT)】
- **推薦標的代號**: [例如 TSLA]
- **建議策略**: BUY PUT
- **合約規格**: 履約價 $[Strike]、到期日 [YYYY-MM-DD]、DTE [數字]天、Delta [數值]、Vol/OI [數值]
- **進場理由與量化籌碼**: [分析機構重金放空、跌破支撐或弱勢結構]

#### 📌 【預選三：ETF 順勢佈局 (BUY CALL)】
- **推薦標的代號**: [例如 QQQ]
- **建議策略**: BUY CALL
- **合約規格**: 履約價 $[Strike]、到期日 [YYYY-MM-DD]、DTE [數字]天、Delta [數值]、Vol/OI [數值]
- **進場理由與宏觀局勢**: [分析資金回流大盤/熱門板塊之多頭共振]

#### 📌 【預選四：ETF 宏觀對沖 (BUY PUT)】
- **推薦標的代號**: [例如 SPY]
- **建議策略**: BUY PUT
- **合約規格**: 履約價 $[Strike]、到期日 [YYYY-MM-DD]、DTE [數字]天、Delta [數值]、Vol/OI [數值]
- **進場理由與宏觀局勢**: [分析機構防禦性避險或下行風險保護買盤]

---
### 💡 綜合風控與部位管理建議
（包含停利停損規劃、各標的倉位配比與 Greeks 對沖提醒）

數據如下：
{data_str}
"""

    def is_report_complete(self, text):
        """驗證報告完整性：包含 4 檔預選建議"""
        if not text or len(text.strip()) < 800:
            return False
        has_s1 = "預選一" in text or ("個股" in text and "CALL" in text.upper())
        has_s2 = "預選二" in text or ("個股" in text and "PUT" in text.upper())
        has_s3 = "預選三" in text or ("ETF" in text and "CALL" in text.upper())
        has_s4 = "預選四" in text or ("ETF" in text and "PUT" in text.upper())
        return has_s1 and has_s2 and has_s3 and has_s4

    def generate_analysis_report(self, data_str, max_retries=3):
        """調用共享之 Gemini Helper 生成量化深度報告"""
        if not self.api_key:
            raise ValueError("[UoaSkill] Gemini API Key 未配置，請檢查 trade/.env")

        prompt = self.build_analysis_prompt(data_str)
        analysis_text = None
        last_error = None

        for attempt in range(1, max_retries + 1):
            print(f"\n[INFO] [UoaSkill] 正在向 Gemini 請求 UOA 深度量化分析報告 (嘗試第 {attempt}/{max_retries} 次)...")
            try:
                res = call_gemini_for_skill(
                    prompt=prompt,
                    api_key=self.api_key,
                    timeout=50,
                    min_chars=800,
                    validate_func=self.is_report_complete,
                    max_output_tokens=80096,
                    temperature=0.3,
                )
                if res and self.is_report_complete(res):
                    analysis_text = res
                    print(f"[SUCCESS] [UoaSkill] ✅ 成功取得 Gemini 4 檔預選量化分析報告 (第 {attempt} 次嘗試成功，長度: {len(analysis_text)} 字)！")
                    break
                else:
                    raise ValueError("Gemini 回傳內容長度不足或未包含完整的 4 檔預選建議")
            except Exception as e:
                last_error = e
                print(f"[WARN] [UoaSkill] ⚠️ 第 {attempt}/{max_retries} 次取得報告失敗: {e}")
                if attempt < max_retries:
                    time.sleep(attempt * 2)

        if not analysis_text:
            raise RuntimeError(f"[UoaSkill] 連續嘗試 {max_retries} 次均無法取得合格分析報告: {last_error}")

        return analysis_text

    def parse_preselected_targets(self, analysis_text, df_stock=None, df_etf=None):
        """
        從報告中結構化解析 4 檔預選標的：
          1. 個股 BUY CALL
          2. 個股 BUY PUT
          3. ETF BUY CALL
          4. ETF BUY PUT
        若 AI 未完整產出則由 DataFrame 首位降級備援。
        """
        targets = []
        specs = [
            {"key": "預選一", "source": "Stock", "action": "BUY", "type": "CALL", "right": "C", "df": df_stock},
            {"key": "預選二", "source": "Stock", "action": "BUY", "type": "PUT", "right": "P", "df": df_stock},
            {"key": "預選三", "source": "ETF",   "action": "BUY", "type": "CALL", "right": "C", "df": df_etf},
            {"key": "預選四", "source": "ETF",   "action": "BUY", "type": "PUT", "right": "P", "df": df_etf},
        ]

        sections = re.split(r"####\s*📌?\s*【預選", analysis_text)

        for i, spec in enumerate(specs):
            symbol = None
            strike = None
            exp_date = None
            ref_price = 1.0

            # 嘗試自專屬章節解析
            target_section = ""
            for sec in sections[1:]:
                # 支援中文數字或 source + type 比對
                num_chinese = spec["key"].replace("預選", "")
                if num_chinese in sec[:30] or (spec["source"] in sec[:40] and spec["type"] in sec[:40]):
                    target_section = sec
                    break

            if target_section:
                # 1. 提取 Symbol
                sym_m = re.search(r"(?:推薦標的代號|標的代號|Symbol)[^A-Za-z0-9\n]*([A-Za-z0-9\.\-]+)", target_section, re.IGNORECASE)
                if sym_m:
                    symbol = sym_m.group(1).strip().upper()

                # 2. 提取 Strike
                strike_m = re.search(r"(?:履約價|Strike)[^\d\n]*\$?\s*([0-9]+\.?[0-9]*)", target_section, re.IGNORECASE)
                if strike_m:
                    try:
                        strike = float(strike_m.group(1))
                    except ValueError:
                        pass

                # 3. 提取 Exp Date
                exp_m = re.search(r"(?:到期日|Exp(?:iration)?\s*(?:Date)?)[^\d\n]*([0-9]{4}[-/][0-9]{2}[-/][0-9]{2}|[0-9]{8})", target_section, re.IGNORECASE)
                if exp_m:
                    exp_date = normalize_exp_date(exp_m.group(1))

            # 備用提取 1: 自頂部【手機速覽摘要】解析
            if not symbol or not strike or not exp_date:
                idx_str = str(i + 1)
                summary_pattern = rf"{idx_str}\.\s*(?:個股|ETF)[^\n]+"
                summary_m = re.search(summary_pattern, analysis_text, re.IGNORECASE)
                if summary_m:
                    s_line = summary_m.group(0)
                    if not symbol:
                        s_sym = re.search(r"(?:首選|代號)[^A-Za-z0-9\n]*([A-Za-z0-9\.\-]+)", s_line)
                        if s_sym:
                            symbol = s_sym.group(1).strip().upper()
                    if not strike:
                        s_str = re.search(r"(?:履約價|Strike)[^\d\n]*\$?\s*([0-9]+\.?[0-9]*)", s_line, re.IGNORECASE)
                        if s_str:
                            try:
                                strike = float(s_str.group(1))
                            except ValueError:
                                pass
                    if not exp_date:
                        s_exp = re.search(r"(?:到期日|Exp(?:iration)?\s*(?:Date)?)[^\d\n]*([0-9]{4}[-/][0-9]{2}[-/][0-9]{2}|[0-9]{8})", s_line, re.IGNORECASE)
                        if s_exp:
                            exp_date = normalize_exp_date(s_exp.group(1))

            # 備援提取 2: 若有 symbol，自 CSV 中尋找該 symbol 之對應 Strike / Exp Date / Ref Price
            df_src = spec["df"]
            if df_src is not None and not df_src.empty:
                if symbol:
                    df_sym = df_src[(df_src["Symbol"].astype(str).str.upper() == symbol) & (df_src["Type"].astype(str).str.upper() == spec["type"])]
                    if df_sym.empty:
                        df_sym = df_src[df_src["Symbol"].astype(str).str.upper() == symbol]

                    if not df_sym.empty:
                        if not strike:
                            strike = float(df_sym.iloc[0]["Strike"])
                        if not exp_date:
                            exp_date = normalize_exp_date(df_sym.iloc[0]["Exp Date"])

                        # 取得真實 Ask/Price
                        matched = df_sym
                        if strike:
                            matched_strike = matched[abs(pd.to_numeric(matched["Strike"], errors="coerce") - strike) < 0.01]
                            if not matched_strike.empty:
                                matched = matched_strike
                        ref_price = float(matched.iloc[0].get("Ask", matched.iloc[0].get("Latest", matched.iloc[0].get("Price~", 1.0))) or 1.0)

                # 備援提取 3: 只有在完全無法獲得 symbol 時，才降級取 df_src 首筆
                if not symbol:
                    df_dir = df_src[df_src["Type"].astype(str).str.upper() == spec["type"]]
                    top_row = df_dir.iloc[0] if not df_dir.empty else df_src.iloc[0]
                    symbol = str(top_row["Symbol"]).strip().upper()
                    strike = float(top_row["Strike"])
                    exp_date = normalize_exp_date(top_row["Exp Date"])
                    ref_price = float(top_row.get("Ask", top_row.get("Latest", top_row.get("Price~", 1.0))) or 1.0)

            targets.append({
                "index": i + 1,
                "label": spec["key"],
                "source": spec["source"],
                "symbol": symbol or ("SPY" if spec["source"] == "ETF" else "AAPL"),
                "action": spec["action"],
                "type": spec["type"],
                "right": spec["right"],
                "strategy": f"{spec['action']} {spec['type']}",
                "strike": strike or 100.0,
                "exp_date": exp_date or datetime.date.today().strftime("%Y%m%d"),
                "exp_date_disp": format_exp_date_display(exp_date or datetime.date.today().strftime("%Y%m%d")),
                "ref_price": ref_price,
            })

        print(f"[INFO] [UoaSkill] 成功解析 4 檔預選期權標的:")
        for t in targets:
            print(f"  • [{t['label']}] {t['source']} {t['strategy']}: {t['symbol']} Strike=${t['strike']} Exp={t['exp_date_disp']}")

        return targets

    def save_report(self, analysis_text, target_dir=None):
        """儲存報告至 reports 目錄與 latest_ai_analysis.txt 供看板與下單模組使用"""
        target_dir = target_dir or self.reports_dir
        now = datetime.datetime.now()
        ts_str = now.strftime("%Y-%m-%d_%H%M%S")
        now_readable = now.strftime("%Y-%m-%d %H:%M:%S")

        archive_filename = f"ai_analysis_{ts_str}.txt"
        archive_path = os.path.join(target_dir, archive_filename)
        latest_path = os.path.join(target_dir, "latest_ai_analysis.txt")
        root_latest_path = os.path.join(self.barchart_dir, "latest_ai_analysis.txt")

        file_content = f"==分析時間：{now_readable}==\n{analysis_text}\n"

        for p in [archive_path, latest_path, root_latest_path]:
            try:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(file_content)
            except Exception as e:
                print(f"[WARN] [UoaSkill] 寫入 {p} 失敗: {e}")

        print(f"[INFO] [UoaSkill] 完整報告存檔至：{archive_path}")
        print(f"[INFO] [UoaSkill] 最新報告同步至：{latest_path}")
        return archive_filename, archive_path

    def send_line_summary(self, analysis_text, archive_filename=None):
        """推播 4 檔預選核心速覽摘要到手機 LINE"""
        if not send_push_message:
            return False

        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        summary_lines = []
        if "【手機速覽摘要】" in analysis_text:
            parts = analysis_text.split("【手機速覽摘要】", 1)[1]
            for line in parts.split("\n"):
                line_str = line.strip()
                if not line_str or line_str.startswith("---") or line_str.startswith("###"):
                    if summary_lines:
                        break
                    continue
                summary_lines.append(line_str.replace("*", ""))
                if len(summary_lines) >= 8:
                    break

        if not summary_lines:
            for line in analysis_text.split("\n"):
                if any(k in line for k in ["預選", "BUY CALL", "BUY PUT", "首選", "推薦"]):
                    summary_lines.append(line.replace("*", "").strip())
                    if len(summary_lines) >= 8:
                        break

        summary_body = "\n".join(summary_lines) if summary_lines else analysis_text[:500]
        line_msg = f"""🔥【Barchart UOA 4檔預選速覽】
🕒 時間：{now_str}

{summary_body}

📁 完整報告：{archive_filename or 'latest_ai_analysis.txt'}"""

        print(f"[INFO] [UoaSkill] 正在發送 LINE 推播摘要...")
        ok = send_push_message(line_msg.strip())
        if ok:
            print(f"[SUCCESS] [UoaSkill] ✅ LINE 速覽摘要推播成功！")
        return ok
