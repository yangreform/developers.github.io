#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""

UOA Analysis Skill: 異常期權大單與價差深度量化分析技能 (trade/skills/uoa_skill/uoa.py)
================================================================================
職責：
  1. 清洗與篩選三份 Barchart 數據源 (個股 UOA、ETF UOA、Bull Put 垂直價差)
  2. 自動過濾 0DTE 極短線雜訊 (7 <= DTE <= 120)，按 Vol/OI 與勝率排序
  3. 垂直價差智能備援：當日無數據時自動回溯歷史 old/ 候選組合
  4. 整合共享之 Gemini Helper (gemini-3.6-flash 首選)，執行三大投資建議量化解讀
  5. 產出結構化文字檔 (latest_ai_analysis.txt) 供看板與下單模組使用，並推播 LINE 速覽
================================================================================
"""

import os
import sys
import glob
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

BARCHART_DIR = os.path.join(BASE_DIR, "Barchart")
OLD_DIR = os.path.join(BARCHART_DIR, "old")
REPORTS_DIR = os.path.join(BARCHART_DIR, "reports")

os.makedirs(BARCHART_DIR, exist_ok=True)
os.makedirs(OLD_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)

# 載入共享模組
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


class UoaAnalysisSkill:
    """
    Barchart 異常期權與垂直價差 AI 深度量化分析技能
    """

    def __init__(self, api_key=None, env_path=None):
        self.env_path = env_path or os.path.join(BASE_DIR, ".env")
        self.api_key = api_key or (load_gemini_api_key(self.env_path) if load_gemini_api_key else None)
        self.barchart_dir = BARCHART_DIR
        self.old_dir = OLD_DIR
        self.reports_dir = REPORTS_DIR

    def is_valid_csv_file(self, file_path, min_rows=1):
        """
        檢查 CSV 檔案是否存在、非空且含有實際數據行（排除僅有表頭或下載 footer 標記）
        """
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
        """
        尋找指定目錄或 old/ 目錄下最新且含有實際數據的 CSV 檔案。
        """
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

    def clean_and_prepare_data(
        self, stock_csv, etf_csv, bull_put_csv=None, top_n_stocks=60, top_n_etfs=40, top_n_bull_put=30
    ):
        """
        清洗並準備 3 個 CSV 數據源：
          - 個股與 ETF：篩選 7 <= DTE <= 120 (排除 0DTE 雜訊)，依 Vol/OI 降序排列
          - Bull Put 價差：篩選未過期合約，自動結合歷史備援，依跌破機率昇序/最大報酬率降序排序
        """
        if not stock_csv or not os.path.exists(stock_csv):
            raise FileNotFoundError(f"[UoaSkill] 找不到個股 CSV: {stock_csv}")
        if not etf_csv or not os.path.exists(etf_csv):
            raise FileNotFoundError(f"[UoaSkill] 找不到 ETF CSV: {etf_csv}")

        print(f"[INFO] [UoaSkill] 讀取個股 CSV: {os.path.basename(stock_csv)}")
        df_stock = pd.read_csv(stock_csv)
        df_stock["DTE"] = pd.to_numeric(df_stock.get("DTE"), errors="coerce")
        if "Vol/OI" in df_stock.columns:
            df_stock["Vol/OI"] = pd.to_numeric(df_stock["Vol/OI"], errors="coerce")

        df_stock_filtered = df_stock[(df_stock["DTE"] >= 7) & (df_stock["DTE"] <= 120)].dropna(
            subset=["Symbol", "Strike"]
        )
        if "Vol/OI" in df_stock_filtered.columns:
            df_stock_filtered = df_stock_filtered.sort_values(by="Vol/OI", ascending=False)
        if top_n_stocks and len(df_stock_filtered) > top_n_stocks:
            df_stock_filtered = df_stock_filtered.head(top_n_stocks)

        print(f"[INFO] [UoaSkill] 讀取 ETF CSV: {os.path.basename(etf_csv)}")
        df_etf = pd.read_csv(etf_csv)
        df_etf["DTE"] = pd.to_numeric(df_etf.get("DTE"), errors="coerce")
        if "Vol/OI" in df_etf.columns:
            df_etf["Vol/OI"] = pd.to_numeric(df_etf["Vol/OI"], errors="coerce")

        df_etf_filtered = df_etf[(df_etf["DTE"] >= 7) & (df_etf["DTE"] <= 120)].dropna(
            subset=["Symbol", "Strike"]
        )
        if "Vol/OI" in df_etf_filtered.columns:
            df_etf_filtered = df_etf_filtered.sort_values(by="Vol/OI", ascending=False)
        if top_n_etfs and len(df_etf_filtered) > top_n_etfs:
            df_etf_filtered = df_etf_filtered.head(top_n_etfs)

        # Bull Put 價差篩選與備援
        df_bull_put_filtered = None
        candidate_bp_files = []
        if bull_put_csv and os.path.exists(bull_put_csv):
            candidate_bp_files.append(bull_put_csv)

        all_bp_found = glob.glob(os.path.join(self.barchart_dir, "*bull-put*.csv")) + glob.glob(
            os.path.join(self.old_dir, "*bull-put*.csv")
        )
        all_bp_found.sort(key=os.path.getmtime, reverse=True)
        for f in all_bp_found:
            if f not in candidate_bp_files:
                candidate_bp_files.append(f)

        all_bp_dfs = []
        today_str = datetime.date.today().strftime("%Y-%m-%d")

        for bp_file in candidate_bp_files:
            if not os.path.exists(bp_file) or os.path.getsize(bp_file) < 350:
                continue
            try:
                df_bp = pd.read_csv(bp_file)
                df_bp = df_bp.dropna(subset=["Symbol", "Exp Date"])
                df_bp = df_bp[~df_bp["Symbol"].astype(str).str.contains("Downloaded", case=False, na=False)]
                df_bp = df_bp[df_bp["Exp Date"].astype(str) >= today_str]
                if not df_bp.empty:
                    all_bp_dfs.append(df_bp)
            except Exception:
                continue

        if all_bp_dfs:
            combined_bp = pd.concat(all_bp_dfs, ignore_index=True)
            dedup_cols = [c for c in ["Symbol", "Exp Date", "Leg1 Strike", "Leg2 Strike"] if c in combined_bp.columns]
            if dedup_cols:
                combined_bp = combined_bp.drop_duplicates(subset=dedup_cols)

            if "Loss Prob" in combined_bp.columns:
                loss_clean = combined_bp["Loss Prob"].astype(str).str.replace("%", "").str.strip()
                combined_bp["_loss_sort"] = pd.to_numeric(loss_clean, errors="coerce")
                combined_bp = combined_bp.sort_values(by="_loss_sort", ascending=True)
                combined_bp = combined_bp.drop(columns=["_loss_sort"])

            if top_n_bull_put and len(combined_bp) > top_n_bull_put:
                combined_bp = combined_bp.head(top_n_bull_put)

            df_bull_put_filtered = combined_bp
            symbols_summary = list(df_bull_put_filtered["Symbol"].unique())[:6]
            print(f"[INFO] [UoaSkill] 清洗完成：Bull Put 篩選 {len(df_bull_put_filtered)} 筆未過期候選組合 (標的: {symbols_summary})")

        bp_count = len(df_bull_put_filtered) if df_bull_put_filtered is not None else 0
        print(f"[INFO] [UoaSkill] 數據準備完成：個股篩選 {len(df_stock_filtered)} 筆，ETF 篩選 {len(df_etf_filtered)} 筆，Bull Put 篩選 {bp_count} 筆")

        data_str = (
            "【個股異常期權數據 (UOA)】\n"
            + df_stock_filtered.to_csv(index=False)
            + "\n\n【ETF異常期權數據 (UOA)】\n"
            + df_etf_filtered.to_csv(index=False)
        )
        if df_bull_put_filtered is not None and not df_bull_put_filtered.empty:
            data_str += "\n\n【Bull Put Spread 垂直價差篩選數據】\n" + df_bull_put_filtered.to_csv(index=False)

        return data_str

    def build_analysis_prompt(self, data_str):
        """
        組裝華爾街頂級量化期權分析提示詞。
        """
        return f"""你是一位頂級的華爾街量化與衍生品資深分析師，擅長追蹤 Smart Money (聰明錢) 動向以及期權價差收租策略。
