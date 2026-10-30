#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Barchart Bull Put Spread Pipeline with Skills Architecture (trade/bull-put.py)
================================================================================
本模組為 Bull Put 垂直價差高階管線調度器，導入模組化 Agentic Skills 設計：
  (1) Download Skill (trade/skills/download_skill/):
      - 自動連線 Barchart 下載官方 Bull Put 垂直價差篩選數據：
        • URL: https://www.barchart.com/options/vertical-spreads/bull-put-spread?popularScreener=221982&setPopularScreener=true&orderBy=lossProbability&orderDir=asc
        • Target Folder: trade/Barchart/
  (2) NVIDIA Helper (trade/skills/nv_helper.py):
      - 清洗價差數據、過濾已到期合約、按跌破機率與報酬率排序
      - 調用 NVIDIA DeepSeek AI 深度量化分析，精選唯一最佳 Bull Put Spread 組合
      - 儲存詳細分析報告並推播至手機 LINE
  (3) Scale In Order Skill (trade/skills/scale_in_order_skill/):
      - 連線 IBKR 建立下翼賣權 (Short Put) 與保護賣權 (Long Put) 之 BAG 組合單
      - 當執行加碼委託 (SELL) 時若遭遇 IBKR Error 201:
        "Cannot have open orders on both sides of the same US Option contract."
        自動依序執行 4 大重組步驟：撤銷衝突舊單 -> 送出加碼母單 -> 合併新舊口數 -> 重掛總口數 OCA 括號單
