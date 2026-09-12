#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Barchart Unusual Options Activity AI Analyzer
---------------------------------------------
1. Reads Gemini API Key from trade/.env (gemini_api:...)
2. Reads & cleans latest Stock and ETF CSVs from trade/Barchart/ using Pandas (7 <= DTE <= 120)
3. Calls Gemini REST API to generate quant analysis (Smart Money tracking)
4. Saves full text analysis to trade/Barchart/reports/ for display in trade/q.py dashboard
5. Pushes concise executive summary to mobile via LINE using trade/notifier.py
"""

import os
import sys
import glob
import json
import time
import datetime
import requests
import pandas as pd

# Ensure UTF-8 output in Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Path configuration
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")
BARCHART_DIR = os.path.join(BASE_DIR, "Barchart")
REPORTS_DIR = os.path.join(BARCHART_DIR, "reports")

os.makedirs(BARCHART_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)

# Add trade dir to sys.path to import notifier
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    from notifier import send_push_message
except ImportError:
    print("[WARN] Could not import send_push_message from notifier.py")
    send_push_message = None


def load_gemini_api_key(env_path=ENV_FILE):
    """
    Parse Gemini API key from trade/.env supporting:
      gemini_api:AIzaSy...
      gemini_api=AIzaSy...
      GEMINI_API_KEY=AIzaSy...
    """
    if not os.path.exists(env_path):
        raise FileNotFoundError(f".env file not found at: {env_path}")

    with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            for delim in [":", "="]:
                if delim in line:
                    k, v = line.split(delim, 1)
                    k = k.strip().lower()
                    v = v.strip().strip('"').strip("'")
                    if k in ["gemini_api", "gemini_api_key", "gemini_key", "gemini"]:
                        return v
    return None


def get_latest_csv_file(pattern, directory=BARCHART_DIR):
    """
    Find the newest CSV matching pattern in directory, with fallback to old/ subdirectory.
    """
    matches = glob.glob(os.path.join(directory, pattern))
    if not matches:
        old_dir = os.path.join(directory, "old")
        if os.path.exists(old_dir):
            matches = glob.glob(os.path.join(old_dir, pattern))
    if not matches:
        return None
    matches.sort(key=os.path.getmtime, reverse=True)
    return matches[0]


def clean_and_prepare_data(stock_csv, etf_csv, bull_put_csv=None, top_n_stocks=60, top_n_etfs=40, top_n_bull_put=30):
    """
    Read, clean, and filter CSV data using Pandas:
      - Stocks & ETFs: Filter 7 <= DTE <= 120 (removes 0DTE noise), sort by Vol/OI descending
      - Bull Put Spreads: Filter valid spreads, sort by Loss Prob asc / Max Profit% desc
      - Returns combined string data for Gemini prompt
    """
    print(f"[INFO] 讀取個股 CSV: {os.path.basename(stock_csv)}")
    df_stock = pd.read_csv(stock_csv)
    df_stock["DTE"] = pd.to_numeric(df_stock["DTE"], errors="coerce")
    if "Vol/OI" in df_stock.columns:
        df_stock["Vol/OI"] = pd.to_numeric(df_stock["Vol/OI"], errors="coerce")

    # Filter out footer rows and apply DTE criteria (7 <= DTE <= 120)
    df_stock_filtered = df_stock[(df_stock["DTE"] >= 7) & (df_stock["DTE"] <= 120)].dropna(subset=["Symbol", "Strike"])
    if "Vol/OI" in df_stock_filtered.columns:
        df_stock_filtered = df_stock_filtered.sort_values(by="Vol/OI", ascending=False)
    if top_n_stocks and len(df_stock_filtered) > top_n_stocks:
        df_stock_filtered = df_stock_filtered.head(top_n_stocks)

    print(f"[INFO] 讀取 ETF CSV: {os.path.basename(etf_csv)}")
    df_etf = pd.read_csv(etf_csv)
    df_etf["DTE"] = pd.to_numeric(df_etf["DTE"], errors="coerce")
    if "Vol/OI" in df_etf.columns:
        df_etf["Vol/OI"] = pd.to_numeric(df_etf["Vol/OI"], errors="coerce")

    df_etf_filtered = df_etf[(df_etf["DTE"] >= 7) & (df_etf["DTE"] <= 120)].dropna(subset=["Symbol", "Strike"])
    if "Vol/OI" in df_etf_filtered.columns:
        df_etf_filtered = df_etf_filtered.sort_values(by="Vol/OI", ascending=False)
    if top_n_etfs and len(df_etf_filtered) > top_n_etfs:
        df_etf_filtered = df_etf_filtered.head(top_n_etfs)

    df_bull_put_filtered = None
    if bull_put_csv and os.path.exists(bull_put_csv):
        print(f"[INFO] 讀取 Bull Put Spread CSV: {os.path.basename(bull_put_csv)}")
        try:
            df_bp = pd.read_csv(bull_put_csv)
            # Remove footer rows and empty rows
            df_bp = df_bp.dropna(subset=["Symbol", "Exp Date"])
            df_bp = df_bp[~df_bp["Symbol"].astype(str).str.contains("Downloaded", case=False, na=False)]

            # Sort by Loss Prob ascending (lowest probability of loss first)
            if "Loss Prob" in df_bp.columns:
                loss_clean = df_bp["Loss Prob"].astype(str).str.replace("%", "").str.strip()
                df_bp["_loss_sort"] = pd.to_numeric(loss_clean, errors="coerce")
                df_bp = df_bp.sort_values(by="_loss_sort", ascending=True)
                df_bp = df_bp.drop(columns=["_loss_sort"])

            if top_n_bull_put and len(df_bp) > top_n_bull_put:
                df_bp = df_bp.head(top_n_bull_put)

            df_bull_put_filtered = df_bp
            print(f"[INFO] 清洗完成：Bull Put 篩選 {len(df_bull_put_filtered)} 筆候選組合")
        except Exception as bp_err:
            print(f"[WARN] 讀取 Bull Put CSV 異常: {bp_err}")

    bp_count = len(df_bull_put_filtered) if df_bull_put_filtered is not None else 0
    print(f"[INFO] 數據準備完成：個股篩選 {len(df_stock_filtered)} 筆，ETF 篩選 {len(df_etf_filtered)} 筆，Bull Put 篩選 {bp_count} 筆")

    data_str = "【個股異常期權數據 (UOA)】\n" + df_stock_filtered.to_csv(index=False) + \
               "\n\n【ETF異常期權數據 (UOA)】\n" + df_etf_filtered.to_csv(index=False)
    if df_bull_put_filtered is not None and not df_bull_put_filtered.empty:
        data_str += "\n\n【Bull Put Spread 垂直價差篩選數據】\n" + df_bull_put_filtered.to_csv(index=False)
    return data_str


def call_gemini_rest(prompt, api_key):
    """
    Direct REST API call with model fallback chain.
    """
    candidate_models = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"]

    for m_name in candidate_models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m_name}:generateContent?key={api_key}"
        payload = {
            "contents": [
                {
                    "parts": [{"text": prompt}]
                }
            ],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 8096
            }
        }

        print(f"[INFO] 正在呼叫 Gemini 模型：{m_name} ...")
        try:
            r = requests.post(url, json=payload, timeout=60)
            if r.status_code == 200:
                res_json = r.json()
                text = res_json["candidates"][0]["content"]["parts"][0]["text"]
                print(f"[SUCCESS] 模型 {m_name} 呼叫成功！")
                return text
            else:
                err_msg = r.json().get("error", {}).get("message", r.text[:200])
                print(f"[WARN] 模型 {m_name} 回應錯誤 (代碼 {r.status_code}): {err_msg}，嘗試備用模型...")
        except Exception as e:
            print(f"[WARN] 模型 {m_name} 連線異常: {e}，嘗試備用模型...")

        time.sleep(1)

    raise RuntimeError("所有 Gemini 模型呼叫均未成功。")


def save_analysis_to_text_file(analysis_text, target_dir=REPORTS_DIR):
    """
    Save full analysis text into text files:
      1. Timestamped file: ai_analysis_YYYY-MM-DD_HHMMSS.txt
      2. Latest pointer file: latest_ai_analysis.txt
    """
    now = datetime.datetime.now()
    ts_str = now.strftime("%Y-%m-%d_%H%M%S")
    now_readable = now.strftime("%Y-%m-%d %H:%M:%S")

    archive_filename = f"ai_analysis_{ts_str}.txt"
    archive_path = os.path.join(target_dir, archive_filename)
    latest_path = os.path.join(target_dir, "latest_ai_analysis.txt")
    root_latest_path = os.path.join(BARCHART_DIR, "latest_ai_analysis.txt")
    base_latest_path = os.path.join(BASE_DIR, "latest_ai_analysis.txt")

    file_content = f"""==分析時間：{now_readable}==
{analysis_text}
"""

    with open(archive_path, "w", encoding="utf-8") as f:
        f.write(file_content)

    with open(latest_path, "w", encoding="utf-8") as f:
        f.write(file_content)

    with open(root_latest_path, "w", encoding="utf-8") as f:
        f.write(file_content)

    with open(base_latest_path, "w", encoding="utf-8") as f:
        f.write(file_content)

    print(f"[INFO] 完整文字檔已成功存檔至：{archive_path}")
    print(f"[INFO] 最新文字檔已同步至：{latest_path} 及 {base_latest_path}")
    return archive_filename, archive_path


def send_line_notification(analysis_text, archive_filename=None):
    """
    Send LINE notification with summary and notice that full report is saved to dashboard.
    Avoids LINE character limit truncation.
    """
    if not send_push_message:
        print("[WARN] 無法使用 send_push_message，略過 LINE 發送。")
        return False

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    # Extract key lines for executive summary
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
            if any(keyword in clean_l for keyword in ["1.", "2.", "3.", "首選", "建議一", "建議二", "建議三", "Bull Put", "標的", "Buy", "Call", "Put", "雙賣"]):
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

    print(f"[INFO] 正在推播 LINE 摘要訊息到手機...")
    ok = send_push_message(line_msg.strip())
    if ok:
        print("[SUCCESS] LINE 摘要訊息發送成功！")
    else:
        print("[WARN] LINE 發送過程有異常，請檢查 Token。")
    return ok


def run_analysis(stock_file=None, etf_file=None, bull_put_file=None):
    print("=" * 60)
    print(" Barchart 選擇權異動與價差 AI 分析模組啟動")
    print("=" * 60)

    # 1. 讀取 API Key
    api_key = load_gemini_api_key(ENV_FILE)
    if not api_key:
        print("[ERROR] 無法在 trade/.env 找到 gemini_api 設定")
        sys.exit(1)
    print(f"[INFO] 成功自 trade/.env 載入 gemini_api (長度: {len(api_key)})")

    # 2. 獲取最新 CSV 檔案
    if not stock_file:
        stock_file = get_latest_csv_file("unusual-stock-options-activity-*.csv")
    if not etf_file:
        etf_file = get_latest_csv_file("unusual-etf-options-activity-*.csv")
    if not bull_put_file:
        bull_put_file = get_latest_csv_file("*bull-put*.csv")

    if not stock_file or not os.path.exists(stock_file):
        print(f"[ERROR] 在 {BARCHART_DIR} 找不到個股 CSV 檔案")
        sys.exit(1)

    if not etf_file or not os.path.exists(etf_file):
        print(f"[ERROR] 在 {BARCHART_DIR} 找不到 ETF CSV 檔案")
        sys.exit(1)

    if bull_put_file and os.path.exists(bull_put_file):
        print(f"[INFO] 找到最新 Bull Put Spread CSV: {os.path.basename(bull_put_file)}")
    else:
        print(f"[WARN] 在 {BARCHART_DIR} 未找到 Bull Put Spread CSV 檔案，將僅分析個股與 ETF")
        bull_put_file = None

def build_analysis_prompt(data_str):
    """
    組裝華爾街量化期權深度分析 Prompt (包含個股突破、ETF 趨勢、Bull Put 垂直價差最佳商品組合)。
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


