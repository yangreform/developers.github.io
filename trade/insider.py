#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Barchart Insider Trading & Options Flow Automated Pipeline (trade/insider.py)
================================================================================
參考 trade/barchart_auto.py 與 trade/barchart_placeOrder.py 的架構與設計模式：
  1. 登入 Barchart 並下載最新內部人交易表格 (Insider Trading Activity)：
     - URL: https://www.barchart.com/investing-ideas/insider-trading-activity
  2. 傳入內部人籌碼數據給 Gemini AI，取得深度評估與最好一檔商品的建議：
     - 存入 trade/latest_insider.txt 與歷史報告
     - 摘要推播至手機 LINE
  3. 動態前往該最佳標的的期權大單流向頁面 (Options Flow)：
     - URL: https://www.barchart.com/stocks/quotes/{SYMBOL}/options-flow
     - 下載最新期權大單 CSV 表格
  4. 傳入 Options Flow 數據給 Gemini AI，取得最好一檔 CALL 買權建議：
     - 存入 trade/latest_insider_call.txt 與歷史報告
     - 摘要推播至手機 LINE
  5. 連線 IBKR 查詢該 CALL 合約即時市場限價：
     - 送出 Adaptive Patient 限價買入母單
     - 同時掛出 Attached Orders 附屬單：
       * Profit Taker (停利): 母單限價之 2 倍 (2.0 * LimitPrice)
       * Stop Loss    (停損): 母單限價之 0.5 倍 (0.5 * LimitPrice)
     - 下單結果與附屬價位推播至手機 LINE