================================================================================
"""

import os
import sys
import glob
import time
import math
import re
import datetime
import argparse
import logging
import pandas as pd

logger = logging.getLogger("BullPutSkill")

# 確保 Windows 主控台正確輸出 UTF-8 字符
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
for p in [BASE_DIR, PROJECT_ROOT]:
    if p not in sys.path:
        sys.path.insert(0, p)

ENV_FILE = os.path.join(BASE_DIR, ".env")
BARCHART_DIR = os.path.join(BASE_DIR, "Barchart")
OLD_DIR = os.path.join(BARCHART_DIR, "old")
REPORTS_DIR = os.path.join(BARCHART_DIR, "reports")

os.makedirs(BARCHART_DIR, exist_ok=True)
os.makedirs(OLD_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)

# 載入模組化 Skills
try:
    from trade.skills import (
        init_driver,
        dismiss_popups,
        login_if_needed,
        load_credentials_from_env,
        download_bull_put_csv,
        PROFILE_DIR,
        ScaleInOrderSkill,
        load_env_settings,
        create_fast_ib_connection,
        load_nv_api_key,
        call_nv_for_skill,
    )
except ImportError:
    from skills.download_skill import (
        init_driver,
        dismiss_popups,
        login_if_needed,
        load_credentials_from_env,
        download_bull_put_csv,
        PROFILE_DIR,
    )
    from skills.scale_in_order_skill import ScaleInOrderSkill
    from skills.ibkr_skill import load_env_settings, create_fast_ib_connection
    from skills.nv_helper import load_nv_api_key, call_nv_for_skill

try:
    from ib_insync import IB, Option, Contract, ComboLeg, Order, OrderComboLeg, TagValue
except ImportError:
    IB = None
    Option = None
    Contract = None
    ComboLeg = None
    Order = None
    OrderComboLeg = None
    TagValue = None

try:
    from notifier import send_push_message
except ImportError:
    send_push_message = None


def normalize_exp_date(date_val):
    """將到期日轉換為 YYYYMMDD 格式"""
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


def format_exp_date_display(exp_date_yyyymmdd):
    """將 YYYYMMDD 格式化為 YYYY-MM-DD"""
    s = str(exp_date_yyyymmdd).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


# ==============================================================================
# 1. Bull Put Spread 數據清洗與 Gemini 深度分析
# ==============================================================================
def clean_and_prepare_bull_put_data(csv_path, top_count=35):
    """
    清洗 Bull Put 數據：
    【步驟 2】強制將最新下載路徑傳入清洗模組，避免讀取舊暫存或歷史檔案污染。
    """
    target_file = None
    if csv_path and os.path.exists(csv_path):
        target_file = csv_path
    else:
        candidates = glob.glob(os.path.join(BARCHART_DIR, "*bull-put*.csv"))
        if not candidates:
            candidates = glob.glob(os.path.join(OLD_DIR, "*bull-put*.csv"))
        if candidates:
            candidates.sort(key=os.path.getmtime, reverse=True)
            target_file = candidates[0]

    if not target_file or not os.path.exists(target_file) or os.path.getsize(target_file) < 350:
        raise ValueError(f"未找到有效之最新 Bull Put Spread CSV 數據檔案: {target_file}")

    print(f"[INFO] [BullPutSkill] 強制鎖定最新 CSV 檔案進行清洗 (避免讀取舊暫存): {target_file}")
    df = pd.read_csv(target_file)
    df = df.dropna(subset=["Symbol", "Exp Date"])
    df = df[~df["Symbol"].astype(str).str.contains("Downloaded", case=False, na=False)]
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    df = df[df["Exp Date"].astype(str) >= today_str]
    if df.empty:
        raise ValueError(f"最新 CSV ({target_file}) 中無任何未過期合約之 Bull Put 價差記錄。")

    combined_bp = df.copy()
    dedup_cols = [c for c in ["Symbol", "Exp Date", "Leg1 Strike", "Leg2 Strike"] if c in combined_bp.columns]
    if dedup_cols:
        combined_bp = combined_bp.drop_duplicates(subset=dedup_cols)

    # 1. 排除標的股價低於 $30 的小型股（低價股的價外 Put 權利金常只有幾分錢，手續費佔比過高且缺乏造市商流動性）
    price_col = next((c for c in ["Price~", "Price", "Stock Price"] if c in combined_bp.columns), None)
    if price_col:
        price_num = pd.to_numeric(combined_bp[price_col].astype(str).str.replace("$", "").str.strip(), errors="coerce")
        before_cnt = len(combined_bp)
        valid_price_mask = price_num >= 30.0
        if valid_price_mask.any():
            combined_bp = combined_bp[valid_price_mask]
            print(f"[INFO] [BullPutSkill] 股價門檻檢驗 (Price >= $30): 排除 {before_cnt - len(combined_bp)} 筆低價股，剩餘 {len(combined_bp)} 筆")

    # 2. DTE 嚴格限制在 20 ～ 35 天（排除 > 45 天的冷門合約）
    if "DTE" in combined_bp.columns:
        dte_num = pd.to_numeric(combined_bp["DTE"].astype(str).str.strip(), errors="coerce")
        # 先排除 > 45 天的冷門合約
        combined_bp = combined_bp[dte_num <= 45].copy()
        # 優先嚴格限制在 20 ~ 35 天甜蜜期
        dte_current = pd.to_numeric(combined_bp["DTE"].astype(str).str.strip(), errors="coerce")
        dte_sweet = combined_bp[(dte_current >= 20) & (dte_current <= 35)]
        if not dte_sweet.empty:
            combined_bp = dte_sweet
            print(f"[INFO] [BullPutSkill] DTE 嚴格約束 (20~35 天): 鎖定 {len(combined_bp)} 筆甜蜜期合約")
        else:
            print(f"[WARN] [BullPutSkill] 無 20~35 天合約，使用已排除 > 45 天之合約 (共 {len(combined_bp)} 筆)")

    # 3. 價差寬度 Width 與 Width - Max Loss (唯一基準)
    if "Leg1 Strike" in combined_bp.columns and "Leg2 Strike" in combined_bp.columns:
        s1 = pd.to_numeric(combined_bp["Leg1 Strike"], errors="coerce")
        s2 = pd.to_numeric(combined_bp["Leg2 Strike"], errors="coerce")
        combined_bp["Width"] = (s1 - s2).abs().round(2)

        ml_col = next((c for c in ["Max Loss", "MaxLoss"] if c in combined_bp.columns), None)
        if ml_col:
            ml_num = pd.to_numeric(combined_bp[ml_col].astype(str).str.replace("$", "").str.strip(), errors="coerce")
            combined_bp["Width-MaxLoss"] = (combined_bp["Width"] - ml_num).round(2)
            # 確保 Net Credit 嚴格等於 Width - Max Loss 唯一基準
            combined_bp["Net Credit"] = combined_bp["Width-MaxLoss"]

    # 排序：以 Loss Prob 昇序為主
    if "Loss Prob" in combined_bp.columns:
        loss_clean = combined_bp["Loss Prob"].astype(str).str.replace("%", "").str.strip()
        combined_bp["_loss_sort"] = pd.to_numeric(loss_clean, errors="coerce")
        combined_bp = combined_bp.sort_values(by="_loss_sort", ascending=True)
        combined_bp = combined_bp.drop(columns=["_loss_sort"])

    top_df = combined_bp.head(top_count).copy()
    print(f"[INFO] [BullPutSkill] 數據清洗完成：篩選出 {len(top_df)} 筆未過期候選價差組合 (涵蓋標的: {list(top_df['Symbol'].unique())[:6]})")
    return top_df


def build_bull_put_prompt(top_df):
    """組裝專屬之 Bull Put Spread 提示詞 (強制加入流動性與 DTE 硬性約束)"""
    display_cols = [
        c for c in [
            "Symbol", "Price~", "Exp Date", "DTE", "Leg1 Strike", "Leg1 Bid",
            "Leg2 Strike", "Leg2 Ask", "Width", "Max Loss", "Width-MaxLoss", "Net Credit",
            "Max Profit", "Return", "Loss Prob", "BE (Buffer)", "IV Rank"
        ] if c in top_df.columns
    ]
    table_str = top_df[display_cols].to_string(index=False)

    return f"""你是一名華爾街頂級期權量化經理人與波動率價差收租專家。
