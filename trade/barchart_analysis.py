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
    Find the newest CSV matching pattern in directory.
    """
    matches = glob.glob(os.path.join(directory, pattern))
    if not matches:
        return None
    matches.sort(key=os.path.getmtime, reverse=True)
    return matches[0]


def clean_and_prepare_data(stock_csv, etf_csv, top_n_stocks=60, top_n_etfs=40):
    """
    Read, clean, and filter CSV data using Pandas:
      - Filter 7 <= DTE <= 120 (removes 0DTE noise)
      - Sort by Vol/OI ratio descending
      - Keep top N highest-conviction signals for prompt efficiency
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

    print(f"[INFO] 清洗完成：個股篩選 {len(df_stock_filtered)} 筆，ETF 篩選 {len(df_etf_filtered)} 筆")

    data_str = "【個股異常期權數據】\n" + df_stock_filtered.to_csv(index=False) + \
               "\n\n【ETF異常期權數據】\n" + df_etf_filtered.to_csv(index=False)
    return data_str


def call_gemini_rest(prompt, api_key):
    """
    Direct REST API call with model fallback chain.
    """
    candidate_models = ["gemini-3.5-flash", "gemini-3.6-flash", "gemini-3-flash-preview"]

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
                "maxOutputTokens": 4096
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

    file_content = f"""============================================================
Barchart 選擇權異動 AI 投資建議 (Smart Money 深度分析)
分析時間：{now_readable}
============================================================

{analysis_text}
"""

    with open(archive_path, "w", encoding="utf-8") as f:
        f.write(file_content)

    with open(latest_path, "w", encoding="utf-8") as f:
        f.write(file_content)

    with open(root_latest_path, "w", encoding="utf-8") as f:
        f.write(file_content)

    print(f"[INFO] 完整文字檔已成功存檔至：{archive_path}")
    print(f"[INFO] 最新文字檔已同步至：{latest_path}")
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
    for line in analysis_text.split("\n"):
        clean_l = line.strip()
        if not clean_l:
            continue
        if any(keyword in clean_l for keyword in ["1.", "2.", "3.", "4.", "首選", "標的", "Buy", "Call", "Put", "Strangle", "雙賣"]):
            summary_lines.append(clean_l.replace("*", ""))
        if len(summary_lines) >= 8:
            break

    summary_block = "\n".join(summary_lines) if summary_lines else analysis_text[:400]

    line_msg = f"""📊【Barchart 選擇權異動 AI 投資建議】
🕒 分析時間：{now_str}
📁 存檔檔名：{archive_filename or 'latest_ai_analysis.txt'}

🎯 核心重點速覽：
{summary_block}

💡 完整華爾街量化推演與期權籌碼分析已存成文字檔，請開啟交易看板【AI 投資建議】分頁查看完整內容！"""

    print(f"[INFO] 正在推播 LINE 摘要訊息到手機...")
    ok = send_push_message(line_msg.strip())
    if ok:
        print("[SUCCESS] LINE 摘要訊息發送成功！")
    else:
        print("[WARN] LINE 發送過程有異常，請檢查 Token。")
    return ok


def run_analysis(stock_file=None, etf_file=None):
    print("=" * 60)
    print(" Barchart 選擇權異動 AI 分析模組啟動")
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

    if not stock_file or not os.path.exists(stock_file):
        print(f"[ERROR] 在 {BARCHART_DIR} 找不到個股 CSV 檔案")
        sys.exit(1)

    if not etf_file or not os.path.exists(etf_file):
        print(f"[ERROR] 在 {BARCHART_DIR} 找不到 ETF CSV 檔案")
        sys.exit(1)

    # 3. 清洗與篩選數據
    data_str = clean_and_prepare_data(stock_file, etf_file)

    # 4. 組裝 Prompt
    prompt = f"""你是一位頂級的華爾街量化分析師，擅長追蹤 Smart Money (聰明錢) 動向。
請根據以下我提供的 Barchart 異常選擇權 (UOA) 數據，進行以下任務：
1. 幫我找出一個最適合做 Buy Call 或 Buy Put 突破的個股。
2. 幫我找出一個最適合做 Buy Call 或 Buy Put 突破的 ETF。
3. 說明挑選理由 (考量 Vol/OI 倍數、Delta、籌碼支撐壓力)。
4. 如果有適合雙賣收租 (Short Strangle) 的標的，請額外提出。

數據如下：
{data_str}
"""

    # 5. 呼叫 Gemini
    analysis_text = call_gemini_rest(prompt, api_key)

    # 6. 儲存所有文字為文字檔 (存放至 trade/Barchart/reports/ 供 q.py 網頁看板顯示)
    archive_fname, archive_path = save_analysis_to_text_file(analysis_text)

    # 7. 終端輸出建議
    print("\n" + "=" * 60)
    print("=== 今日 AI 投資建議 ===")
    print("=" * 60)
    print(analysis_text)
    print("=" * 60)

    # 8. LINE 推播（發送速覽摘要並引導查看網頁新分頁）
    send_line_notification(analysis_text, archive_fname)


if __name__ == "__main__":
    stock_arg = sys.argv[1] if len(sys.argv) > 1 else None
    etf_arg = sys.argv[2] if len(sys.argv) > 2 else None
    run_analysis(stock_arg, etf_arg)