================================================================================
"""

import os
import sys
import re
import time
import glob
import math
import random
import datetime
import argparse
import requests
import pandas as pd

# 確保 Windows 主控台正確輸出 UTF-8 字符
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")
BARCHART_DIR = os.path.join(BASE_DIR, "Barchart")
OLD_DIR = os.path.join(BARCHART_DIR, "old")
REPORTS_DIR = os.path.join(BARCHART_DIR, "reports")

os.makedirs(BARCHART_DIR, exist_ok=True)
os.makedirs(OLD_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)

if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# 載入模組函式
try:
    from barchart_download import (
        init_driver,
        dismiss_popups,
        login_if_needed,
        load_credentials_from_env,
        wait_for_file_download,
        PROFILE_DIR,
    )
except ImportError as e:
    print(f"[WARN] 無法自 barchart_download 載入函式: {e}")
    init_driver = None

try:
    from barchart_analysis import load_gemini_api_key, call_gemini_rest
except ImportError as e:
    print(f"[WARN] 無法自 barchart_analysis 載入函式: {e}")
    load_gemini_api_key = None
    call_gemini_rest = None

try:
    from barchart_placeOrder import create_fast_ib_connection, load_env_settings
except ImportError as e:
    print(f"[WARN] 無法自 barchart_placeOrder 載入函式: {e}")
    create_fast_ib_connection = None

try:
    from notifier import send_push_message
except ImportError:
    send_push_message = None

try:
    from ib_insync import IB, Option, LimitOrder, StopOrder, TagValue
except ImportError:
    IB = None


# ==============================================================================
# 1. 網頁自動化：下載 Insider Trading Activity 與 Options Flow CSV
# ==============================================================================
def download_insider_activity_csv(driver, target_dir=BARCHART_DIR):
    """
    導航至 https://www.barchart.com/investing-ideas/insider-trading-activity
    點擊下載按鈕下載官方 CSV。
    """
    from selenium.webdriver.common.by import By

    url = "https://www.barchart.com/investing-ideas/insider-trading-activity"
    print(f"\n[INFO] 正在導航至內部人交易頁面: {url} ...")
    driver.get(url)
    time.sleep(6)
    dismiss_popups(driver)

    before_snapshot = set(glob.glob(os.path.join(target_dir, "*.csv")))
    dl_buttons = driver.find_elements(By.CSS_SELECTOR, "a.toolbar-button.download, [data-bc-download-button], button.download")

    downloaded_file = None
    if dl_buttons:
        btn = dl_buttons[0]
        print(f"[INFO] 找到下載按鈕 (text='{btn.text.strip()}'), 點擊下載內部人交易 CSV ...")
        driver.execute_script("arguments[0].click();", btn)
        downloaded_file = wait_for_file_download(target_dir, before_snapshot, timeout=18)

    if not downloaded_file or not os.path.exists(downloaded_file):
        # 尋找現存最新檔案
        candidates = glob.glob(os.path.join(target_dir, "*insider*.csv"))
        if candidates:
            candidates.sort(key=os.path.getmtime, reverse=True)
            downloaded_file = candidates[0]
            print(f"[INFO] 沿用現存內部人交易 CSV: {downloaded_file}")

    if downloaded_file and os.path.exists(downloaded_file):
        print(f"[SUCCESS] ✅ 內部人交易 CSV 準備就緒: {downloaded_file} (大小: {os.path.getsize(downloaded_file):,} 位元組)")
        return downloaded_file

    raise RuntimeError("無法成功下載或取得 Insider Trading Activity CSV 表格。")


def download_options_flow_csv(driver, symbol, target_dir=BARCHART_DIR):
    """
    導航至 https://www.barchart.com/stocks/quotes/{symbol}/options-flow
    點擊下載按鈕下載期權大單流向 CSV。
    """
    from selenium.webdriver.common.by import By

    sym_clean = symbol.strip().upper()
    url = f"https://www.barchart.com/stocks/quotes/{sym_clean}/options-flow"
    print(f"\n[INFO] 正在導航至 {sym_clean} 期權大單流向頁面: {url} ...")
    driver.get(url)
    time.sleep(6)
    dismiss_popups(driver)

    before_snapshot = set(glob.glob(os.path.join(target_dir, "*.csv")))
    dl_buttons = driver.find_elements(By.CSS_SELECTOR, "a.toolbar-button.download, [data-bc-download-button], button.download")

    downloaded_file = None
    if dl_buttons:
        btn = dl_buttons[0]
        print(f"[INFO] 找到下載按鈕 (text='{btn.text.strip()}'), 點擊下載 {sym_clean} Options Flow CSV ...")
        driver.execute_script("arguments[0].click();", btn)
        downloaded_file = wait_for_file_download(target_dir, before_snapshot, timeout=18)

    if not downloaded_file or not os.path.exists(downloaded_file):
        candidates = glob.glob(os.path.join(target_dir, f"*{sym_clean.lower()}*options-flow*.csv"))
        if not candidates:
            candidates = glob.glob(os.path.join(target_dir, f"*options-flow*.csv"))
        if candidates:
            candidates.sort(key=os.path.getmtime, reverse=True)
            downloaded_file = candidates[0]
            print(f"[INFO] 沿用現存 Options Flow CSV: {downloaded_file}")

    if downloaded_file and os.path.exists(downloaded_file):
        print(f"[SUCCESS] ✅ {sym_clean} Options Flow CSV 準備就緒: {downloaded_file} (大小: {os.path.getsize(downloaded_file):,} 位元組)")
        return downloaded_file

    raise RuntimeError(f"無法成功下載或取得 {sym_clean} Options Flow CSV 表格。")


# ==============================================================================
# 2. 內部人交易數據清洗與 Gemini 提示詞構建
# ==============================================================================
def clean_and_prepare_insider_data(csv_path, top_buys_count=40, top_volume_count=25):
    """
    清洗內部人交易數據，聚合個股統計資訊，輸出給 Gemini 進行量化篩選。
    """
    print(f"[INFO] 讀取內部人交易數據: {os.path.basename(csv_path)}")
    df = pd.read_csv(csv_path)

    # 移除 footer 行
    df = df[df["Symbol"].notna() & (~df["Symbol"].astype(str).str.contains("Downloaded", case=False, na=False))]

    # 數值轉換
    df["Shares"] = pd.to_numeric(df["Shares"].astype(str).str.replace(",", ""), errors="coerce")
    df["@Price"] = pd.to_numeric(df["@Price"].astype(str).str.replace(",", "").str.replace("$", ""), errors="coerce")
    df["Trans Total"] = pd.to_numeric(df["Trans Total"].astype(str).str.replace(",", "").str.replace("$", ""), errors="coerce")
    df["Shares After"] = pd.to_numeric(df["Shares After"].astype(str).str.replace(",", ""), errors="coerce")

    # 篩選真實買盤 (Buy, Contract Buy)
    buys_mask = df["Transaction"].astype(str).str.upper().str.contains("BUY")
    df_buys = df[buys_mask].sort_values(by="Trans Total", ascending=False)

    # 篩選賣盤 (Sell, Sale Post-exercise)
    sells_mask = df["Transaction"].astype(str).str.upper().str.contains("SELL|SALE")
    df_sells = df[sells_mask].sort_values(by="Trans Total", ascending=False)

    # 聚合標的購買總額與賣出總額
    buy_agg = df[buys_mask].groupby("Symbol").agg(
        Buy_Total=("Trans Total", "sum"),
        Buy_Shares=("Shares", "sum"),
        Buy_Trades=("Transaction", "count"),
        Company_Name=("Name", "first"),
        Insiders=("Insider Name", lambda s: ", ".join(s.dropna().unique()[:3])),
        Titles=("Title", lambda s: ", ".join(s.dropna().unique()[:3]))
    ).reset_index()

    sell_agg = df[sells_mask].groupby("Symbol").agg(
        Sell_Total=("Trans Total", "sum"),
        Sell_Trades=("Transaction", "count")
    ).reset_index()

    merged = pd.merge(buy_agg, sell_agg, on="Symbol", how="left").fillna(0)
    merged["Net_Buy_Total"] = merged["Buy_Total"] - merged["Sell_Total"]
    merged = merged.sort_values(by="Buy_Total", ascending=False)

    top_summary = merged.head(top_volume_count)
    top_buys_detail = df_buys.head(top_buys_count)[
        ["Symbol", "Name", "Insider Name", "Title", "Transaction", "Shares", "@Price", "Trans Total", "Date"]
    ]

    print(f"[INFO] 內部人交易清洗完成：買盤交易 {len(df_buys)} 筆，淨買入標的 {len(merged)} 檔")

    summary_text = f"""【標的內部人淨買入總額排行榜 (Top {len(top_summary)})】
{top_summary.to_string(index=False)}