請根據以下來自 Barchart Bull Put Spread 官方篩選器的未過期候選價差組合清單，進行深度的多因子量化評選，挑選出「唯一最佳、勝率極高、安全緩衝極充足、性價比最高」的 1 組 Bull Put 垂直價差組合：

======================================================================
【待評選之 Bull Put 垂直價差候選清單】
======================================================================
{table_str}

======================================================================
【🚨 流動性與 DTE 硬性約束條件 (CRITICAL RULES - 必須嚴格遵守)】
======================================================================
1. **DTE 嚴格限制在 20 ～ 35 天**：
   - 挑選組合之到期天數 (DTE) 必須嚴格落在 20 至 35 天範圍內。
   - 嚴格排除 > 45 天的冷門合約（時間過長、Theta 衰退過慢且缺乏近月做市商流動性）。
2. **排除標的股價低於 $30 的小型股**：
   - 標的現價 (Price) 必須 >= $30。
   - 嚴格排除股價 < $30 的小型股（低價股的價外 Put 權利金常只有幾分錢，手續費佔比過高且缺乏造市商流動性）。
3. **預期收入基準：嚴格以 Width - Max Loss 作為唯一基準**：
   - AI 推薦之組合淨權利金收入 (Net Credit)，必須嚴格以表格中的【價差寬度 (Width) - 最大虧損 (Max Loss)】作為唯一基準 (Width = |Leg1 Strike - Leg2 Strike|，Net Credit = Width - Max Loss)。
   - **絕對不得自行臆測、捏造或隨意估計預期收入！**

======================================================================
【量化評比標準】
1. **安全防禦空間 (Break-Even Buffer %)**：
   優先挑選下檔緩衝空間充足（BE Buffer 較大），現貨價格遠高於賣出下翼 Put 履約價，具備堅實技術支撐的標的。
2. **極高勝率與低跌破機率 (Low Loss Probability)**：
   跌破虧損機率 (Loss Prob) 越低越優（通常建議小於 20%~25%），享有 75% 以上的高概率收租勝率。
3. **合理的淨權利金收入 (Net Credit & Return %)**：
   在確保極高安全邊際的前提下，提供健康的每口淨權利金收入（嚴格以 Width - Max Loss 為準）。
4. **期限窗口最適性 (DTE Horizon)**：
   DTE 嚴格限制在 20 至 35 天（鎖定時間價值 Theta 加速衰退甜蜜期與做市商高流動性窗口，嚴格排除 > 45 天）。

======================================================================
【輸出格式規範】
請嚴格依據下列 Markdown 結構輸出，不得更動欄位名稱：