請根據以下我提供的三份 Barchart 數據（個股異常期權 UOA、ETF 異常期權 UOA、Bull Put 垂直價差篩選器），為我產出包含【三大核心投資建議】的量化分析報告：

請嚴格遵守以下格式產出：

【手機速覽摘要】
1. 個股突破首選：[標的代碼] [建議方向 Buy Call / Buy Put]，核心理由與合約行使價/到期日
2. ETF 趨勢首選：[標的代碼] [建議方向 Buy Call / Buy Put]，宏觀邏輯與合約行使價/到期日
3. Bull Put 最佳組合：[標的代碼] [賣出 Leg1 / 買入 Leg2 Put]，到期日、最大報酬率%、跌破機率%

---

### 📊 詳細深度量化分析報告

#### 📌 【投資建議一：個股突破交易】（來源：個股異常期權 UOA）
- **推薦標的代號**：
- **建議策略**：Buy Call 或 Buy Put
- **合約規格**：履約價 (Strike)、到期日 (Exp Date / DTE)、Delta、Vol/OI 倍數
- **進場理由與量化籌碼**：分析主力資金 (Smart Money) 爆量建倉意圖、支撐壓力與波動率

#### 📌 【投資建議二：ETF 趨勢/宏觀對沖】（來源：ETF 異常期權 UOA）
- **推薦標的代號**：
- **建議策略**：Buy Call 或 Buy Put
- **合約規格**：履約價 (Strike)、到期日 (Exp Date / DTE)、Delta、Vol/OI 倍數
- **進場理由與宏觀局勢**：大盤/板塊資金流向、避險或做多意圖