【頂尖單筆內部人買入明細 (Top {len(top_buys_detail)})】
{top_buys_detail.to_string(index=False)}
"""
    return summary_text, top_summary


def build_insider_prompt(data_summary_text):
    prompt = f"""你是一名華爾街頂級股權籌碼與內部人交易（Insider Trading）量化研究主管。
以下是 Barchart 提供的最新全市場內部人交易活動（Insider Trading Activity）清洗與聚合數據：

{data_summary_text}

【分析要求與評估維度】
1. **內部人真金白銀持股信念 (Conviction)**：
   - 聚焦高階主管（CEO、CFO、董事會主席、核心董事、10% 大股東）直接在公開市場斥資「Buy / Contract Buy」的行為，過濾掉單純期權行權轉出（Transfer Out/Sale Post-exercise）。
   - 買入金額越大、多位內部人協同買入（Cluster Buying）或相較於過往持股顯著倍增者為最核心加分項。
2. **基本面與產業景氣循環**：
   - 評估該標的是否處於產業轉折點、AI/伺服器科技循環、景氣防禦、或價值重估階段。
3. **選出「唯一最好一檔商品」**：
   - 綜合以上內部人掃貨力道與勝率，在所有候選標的中挑選出最具上漲爆發力、機構跟進意願最高、性價比最佳的「唯一一檔商品」。

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