def run_analysis(stock_file=None, etf_file=None, bull_put_file=None):
    print("=" * 60)
    print(" Barchart 選擇權異動與價差 AI 分析模組啟動")
    print("=" * 60)

    # 1. 讀取 API Key
    api_key = load_gemini_api_key(ENV_FILE)
    if not api_key:
        print("[ERROR] 無法在 trade/.env 找到 gemini_api 設定")
        sys.exit(1)
    print(f"[INFO] 成功自 trade/.env 載入 gemini_api (長度: {len(api_key)})")

    # 2. 獲取最新 CSV 檔案
    if not stock_file:
        stock_file = get_latest_csv_file("unusual-stock-options-activity-*.csv")
    if not etf_file:
        etf_file = get_latest_csv_file("unusual-etf-options-activity-*.csv")
    if not bull_put_file:
        bull_put_file = get_latest_csv_file("*bull-put*.csv")

    if not stock_file or not os.path.exists(stock_file):
        print(f"[ERROR] 在 {BARCHART_DIR} 找不到個股 CSV 檔案")
        sys.exit(1)

    if not etf_file or not os.path.exists(etf_file):
        print(f"[ERROR] 在 {BARCHART_DIR} 找不到 ETF CSV 檔案")
        sys.exit(1)

    if bull_put_file and os.path.exists(bull_put_file):
        print(f"[INFO] 找到最新 Bull Put Spread CSV: {os.path.basename(bull_put_file)}")
    else:
        print(f"[WARN] 在 {BARCHART_DIR} 未找到 Bull Put Spread CSV 檔案，將僅分析個股與 ETF")
        bull_put_file = None

    # 3. 清洗與篩選數據
    data_str = clean_and_prepare_data(stock_file, etf_file, bull_put_file)

    # 4. 組裝 Prompt
    prompt = build_analysis_prompt(data_str)

    # 5. 呼叫 Gemini
    analysis_text = call_gemini_rest(prompt, api_key)

    # 6. 儲存所有文字為文字檔 (存放至 trade/Barchart/reports/ 供 q.py 網頁看板顯示)
    archive_fname, archive_path = save_analysis_to_text_file(analysis_text)

    # 7. 終端輸出建議
    print("\n" + "=" * 60)
    print("=== 今日 AI 三大投資建議 ===")
    print("=" * 60)
    print(analysis_text)
    print("=" * 60)

    # 8. LINE 推播（發送速覽摘要並引導查看網頁新分頁）
    send_line_notification(analysis_text, archive_fname)


if __name__ == "__main__":
    stock_arg = sys.argv[1] if len(sys.argv) > 1 else None
    etf_arg = sys.argv[2] if len(sys.argv) > 2 else None
    bull_put_arg = sys.argv[3] if len(sys.argv) > 3 else None
    run_analysis(stock_arg, etf_arg, bull_put_arg)