#### 📌 【最佳 Bull Put 垂直價差推薦】
- **推薦標的代號 (Symbol)**: [標的代號，如 QQQ]
- **標的現價 (Price)**: $[現價，必須 >= 30]
- **建議策略**: Bull Put Spread (垂直賣權信用價差)
- **到期日 (Exp Date)**: [YYYY-MM-DD]
- **到期天數 (DTE)**: [數字] 天 (必須介於 20~35 天)
- **賣出下翼 (Short Put Leg 1)**: 履約價 $[數字] @ Bid $[數字]
- **買入保護 (Long Put Leg 2)**: 履約價 $[數字] @ Ask $[數字]
- **價差寬度 (Width)**: $[數字]
- **最大虧損風險 (Max Loss)**: $[數字]
- **淨權利金收入 (Net Credit)**: $[數字] (必須嚴格等於 Width - Max Loss，不得臆測)
- **損益兩平點與安全空間 (Break-Even Buffer)**: $[數字] (緩衝約 [百分比]%)
- **最大報酬率 (Max Return %)**: [數字]%
- **跌破虧損機率 (Loss Prob %)**: [數字]%
- **進場推薦理由與深度量化分析**:
  1. [安全邊際與下檔支撐評估]
  2. [跌破機率與勝率優勢]
  3. [權利金收斂與 Theta 衰退性價比 (符合 DTE 20~35 天及流動性約束)]
"""


def parse_best_bull_put(report_text, top_df):
    """自報告中解析獲勝之 Bull Put 價差合約參數"""
    if not report_text:
        return None

    sym_m = re.search(r"(?:推薦標的代號|標的代號|Symbol).*?[:：]\s*[*_`]*([A-Za-z0-9\.\-]+)", report_text, re.IGNORECASE)
    symbol = sym_m.group(1).strip().upper().replace("*", "") if sym_m else None

    exp_m = re.search(r"(?:到期日|Exp(?:iration)?\s*(?:Date)?).*?[:：]\s*[*_`]*([0-9\-\/]{8,10})", report_text, re.IGNORECASE)
    exp_date_norm = normalize_exp_date(exp_m.group(1)) if exp_m else None

    # Leg 1 Short Put
    leg1_m = re.search(r"(?:賣出下翼|Short Put|Leg 1).*?履約價.*?\$?\s*([0-9]+(?:\.[0-9]+)?)", report_text, re.IGNORECASE)
    if not leg1_m:
        leg1_m = re.search(r"Leg 1 Strike.*?\$?\s*([0-9]+(?:\.[0-9]+)?)", report_text, re.IGNORECASE)
    leg1_strike = None
    if leg1_m:
        try:
            leg1_strike = float(leg1_m.group(1))
        except Exception:
            leg1_strike = None

    # Leg 2 Long Put
    leg2_m = re.search(r"(?:買入保護|Long Put|Leg 2).*?履約價.*?\$?\s*([0-9]+(?:\.[0-9]+)?)", report_text, re.IGNORECASE)
    if not leg2_m:
        leg2_m = re.search(r"Leg 2 Strike.*?\$?\s*([0-9]+(?:\.[0-9]+)?)", report_text, re.IGNORECASE)
    leg2_strike = None
    if leg2_m:
        try:
            leg2_strike = float(leg2_m.group(1))
        except Exception:
            leg2_strike = None

    # Net Credit
    credit_m = re.search(r"(?:淨權利金收入|Net Credit).*?[:：]\s*[*_`]*\$?\s*([0-9]+(?:\.[0-9]+)?)", report_text, re.IGNORECASE)
    net_credit = 0.50
    if credit_m:
        try:
            net_credit = float(credit_m.group(1))
        except Exception:
            net_credit = 0.50

    # 備援：若解析缺失，自 top_df 首筆取出
    if not symbol or not exp_date_norm or leg1_strike is None or leg2_strike is None:
        top_row = top_df.iloc[0]
        symbol = str(top_row["Symbol"]).strip().upper()
        exp_date_norm = normalize_exp_date(top_row["Exp Date"])
        leg1_strike = float(top_row["Leg1 Strike"])
        leg2_strike = float(top_row["Leg2 Strike"])
        net_credit = float(str(top_row.get("Net Credit", 0.5)).replace("$", "")) if "Net Credit" in top_row else 0.50

    # 確保 Short Strike > Long Strike (Bull Put Spread 特性)
    if leg1_strike < leg2_strike:
        leg1_strike, leg2_strike = leg2_strike, leg1_strike

    # 嚴格以表格中的 Width - Max Loss 作為唯一基準校正 net_credit
    if top_df is not None and not top_df.empty:
        cond = (top_df["Symbol"].astype(str).str.upper() == symbol)
        if exp_date_norm and "Exp Date" in top_df.columns:
            cond = cond & (top_df["Exp Date"].astype(str) == exp_date_norm)
        if leg1_strike and "Leg1 Strike" in top_df.columns:
            cond = cond & (pd.to_numeric(top_df["Leg1 Strike"], errors="coerce") == leg1_strike)
        matches = top_df[cond]
        if not matches.empty:
            matched_row = matches.iloc[0]
            ml_col = next((c for c in ["Max Loss", "MaxLoss"] if c in matched_row), None)
            if ml_col:
                try:
                    ml_val = float(str(matched_row[ml_col]).replace("$", "").strip())
                    exact_width = abs(leg1_strike - leg2_strike)
                    exact_credit = round(exact_width - ml_val, 2)
                    if exact_credit > 0:
                        if abs(net_credit - exact_credit) > 0.01:
                            print(f"[INFO] [BullPutSkill] 依硬性約束校正 Net Credit: ${exact_credit:.2f} (原解析: ${net_credit:.2f}, Width=${exact_width:.2f}, MaxLoss=${ml_val:.2f})")
                        net_credit = exact_credit
                except Exception:
                    pass

    spread_info = {
        "symbol": symbol,
        "strategy": "Bull Put Spread",
        "action": "SELL",
        "exp_date": exp_date_norm,
        "exp_date_disp": format_exp_date_display(exp_date_norm),
        "leg1_strike": leg1_strike,
        "leg2_strike": leg2_strike,
        "net_credit": net_credit,
        "report_text": report_text,
    }
    return spread_info


def save_bull_put_report_and_notify(report_text, spread_info):
    """儲存 Bull Put 報告並推播至 LINE"""
    now = datetime.datetime.now()
    ts_str = now.strftime("%Y-%m-%d_%H%M%S")
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    archive_fname = f"latest_bull_put_{ts_str}.txt"
    archive_path = os.path.join(REPORTS_DIR, archive_fname)
    latest_path = os.path.join(BARCHART_DIR, "latest_bull_put.txt")

    content = f"==分析時間：{now_str}==\n{report_text}\n"

    for p in [archive_path, latest_path]:
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception:
            pass

    print(f"[INFO] [BullPutSkill] 完整報告已存檔至：{archive_path}")

    # 發送 LINE 推播
    if send_push_message:
        sym = spread_info["symbol"]
        exp = spread_info["exp_date_disp"]
        s1 = spread_info["leg1_strike"]
        s2 = spread_info["leg2_strike"]
        nc = spread_info["net_credit"]

        line_msg = f"""🛡️【Barchart Bull Put 垂直價差最佳推薦】