# ==============================================================================
# 3. 期權大單流向 (Options Flow) 數據清洗與 Gemini 提示詞構建
# ==============================================================================
def clean_and_prepare_options_flow(csv_path, symbol, top_count=40):
    """
    清洗目標股票的 Options Flow 數據，專注於 CALL 買權大單，過濾 0DTE 雜訊。
    """
    print(f"[INFO] 讀取 {symbol} Options Flow 數據: {os.path.basename(csv_path)}")
    df = pd.read_csv(csv_path)

    # 移除 footer 行
    df = df[df["Symbol"].notna() & (~df["Symbol"].astype(str).str.contains("Downloaded", case=False, na=False))]

    # 數值型態清理
    numeric_cols = ["Price~", "Strike", "DTE", "Trade", "Size", "Premium", "Volume", "Open Int", "Delta"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace(",", "").str.replace("$", ""), errors="coerce")

    # 專注於 CALL 買權
    df_call = df[df["Type"].astype(str).str.upper() == "CALL"].copy()

    # 過濾 0DTE (建議 DTE >= 7，保留波段持倉時間；若不足則保留 DTE >= 3)
    df_call_filtered = df_call[df_call["DTE"] >= 7]
    if df_call_filtered.empty:
        df_call_filtered = df_call[df_call["DTE"] >= 1]
    if df_call_filtered.empty:
        df_call_filtered = df_call

    # 依成交權利金 (Premium) 降序排序
    if "Premium" in df_call_filtered.columns:
        df_call_filtered = df_call_filtered.sort_values(by="Premium", ascending=False)

    top_calls = df_call_filtered.head(top_count)
    print(f"[INFO] {symbol} Options Flow 清洗完成：Call 交易 {len(df_call)} 筆，篩選最具代表性大單 {len(top_calls)} 筆")

    cols_to_show = [c for c in ["Symbol", "Price~", "Exp Date", "Type", "Strike", "DTE", "Trade", "Size", "Side", "Premium", "Volume", "Open Int", "IV", "Delta", "Time"] if c in top_calls.columns]
    data_str = top_calls[cols_to_show].to_string(index=False)
    return data_str, top_calls


def build_options_flow_prompt(symbol, flow_data_str):
    prompt = f"""你是一名華爾街期權造市商與主力大單（Whale Options Flow）量化專家。
我們已透過內部人交易（Insider Trading）篩選出最強標的：{symbol}。
現在請針對該標的在市場上的最新期權大單流向（Options Flow）進行深度解讀：

【{symbol} 期權大單流向 (CALL 買權清單)】
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
- **推薦標的代號 (Symbol)**: {symbol}
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


# ==============================================================================
# 4. 解析與存檔輔助函式
# ==============================================================================
def parse_symbol_from_insider_report(report_text):
    """
    自內部人報告中解析推薦之標的代號
    """
    patterns = [
        r'推薦標的代號[^\n:]*[：:]\s*\*?([A-Za-z0-9]+)',
        r'Symbol[^\n:]*[：:]\s*\*?([A-Za-z0-9]+)',
        r'標的代號[^\n:]*[：:]\s*\*?([A-Za-z0-9]+)',
    ]
    for p in patterns:
        m = re.search(p, report_text, re.IGNORECASE)
        if m:
            sym = m.group(1).strip().upper()
            if sym not in ("BUY", "CALL", "PUT", "STOCK", "NONE", "N/A", "SYMBOL"):
                return sym
    return None


def parse_call_contract_from_report(report_text, default_symbol="DELL"):
    """
    自期權報告中解析：symbol, strike, exp_date (YYYYMMDD), ref_price
    """
    # 標的
    patterns = [
        r'推薦標的代號[^\n:]*[：:]\s*\*?([A-Za-z0-9]+)',
        r'Symbol[^\n:]*[：:]\s*\*?([A-Za-z0-9]+)',
        r'標的代號[^\n:]*[：:]\s*\*?([A-Za-z0-9]+)',
    ]
    symbol = default_symbol
    for p in patterns:
        m = re.search(p, report_text, re.IGNORECASE)
        if m:
            s = m.group(1).strip().upper()
            if s not in ("BUY", "CALL", "PUT", "STOCK", "NONE", "N/A", "SYMBOL"):
                symbol = s
                break

    # 履約價
    strike_match = re.search(r'履約價[^\n:]*[：:]\s*\*?\$?(\d+\.?\d*)', report_text)
    strike = float(strike_match.group(1)) if strike_match else None

    # 到期日
    exp_match = re.search(r'到期日[^\d]*(\d{4}[-/]\d{2}[-/]\d{2})', report_text)
    exp_date = exp_match.group(1).replace("-", "").replace("/", "") if exp_match else None

    # 參考價格
    ref_match = re.search(r'(?:參考價格|權利金)[^\$\d\n]*\$?(\d+\.?\d*)', report_text, re.IGNORECASE)
    if not ref_match:
        ref_match = re.search(r'Trade[^\$\d\n]*\$?(\d+\.?\d*)', report_text, re.IGNORECASE)
    ref_price = float(ref_match.group(1)) if ref_match else 1.0

    return {
        "symbol": symbol,
        "strike": strike,
        "exp_date": exp_date,
        "ref_price": ref_price,
        "strategy": "Buy Call",
        "right": "C",
        "action": "BUY",
    }


def save_report_and_notify(report_text, filename_prefix, line_title):
    """
    儲存分析報告並推播至 LINE
    """
    now = datetime.datetime.now()
    ts_str = now.strftime("%Y-%m-%d_%H%M%S")
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    archive_fname = f"{filename_prefix}_{ts_str}.txt"
    archive_path = os.path.join(REPORTS_DIR, archive_fname)
    latest_path = os.path.join(BASE_DIR, f"{filename_prefix}.txt")
    root_latest_path = os.path.join(BARCHART_DIR, f"{filename_prefix}.txt")

    content = f"==分析時間：{now_str}==\n{report_text}\n"

    with open(archive_path, "w", encoding="utf-8") as f:
        f.write(content)
    with open(latest_path, "w", encoding="utf-8") as f:
        f.write(content)
    with open(root_latest_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"[INFO] 報告已存檔至：{archive_path}")
    print(f"[INFO] 最新文字檔已同步至：{latest_path}")

    # 組裝推播訊息 (前 1,500 字元或重點行)
    lines = report_text.strip().split("\n")
    summary_lines = []
    keywords = ["【", "📌", "推薦", "代號", "履約價", "到期日", "金額", "理由", "增持", "均價", "主力", "權利金", "Profit", "Stop", "Symbol"]
    for l in lines:
        if any(kw in l for kw in keywords):
            summary_lines.append(l)

    summary_block = "\n".join(summary_lines[:15]) if summary_lines else report_text[:800]

    line_msg = f"""📊【{line_title}】