#### 📌 【投資建議三：Bull Put 垂直價差最佳商品組合】（來源：Bull Put Spread 數據）
- **推薦標的代號**：
- **最佳商品組合規格 (Spread 結構)**：
  - 標的現價 (Price)：
  - 到期日 (Exp Date) 與天數 (DTE)：
  - 賣出下翼 Put (Leg 1 Short Strike @ Bid)：
  - 買入保護 Put (Leg 2 Long Strike @ Ask)：
  - 淨權利金收入 (Net Credit / Max Profit)：
  - 最大風險虧損 (Max Loss)：
  - 損益兩平點 (Break-Even) 與安全緩衝空間 (BE Buffer %)：
  - 最大報酬率 (Max Profit %)：
  - 跌破虧損機率 (Loss Probability) / 預估勝率：
  - 隱含波動率位階 (IV Rank)：
- **最佳組合評選理由**：說明為何此組為當前全部候選組合中的最佳解（考量安全邊際、下檔支撐力道、報酬率與勝率之性價比 Risk/Reward）。

---
### 💡 綜合風控與部位管理建議
（包含止損停利設定、保證金運用與 Greeks Delta/Theta 對沖提醒）

數據如下：
{data_str}
"""

    def is_report_complete(self, text):
        """
        驗證報告完整性：字數 >= 1000 且包含三大核心投資建議。
        """
        if not text or len(text.strip()) < 1000:
            return False
        has_s1 = any(k in text for k in ["投資建議一", "建議一", "個股突破"])
        has_s2 = any(k in text for k in ["投資建議二", "建議二", "ETF"])
        has_s3 = any(k in text for k in ["投資建議三", "建議三", "Bull Put", "垂直價差"])
        return has_s1 and has_s2 and has_s3

    def generate_analysis_report(self, data_str, max_retries=3):
        """
        調用共享之 Gemini Helper 生成量化深度報告，若未成功則自動重試。
        """
        if not self.api_key:
            raise ValueError("[UoaSkill] Gemini API Key 未配置，請檢查 trade/.env")

        prompt = self.build_analysis_prompt(data_str)
        analysis_text = None
        last_error = None

        for attempt in range(1, max_retries + 1):
            print(f"\n[INFO] [UoaSkill] 正在向 Gemini 請求深度量化分析報告 (嘗試第 {attempt}/{max_retries} 次)...")
            try:
                res = call_gemini_for_skill(
                    prompt=prompt,
                    api_key=self.api_key,
                    timeout=50,
                    min_chars=1000,
                    validate_func=self.is_report_complete,
                    max_output_tokens=8096,
                    temperature=0.3,
                )
                if res and self.is_report_complete(res):
                    analysis_text = res
                    print(f"[SUCCESS] [UoaSkill] ✅ 成功取得 Gemini 深度量化分析報告 (第 {attempt} 次嘗試成功，長度: {len(analysis_text)} 字)！")
                    break
                else:
                    raise ValueError("Gemini 回傳內容為空、長度不足或未通過三大建議完整性校驗")
            except Exception as e:
                last_error = e
                print(f"[WARN] [UoaSkill] ⚠️ 第 {attempt}/{max_retries} 次取得報告失敗: {e}")
                if attempt < max_retries:
                    sleep_sec = attempt * 3
                    print(f"[INFO] [UoaSkill] 等待 {sleep_sec} 秒後進行第 {attempt + 1} 次重試...")
                    time.sleep(sleep_sec)

        if not analysis_text:
            raise RuntimeError(f"[UoaSkill] 連續嘗試 {max_retries} 次均無法取得合格分析報告: {last_error}")

        return analysis_text

    def save_report(self, analysis_text, target_dir=None):
        """
        儲存報告至 reports 目錄與 latest_ai_analysis.txt 指標檔。
        """
        target_dir = target_dir or self.reports_dir
        now = datetime.datetime.now()
        ts_str = now.strftime("%Y-%m-%d_%H%M%S")
        now_readable = now.strftime("%Y-%m-%d %H:%M:%S")

        archive_filename = f"ai_analysis_{ts_str}.txt"
        archive_path = os.path.join(target_dir, archive_filename)
        latest_path = os.path.join(target_dir, "latest_ai_analysis.txt")
        root_latest_path = os.path.join(self.barchart_dir, "latest_ai_analysis.txt")
        base_latest_path = os.path.join(BASE_DIR, "latest_ai_analysis.txt")

        file_content = f"""==分析時間：{now_readable}==
{analysis_text}
"""

        for p in [archive_path, latest_path, root_latest_path, base_latest_path]:
            try:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(file_content)
            except Exception as e:
                print(f"[WARN] [UoaSkill] 寫入 {p} 失敗: {e}")

        print(f"[INFO] [UoaSkill] 完整文字檔已成功存檔至：{archive_path}")
        print(f"[INFO] [UoaSkill] 最新文字檔已同步至：{latest_path} 及 {base_latest_path}")
        return archive_filename, archive_path

    def send_line_summary(self, analysis_text, archive_filename=None):
        """
        推播核心速覽摘要到手機 LINE。
        """
        if not send_push_message:
            print("[WARN] [UoaSkill] 無法使用 send_push_message，略過 LINE 發送。")
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
                if len(summary_lines) >= 6:
                    break

        if not summary_lines:
            for line in analysis_text.split("\n"):
                clean_l = line.strip()
                if not clean_l:
                    continue
                if any(
                    keyword in clean_l
                    for keyword in ["1.", "2.", "3.", "首選", "建議一", "建議二", "建議三", "Bull Put", "標的", "Buy", "Call", "Put"]
                ):
                    summary_lines.append(clean_l.replace("*", ""))
                if len(summary_lines) >= 8:
                    break

        summary_block = "\n".join(summary_lines) if summary_lines else analysis_text[:8096]
        line_msg = f"""📊【Barchart 選擇權 AI 三大投資建議】