🕒 時間：{now_str}
📋 標的：{sym} Bull Put Spread
📅 到期日：{exp}
📉 組合：賣出 Short Put ${s1} / 買入 Long Put ${s2}
💵 預估權利金收入：${nc:.2f}

📁 完整報告：{archive_fname}"""
        print(f"[INFO] [BullPutSkill] 正在發送 LINE 推播通知...")
        ok = send_push_message(line_msg.strip())
        if ok:
            print(f"[SUCCESS] [BullPutSkill] ✅ LINE 推播成功！")

    return archive_path


# ==============================================================================
# 2. 主控管線入口 (Pipeline Orchestrator with Skills)
# ==============================================================================
def run_bull_put_pipeline(
    headless=False,
    dry_run=False,
    skip_download=False,
    skip_order=False,
    retries=3,
    #no_line=False,
    no_line=True,
    max_slippage=0.5,
    step=0.05,
    step_time=10.0,
    max_steps=10,
):
    """
    執行 Bull Put 完整模組化技能流程：
      (1) 下載 Bull Put Spread 官方 CSV
      (2) 調用 Gemini AI 量化分析精選 1 檔最佳垂直價差
      (3) 調用 ScaleInOrderSkill 下單並處理 Error 201 兩側衝突防禦
    """
    global send_push_message
    if no_line:
        send_push_message = None

    start_time = time.time()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("\n" + "=" * 70)
    print(f"🌟 Barchart Bull Put 垂直價差 AI 選股與自動下單系統啟動 ({now_str})")
    print("=" * 70)

    driver = None
    bp_csv = None

    try:
        # ======================================================================
        # (1) 步驟 1: 下載 Bull Put Spread 官方篩選器數據
        # ======================================================================
        print("\n📥 【步驟 1】取得 Bull Put Spread 官方 CSV 表格")
        print("-" * 70)
        if not skip_download:
            account, password = load_credentials_from_env(ENV_FILE)
            driver = init_driver(download_dir=BARCHART_DIR, profile_dir=PROFILE_DIR, headless=headless)
            login_if_needed(driver, account, password)
            bp_csv = download_bull_put_csv(driver, target_dir=BARCHART_DIR)
        else:
            candidates = glob.glob(os.path.join(BARCHART_DIR, "*bull-put*.csv"))
            if not candidates:
                candidates = glob.glob(os.path.join(OLD_DIR, "*bull-put*.csv"))
            if candidates:
                candidates.sort(key=os.path.getmtime, reverse=True)
                bp_csv = candidates[0]
                print(f"[INFO] (跳過下載) 使用現存 Bull Put CSV: {bp_csv}")
            else:
                raise FileNotFoundError("未找到任何現存 Bull Put Spread CSV 檔案。")

        # 關閉瀏覽器釋放資源
        if driver:
            try:
                driver.quit()
                driver = None
            except Exception:
                pass

        # ======================================================================
        # (2) 步驟 2: 清洗數據並調用 NVIDIA DeepSeek AI 深度量化分析
        # ======================================================================
        print("\n🎯 【步驟 2】清洗數據並呼叫 NVIDIA DeepSeek AI 精選唯一最佳 Bull Put Spread")
        print("-" * 70)
        api_key = load_nv_api_key(ENV_FILE) if load_nv_api_key else None
        top_df = clean_and_prepare_bull_put_data(bp_csv)
        prompt = build_bull_put_prompt(top_df)

        report_text = None
        for attempt in range(1, retries + 1):
            print(f"[INFO] [BullPutSkill] 正在向 NVIDIA DeepSeek 請求 Bull Put 深度量化分析 (嘗試第 {attempt}/{retries} 次)...")
            try:
                if call_nv_for_skill:
                    res = call_nv_for_skill(prompt=prompt, api_key=api_key, min_chars=500, max_output_tokens=262144)
                    if res and len(res.strip()) > 300:
                        report_text = res
                        print(f"[SUCCESS] [BullPutSkill] ✅ 第 {attempt} 次成功獲得 Bull Put 最佳量化評選報告！")
                        break
            except Exception as e:
                print(f"[WARN] [BullPutSkill] 第 {attempt} 次呼叫異常: {e}")
            time.sleep(2)

        if not report_text:
            print("[WARN] [BullPutSkill] AI 分析未果，啟動排行榜首位量化備援機制...")
            top_row = top_df.iloc[0]
            report_text = f"""#### 📌 【最佳 Bull Put 垂直價差推薦 (規則型備援)】