🕒 時間：{now_str}

{summary_block}

📁 完整報告：{archive_fname}"""

    if send_push_message:
        print(f"[INFO] 正在推播 {line_title} 訊息至手機 LINE...")
        ok = send_push_message(line_msg.strip())
        if ok:
            print(f"[SUCCESS] ✅ {line_title} LINE 推播成功！")
        else:
            print(f"[WARN] ⚠️ LINE 推播異常。")

    return archive_path


# ==============================================================================
# 5. IBKR 下單：預查現價 + Adaptive Patient + 2倍停利 / 一半停損
# ==============================================================================
def execute_ibkr_call_order(contract_info, dry_run=False):
    """
    連線 IBKR，建立 Call 期權合約，預查即時市價，
    送出 Adaptive Patient 限價單，並掛出 Attached 訂單：
      - Profit Taker: 2倍限價 (2.0 * limit_price)
      - Stop Loss:    一半限價 (0.5 * limit_price)
    下單完成後推播結果至手機 LINE。
    """
    symbol = contract_info["symbol"]
    strike = contract_info["strike"]
    exp_date = contract_info["exp_date"]
    ref_price = contract_info.get("ref_price", 1.0)

    print("\n" + "=" * 65)
    print(f"🚀 【IBKR 智能下單模組】執行 {symbol} CALL 買入委託")
    print("=" * 65)

    if not strike or not exp_date:
        err = f"❌ [合約參數缺失] 無法下單：Strike={strike}, ExpDate={exp_date}"
        print(err)
        return {"status": "error", "message": err}

    cfg = load_env_settings(ENV_FILE)
    host = cfg.get("IB_HOST", "127.0.0.1")
    port = cfg.get("IB_PORT", 4001)
    target_account = cfg.get("IB_TARGET_ACCOUNT", "")

    ib = None
    try:
        ib = create_fast_ib_connection(host=host, port=port, client_id=random.randint(9700, 9799))

        contract = Option(symbol, exp_date, strike, "C", "SMART")
        qualified = ib.qualifyContracts(contract)
        if not qualified or not contract.conId:
            err = f"❌ [合約無效] 無法在 IBKR 驗證合約: {symbol} {exp_date} C{strike}"
            print(err)
            return {"status": "error", "message": err}

        print(f"  -> 合約驗證成功: {contract.localSymbol} (conId: {contract.conId})")

        # 預查即時市場報價
        ticker = ib.reqMktData(contract, "", False, False)
        ib.sleep(2.5)

        current_price = 0.0
        if ticker.ask and not math.isnan(ticker.ask) and ticker.ask > 0:
            current_price = ticker.ask
        elif ticker.marketPrice() and not math.isnan(ticker.marketPrice()) and ticker.marketPrice() > 0:
            current_price = ticker.marketPrice()
        elif ticker.bid and not math.isnan(ticker.bid) and ticker.bid > 0:
            current_price = ticker.bid
        elif ticker.close and not math.isnan(ticker.close) and ticker.close > 0:
            current_price = ticker.close
        else:
            current_price = ref_price

        current_price = max(0.05, round(current_price, 2))
        print(f"  -> 預查即時限價成功: ${current_price:.2f} (即時Bid: {ticker.bid}, Ask: {ticker.ask}, 參考價: {ref_price})")

        # 計算 Attached Order 價格：
        # 規則：
        # BUY CALL: Profit Taker 現價二倍 (2.0 * 現價)，Stop Loss 現價一半 (0.5 * 現價)
        take_profit_price = round(current_price * 2.0, 2)
        stop_loss_price = max(0.01, round(current_price * 0.5, 2))

        print(f"  -> 附屬單規劃:")
        print(f"     • 🎯 Profit Taker (2倍停利單): ${take_profit_price:.2f}")
        print(f"     • 🛑 Stop Loss    (一半停損單): ${stop_loss_price:.2f}")

        # 組裝 Bracket Order
        bracket = ib.bracketOrder(
            action="BUY",
            quantity=1,
            limitPrice=current_price,
            takeProfitPrice=take_profit_price,
            stopLossPrice=stop_loss_price,
        )

        # 母單套用 Adaptive Patient 演算法
        bracket.parent.algoStrategy = "Adaptive"
        bracket.parent.algoParams = [TagValue("adaptivePriority", "Patient")]
        bracket.parent.tif = "DAY"
        bracket.takeProfit.tif = "GTC"
        bracket.stopLoss.tif = "GTC"

        if target_account:
            bracket.parent.account = target_account
            bracket.takeProfit.account = target_account
            bracket.stopLoss.account = target_account

        order_list = [bracket.parent, bracket.takeProfit, bracket.stopLoss]
        desc = (
            f"Buy Call {contract.localSymbol}\n"
            f"     委託: BUY 1口 @ 限價 ${current_price:.2f} (Adaptive Patient)\n"
            f"     🎯 停利 (2倍): ${take_profit_price:.2f} | 🛑 停損 (一半): ${stop_loss_price:.2f}"
        )

        if not dry_run:
            for o in order_list:
                ib.placeOrder(contract, o)
            ib.sleep(1)
            status = bracket.parent.orderStatus.status if hasattr(bracket.parent, "orderStatus") else "Submitted"
            print(f"  -> ✅ [已送出委託至 IBKR] 母單狀態: {status}")
        else:
            print(f"  -> 🔍 [模擬模式 (Dry-Run)] 不實際送單至交易所")
            status = "DryRun"

        # 推播下單詳情報告至 LINE
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line_msg = f"""🚀【IBKR 內部人期權自動下單回報】
🕒 時間：{now_str}
⚙️ 模式：{'模擬測試 (Dry-Run)' if dry_run else '正式送單 (Live)'}
📋 合約：{contract.localSymbol} (conId: {contract.conId})
💵 即時限價：${current_price:.2f}
🎯 停利單 (2倍)：${take_profit_price:.2f}
🛑 停損單 (一半)：${stop_loss_price:.2f}
⚡ 演算法：Adaptive Patient
📊 委託狀態：{status}"""

        if send_push_message:
            print(f"[INFO] 正在推播下單詳情至手機 LINE...")
            ok = send_push_message(line_msg.strip())
            if ok:
                print(f"[SUCCESS] ✅ 下單回報 LINE 推播成功！")
            else:
                print(f"[WARN] ⚠️ LINE 推播異常。")

        return {
            "status": "ok",
            "symbol": symbol,
            "desc": desc,
            "limit_price": current_price,
            "take_profit": take_profit_price,
            "stop_loss": stop_loss_price,
            "order_status": status,
        }

    except Exception as e:
        print(f"[ERROR] 執行 IBKR 下單異常: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        if ib and ib.isConnected():
            ib.disconnect()
            print("[INFO] 已安全斷開 IBKR 連線。")


# ==============================================================================
# 6. 主控排程入口 (Pipeline Orchestrator)
# ==============================================================================
def run_insider_pipeline(
    headless=False,
    dry_run=False,
    skip_download=False,
    skip_order=False,
    symbol_override=None,
    retries=3,
):
    """
    完整執行內部人交易與期權大單自動化五步驟流程
    """
    start_time = time.time()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("\n" + "=" * 65)
    print(f"🌟 Barchart 內部人交易 & 期權大單自動化系統啟動 ({now_str})")
    print("=" * 65)

    api_key = load_gemini_api_key(ENV_FILE)
    if not api_key:
        raise ValueError(f"無法在 {ENV_FILE} 中找到有效的 gemini_api 金鑰！")

    driver = None
    insider_csv = None
    options_flow_csv = None
    selected_symbol = symbol_override

    try:
        # 步驟 1: 下載內部人交易數據
        if not skip_download:
            account, password = load_credentials_from_env(ENV_FILE)
            driver = init_driver(download_dir=BARCHART_DIR, profile_dir=PROFILE_DIR, headless=headless)
            login_if_needed(driver, account, password)
            insider_csv = download_insider_activity_csv(driver, target_dir=BARCHART_DIR)
        else:
            candidates = glob.glob(os.path.join(BARCHART_DIR, "*insider*.csv"))
            if candidates:
                candidates.sort(key=os.path.getmtime, reverse=True)
                insider_csv = candidates[0]
                print(f"[INFO] (跳過下載) 使用現存內部人交易 CSV: {insider_csv}")
            else:
                raise FileNotFoundError("未找到任何現存的內部人交易 CSV 檔案。")

        # 步驟 2: Gemini 內部人籌碼分析，選出最佳個股
        print("\n" + "=" * 65)
        print("【步驟 2】Gemini AI 內部人持股分析與最佳個股挑選")
        print("=" * 65)

        insider_summary_text, top_summary = clean_and_prepare_insider_data(insider_csv)
        insider_prompt = build_insider_prompt(insider_summary_text)

        insider_report = None
        for attempt in range(1, retries + 1):
            try:
                print(f"[INFO] 正在向 Gemini 請求內部人交易分析 (第 {attempt}/{retries} 次)...")
                insider_report = call_gemini_rest(insider_prompt, api_key)
                if insider_report and len(insider_report.strip()) > 100:
                    break
            except Exception as e:
                print(f"[WARN] 第 {attempt} 次請求失敗: {e}")
                time.sleep(2)

        if not insider_report:
            raise RuntimeError("無法自 Gemini 取得內部人交易分析報告。")

        save_report_and_notify(insider_report, "latest_insider", "Barchart 內部人交易 AI 最佳推薦")

        # 解析最優標的代號
        if not selected_symbol:
            selected_symbol = parse_symbol_from_insider_report(insider_report)
        if not selected_symbol:
            # Fallback to top insider buy symbol
            if not top_summary.empty and "Symbol" in top_summary.columns:
                selected_symbol = str(top_summary.iloc[0]["Symbol"]).strip().upper()
                print(f"[WARN] 無法從文字解析標的，自動採用淨買入第一名: {selected_symbol}")
            else:
                selected_symbol = "DELL"
                print(f"[WARN] 採用預設標的: {selected_symbol}")

        print(f"\n🎯 【確認選定標的代號】: {selected_symbol}")

        # 步驟 3: 下載該標的之 Options Flow 數據
        if not skip_download:
            if not driver:
                account, password = load_credentials_from_env(ENV_FILE)
                driver = init_driver(download_dir=BARCHART_DIR, profile_dir=PROFILE_DIR, headless=headless)
                login_if_needed(driver, account, password)
            options_flow_csv = download_options_flow_csv(driver, selected_symbol, target_dir=BARCHART_DIR)
        else:
            candidates = glob.glob(os.path.join(BARCHART_DIR, f"*{selected_symbol.lower()}*options-flow*.csv"))
            if not candidates:
                candidates = glob.glob(os.path.join(BARCHART_DIR, f"*options-flow*.csv"))
            if candidates:
                candidates.sort(key=os.path.getmtime, reverse=True)
                options_flow_csv = candidates[0]
                print(f"[INFO] (跳過下載) 使用現存 Options Flow CSV: {options_flow_csv}")
            else:
                raise FileNotFoundError(f"未找到任何現存的 {selected_symbol} Options Flow CSV 檔案。")

        # 關閉瀏覽器
        if driver:
            try:
                driver.quit()
                driver = None
            except Exception:
                pass

        # 步驟 4: Gemini 期權大單流向分析，選出最佳 CALL
        print("\n" + "=" * 65)
        print(f"【步驟 4】Gemini AI 解讀 {selected_symbol} Options Flow 並挑選最佳 CALL")
        print("=" * 65)

        flow_data_str, top_calls = clean_and_prepare_options_flow(options_flow_csv, selected_symbol)
        flow_prompt = build_options_flow_prompt(selected_symbol, flow_data_str)

        call_report = None
        for attempt in range(1, retries + 1):
            try:
                print(f"[INFO] 正在向 Gemini 請求 {selected_symbol} 期權大單分析 (第 {attempt}/{retries} 次)...")
                call_report = call_gemini_rest(flow_prompt, api_key)
                if call_report and len(call_report.strip()) > 100:
                    break
            except Exception as e:
                print(f"[WARN] 第 {attempt} 次請求失敗: {e}")
                time.sleep(2)

        if not call_report:
            raise RuntimeError(f"無法自 Gemini 取得 {selected_symbol} Options Flow 分析報告。")

        save_report_and_notify(call_report, "latest_insider_call", f"{selected_symbol} 期權大單 AI 最佳 CALL 推薦")

        # 步驟 5: 解析 CALL 規格並下單 IBKR
        contract_info = parse_call_contract_from_report(call_report, default_symbol=selected_symbol)
        print(f"[INFO] 解析期權合約規格: 標的={contract_info['symbol']}, 履約價={contract_info['strike']}, 到期日={contract_info['exp_date']}, 參考價={contract_info['ref_price']}")

        if not skip_order:
            order_res = execute_ibkr_call_order(contract_info, dry_run=dry_run)
            print(f"[INFO] 下單處理結果: {order_res.get('status')} - {order_res.get('desc', order_res.get('message'))}")
        else:
            print("[INFO] 已略過 IBKR 下單步驟 (--skip-order)")

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    elapsed = time.time() - start_time
    print("\n" + "#" * 65)
    print(f"# ✅ Barchart 內部人 & 期權大單自動化流程完成！(總耗時: {elapsed:.1f} 秒)")
    print("#" * 65 + "\n")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Barchart 內部人交易與期權大單 AI 自動化選股與下單流水線")
    parser.add_argument("--headless", action="store_true", help="以無瀏覽器視窗模式執行")
    parser.add_argument("--dry-run", action="store_true", help="以模擬模式執行下單（預查市價並計算附屬單，不實際送單至 IBKR）")
    parser.add_argument("--skip-download", action="store_true", help="跳過下載步驟，使用現存 CSV")
    parser.add_argument("--skip-order", action="store_true", help="跳過向 IBKR 下單步驟")
    parser.add_argument("--symbol", type=str, default=None, help="手動指定分析標的（覆蓋 Gemini 自內部人選出之標的）")
    parser.add_argument("--retries", type=int, default=3, help="Gemini 請求最大重試次數")
    args = parser.parse_args()

    run_insider_pipeline(
        headless=args.headless,
        dry_run=args.dry_run,
        skip_download=args.skip_download,
        skip_order=args.skip_order,
        symbol_override=args.symbol,
        retries=args.retries,
    )