🕒 分析時間：{now_str}
📁 存檔檔名：{archive_filename or 'latest_ai_analysis.txt'}

🎯 核心重點速覽：
{summary_block}
"""

        print(f"[INFO] [UoaSkill] 正在推播 LINE 摘要訊息到手機...")
        ok = send_push_message(line_msg.strip())
        if ok:
            print("[SUCCESS] [UoaSkill] LINE 摘要訊息發送成功！")
        else:
            print("[WARN] [UoaSkill] LINE 發送過程有異常，請檢查 Token。")
        return ok

    def run_pipeline(
        self, stock_file=None, etf_file=None, bull_put_file=None, downloaded=None, max_retries=3, push_line=True
    ):
        """
        執行完整的 UOA 與垂直價差量化分析管線：
          1. 解析 3 個 CSV 路徑
          2. 數據清洗與過濾
          3. Gemini AI 深度量化分析
          4. 儲存報告並同步指標檔
          5. 手機 LINE 推播
        """
        stock_file = (downloaded.get("stocks") if downloaded else None) or stock_file or self.get_latest_csv_file("unusual-stock-options-activity-*.csv")
        etf_file = (downloaded.get("etfs") if downloaded else None) or etf_file or self.get_latest_csv_file("unusual-etf-options-activity-*.csv")
        bull_put_file = (downloaded.get("bull_put") if downloaded else None) or bull_put_file or self.get_latest_csv_file("*bull-put*.csv")

        if not stock_file or not os.path.exists(stock_file):
            raise FileNotFoundError(f"[UoaSkill] 找不到個股 CSV 檔案: {stock_file}")
        if not etf_file or not os.path.exists(etf_file):
            raise FileNotFoundError(f"[UoaSkill] 找不到 ETF CSV 檔案: {etf_file}")

        print(f"[INFO] [UoaSkill] 準備傳入量化分析的 3 個 CSV 檔案:")
        print(f"  1. 個股 UOA CSV   : {os.path.basename(stock_file)} ({os.path.getsize(stock_file):,} bytes)")
        print(f"  2. ETF UOA CSV    : {os.path.basename(etf_file)} ({os.path.getsize(etf_file):,} bytes)")
        if bull_put_file and os.path.exists(bull_put_file):
            print(f"  3. Bull Put CSV   : {os.path.basename(bull_put_file)} ({os.path.getsize(bull_put_file):,} bytes)")
        else:
            print(f"  3. Bull Put CSV   : (未找到，將僅分析個股與 ETF)")
            bull_put_file = None

        # 清洗與準備數據
        data_str = self.clean_and_prepare_data(stock_file, etf_file, bull_put_file)

        # 生成報告
        analysis_text = self.generate_analysis_report(data_str, max_retries=max_retries)

        # 存檔
        archive_fname, _ = self.save_report(analysis_text)

        # 推播
        if push_line:
            self.send_line_summary(analysis_text, archive_fname)

        return analysis_text, archive_fname