- **推薦標的代號 (Symbol)**: {top_row['Symbol']}
- **標的現價 (Price)**: ${top_row.get('Price~', 0)}
- **建議策略**: Bull Put Spread (垂直賣權信用價差)
- **到期日 (Exp Date)**: {top_row['Exp Date']}
- **到期天數 (DTE)**: {top_row.get('DTE', 30)} 天
- **賣出下翼 (Short Put Leg 1)**: 履約價 ${top_row['Leg1 Strike']} @ Bid ${top_row.get('Leg1 Bid', 0)}
- **買入保護 (Long Put Leg 2)**: 履約價 ${top_row['Leg2 Strike']} @ Ask ${top_row.get('Leg2 Ask', 0)}
- **淨權利金收入 (Net Credit)**: ${top_row.get('Net Credit', 0.5)}
- **進場推薦理由與深度量化分析**:
  1. 系統自動由 Barchart 官方篩選器按跌破機率昇序與高性價比綜合排名首位自動挑選。
"""

        spread_info = parse_best_bull_put(report_text, top_df)
        save_bull_put_report_and_notify(report_text, spread_info)

        print(f"\n✨ 【Bull Put 精選價差組合】: {spread_info['symbol']} Exp={spread_info['exp_date_disp']} 賣出 ${spread_info['leg1_strike']} Put / 買入 ${spread_info['leg2_strike']} Put (預期收入: ${spread_info['net_credit']:.2f})")

        # ======================================================================
        # (3) 步驟 3: 調用 ScaleInOrderSkill 下單並處理 Error 201 兩側衝突防禦
        # ======================================================================
        print("\n⚡ 【步驟 3】連線 IBKR 執行 Bull Put Spread 組合單 (整合 ScaleInOrderSkill)")
        print("-" * 70)

        if skip_order:
            print("[INFO] 已略過 IBKR 下單步驟 (--skip-order)")
            return True

        cfg = load_env_settings(ENV_FILE)
        host = cfg.get("IB_HOST", "127.0.0.1")
        port = cfg.get("IB_PORT", 4001)
        target_account = cfg.get("IB_TARGET_ACCOUNT", "")

        ib = None
        try:
            ib = create_fast_ib_connection(host=host, port=port)
            symbol = spread_info["symbol"]
            exp_date = spread_info["exp_date"]
            s1 = spread_info["leg1_strike"]
            s2 = spread_info["leg2_strike"]
            credit = float(spread_info.get("net_credit", 0.5) or 0.5)

            # 建立並驗證期權雙腿合約
            short_put = Option(symbol, exp_date, s1, "P", "SMART")
            long_put = Option(symbol, exp_date, s2, "P", "SMART")

            qualified = ib.qualifyContracts(short_put, long_put)
            if not short_put.conId or not long_put.conId:
                print(f"[WARN] ⚠️ 期權腿合約在 IBKR 查無定義 (short: {short_put.conId}, long: {long_put.conId})，略過下單。")
                return False

            print(f"[SUCCESS] ✅ 雙腿合約通過 IBKR 驗證: Short Put conId={short_put.conId}, Long Put conId={long_put.conId}")

            # 向 IBKR 取得雙腿即時盤口報價進行流動性死水檢驗與公允中間價計算
            ib.reqMarketDataType(3)
            short_put_ticker = ib.reqMktData(short_put, '', False, False)
            long_put_ticker = ib.reqMktData(long_put, '', False, False)
            ib.sleep(2.0)  # 等待行情回傳

            # 雙腿盤口流動性檢查
            if long_put_ticker.ask <= 0 or short_put_ticker.bid <= 0:
                logger.warning("雙腿存在零報價死水期，做市商無意撮合，放棄此標的！")
                print("⚠️ [流動性熔斷] 雙腿存在零報價死水期，做市商無意撮合，放棄此標的！")
                try:
                    ib.cancelMktData(short_put)
                    ib.cancelMktData(long_put)
                except Exception:
                    pass
                return False

            # 計算真實市場做市商買盤 (Natural Bid) 與公允中價 (Mid)
            natural_bid = short_put_ticker.bid - long_put_ticker.ask
            if natural_bid <= 0.05:
                logger.warning(f"即時做市商買盤 Natural Bid 僅 ${natural_bid:.2f} <= $0.05，毫無收租價值，終止下單！")
                print(f"⚠️ [流動性熔斷] 即時做市商買盤 Natural Bid 僅 ${natural_bid:.2f} <= $0.05，毫無收租價值，終止下單！")
                try:
                    ib.cancelMktData(short_put)
                    ib.cancelMktData(long_put)
                except Exception:
                    pass
                return False

            natural_ask = short_put_ticker.ask - long_put_ticker.bid
            mid_price = (natural_bid + natural_ask) / 2

            # 以真實 Mid 啟動讓步，而非 Barchart 歷史價格
            start_price = round(mid_price, 2)
            logger.info(f"即時做市商買盤: ${natural_bid:.2f} | 啟動 Walk-Up 起始限價: ${start_price:.2f}")
            print(f"[INFO] [BullPutSkill] 雙腿即時盤口: Short Put (Bid=${short_put_ticker.bid:.2f}, Ask=${short_put_ticker.ask:.2f}) | Long Put (Bid=${long_put_ticker.bid:.2f}, Ask=${long_put_ticker.ask:.2f})")
            print(f"[INFO] [BullPutSkill] 即時做市商買盤: ${natural_bid:.2f} | 啟動 Walk-Up 起始限價: ${start_price:.2f} (原 Barchart 歷史參考: ${credit:.2f})")

            try:
                ib.cancelMktData(short_put)
                ib.cancelMktData(long_put)
            except Exception:
                pass

            # 建立 BAG 垂直價差組合單
            # 重要：當母單為 SELL 且使用正數限價 (Credit) 時，IBKR 實際執行方向 = Order.action * ComboLeg.action
            # 因此賣出下翼 (short_put) 必須設為 BUY (SELL*BUY=SELL)，買入保護 (long_put) 必須設為 SELL (SELL*SELL=BUY)
            c1 = ComboLeg(conId=short_put.conId, ratio=1, action="BUY", exchange="SMART")
            c2 = ComboLeg(conId=long_put.conId, ratio=1, action="SELL", exchange="SMART")
            bag_contract = Contract(
                symbol=symbol,
                secType="BAG",
                currency="USD",
                exchange="SMART",
                comboLegs=[c1, c2],
            )

            # 規劃 Attached 停利停損價格 (以真實市場起始價格 start_price 計算：0.5倍停利，2倍停損)
            take_profit_price = round(start_price * 0.5, 2)
            stop_loss_price = round(start_price * 2.0, 2)

            print(f"  -> 附屬單規劃 (以真實市場起始價 ${start_price:.2f} 計算):")
            print(f"     • 🎯 Profit Taker (50%停利單): ${take_profit_price:.2f}")
            print(f"     • 🛑 Stop Loss    (2倍停損單):  ${stop_loss_price:.2f}")

            # 調用 ScaleInOrderSkill 執行下單與加碼防衝突處理
            scale_skill = ScaleInOrderSkill(ib_instance=ib)
            order_res = scale_skill.execute(
                contract=bag_contract,
                symbol=symbol,
                action="SELL",
                quantity=1.0,
                credit=start_price,
                take_profit_price=take_profit_price,
                stop_loss_price=stop_loss_price,
                target_account=target_account,
                dry_run=dry_run,
                max_slippage=max_slippage,
                step=step,
                step_time=step_time,
                max_steps=max_steps,
            )

            print(f"[INFO] ScaleInOrderSkill 處理結果: {order_res.get('status')} (總口數: {order_res.get('total_qty', 1)} 口)")

        finally:
            if ib and ib.isConnected():
                ib.disconnect()
                print("[INFO] 已安全斷開 IBKR 連線。")

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    elapsed = time.time() - start_time
    print("\n" + "#" * 70)
    print(f"# ✅ Barchart Bull Put 垂直價差自動化流程完成！(總耗時: {elapsed:.1f} 秒)")
    print("#" * 70 + "\n")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Barchart Bull Put 垂直價差 AI 自動化選股與下單流水線 (Skills 架構版)")
    parser.add_argument("--headless", action="store_true", help="以無瀏覽器視窗模式執行")
    parser.add_argument("--dry-run", action="store_true", help="以模擬模式執行下單（預查市價並計算附屬單，不實際送單至 IBKR）")
    parser.add_argument("--skip-download", action="store_true", help="跳過下載步驟，使用現存 CSV")
    parser.add_argument("--skip-order", action="store_true", help="跳過向 IBKR 下單步驟")
    parser.add_argument("--retries", type=int, default=3, help="Gemini 請求最大重試次數")
    parser.add_argument("--no-line", action="store_true", help="不發送 LINE 通知")
    parser.add_argument("--no-sleep", action="store_true", help="執行完畢後直接退出，不常駐 sleep")
    parser.add_argument("--step-time", type=float, default=40.0, help="自適應步進修單每階等待秒數 (預設: 40.0 秒)")
    parser.add_argument("--max-steps", type=int, default=10, help="自適應步進修單最大讓步次數 (預設: 10 次)")
    parser.add_argument("--max-slippage", type=float, default=0.5, help="自適應步進修單最大累計讓步金額 (預設: 0.25)")
    parser.add_argument("--step", type=float, default=0.05, help="自適應步進修單每次讓步金額 (預設: 0.05)")
    args = parser.parse_args()

    run_bull_put_pipeline(
        headless=args.headless,
        dry_run=args.dry_run,
        skip_download=args.skip_download,
        skip_order=args.skip_order,
        retries=args.retries,
        no_line=args.no_line,
        max_slippage=args.max_slippage,
        step=args.step,
        step_time=args.step_time,
        max_steps=args.max_steps,
    )

    if not args.no_sleep and not args.dry_run and sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
        try:
            time.sleep(60 * 60 * 20)
        except KeyboardInterrupt:
            print("\n[INFO] 使用者手動中斷常駐程序。")
