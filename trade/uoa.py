#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Barchart UOA Options Pipeline with Skills Architecture (trade/uoa.py)
================================================================================
本模組為異常期權大單 (UOA) 高階管線調度器，導入模組化 Agentic Skills 設計：
  (1) Download Skill (trade/skills/download_skill/):
      - 自動連線 Barchart 下載個股與 ETF 異常期權大單 CSV：
        • Stocks: https://www.barchart.com/options/unusual-activity/stocks
        • ETFs:   https://www.barchart.com/options/unusual-activity/etfs
  (2) UOA Analysis Skill (trade/skills/uoa_skill/):
      - 清洗篩選個股與 ETF 二份數據源，過濾 0DTE 極短線雜訊 (7 <= DTE <= 120)
      - 調用 Gemini AI 量化分析，各挑選出 BUY CALL 與 BUY PUT，產出共 4 檔預選期權標的
      - 儲存 latest_ai_analysis.txt 供即時看板展示，並推播 LINE 速覽
  (3) Download Skill:
      - 導航至 https://www.barchart.com/stocks/quotes/{symbol}/vertical-spreads/bull-put-spread
      - 下載這 4 檔預選標的之官方 Bull Put 垂直價差篩選數據，若查無數據則依風控規則放棄該標的
        • Target Folder: trade/Barchart/
  (4) Gemini Helper (trade/skills/gemini_helper.py):
      - 針對每檔預選標的重複執行 Bull Put 垂直價差自動化分析流程：
      - 清洗各標的價差數據、過濾已到期合約、按跌破機率與報酬率排序
      - 調用 Gemini AI 深度量化分析，精選各預選標的之最佳 Bull Put Spread 組合
      - 儲存詳細分析報告並推播至手機 LINE
  (5) Scale In Order Skill (trade/skills/scale_in_order_skill/):
      - 依序為每檔精選標的連線 IBKR 建立下翼賣權 (Short Put) 與保護賣權 (Long Put) 之 BAG 組合單
      - 當執行加碼委託 (SELL) 時若遭遇 IBKR Error 201:
        "Cannot have open orders on both sides of the same US Option contract."
        自動依序執行 4 大重組步驟：撤銷衝突舊單 -> 送出加碼母單 -> 合併新舊口數 -> 重掛總口數 OCA 括號單
================================================================================
"""

import os
import sys
import glob
import time
import re
import datetime
import argparse
import pandas as pd

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
        download_uoa_stocks_csv,
        download_uoa_etfs_csv,
        download_stock_vertical_spread_csv,
        download_bull_put_csv,
        PROFILE_DIR,
        UoaAnalysisSkill,
        ScaleInOrderSkill,
        load_env_settings,
        create_fast_ib_connection,
        load_gemini_api_key,
        call_gemini_for_skill,
    )
except ImportError:
    from skills.download_skill import (
        init_driver,
        dismiss_popups,
        login_if_needed,
        load_credentials_from_env,
        download_uoa_stocks_csv,
        download_uoa_etfs_csv,
        download_stock_vertical_spread_csv,
        download_bull_put_csv,
        PROFILE_DIR,
    )
    from skills.uoa_skill import UoaAnalysisSkill
    from skills.scale_in_order_skill import ScaleInOrderSkill
    from skills.ibkr_skill import (
        load_env_settings,
        create_fast_ib_connection,
    )
    from skills.gemini_helper import load_gemini_api_key, call_gemini_for_skill

try:
    from notifier import send_push_message
except ImportError:
    send_push_message = None

try:
    from ib_insync import IB, Option, Contract, ComboLeg
except ImportError:
    IB = None
    Option = None
    Contract = None
    ComboLeg = None


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


def clean_and_prepare_bull_put_data(csv_path_input, top_count=35, target_symbols=None):
    """
    清洗 Bull Put 價差數據，過濾已過期合約。
    若指定 target_symbols (UOA 預選標的)，僅鎖定並保留該等標的之專屬 Bull Put 價差；
    若無任何預選標的之價差數據，回傳空 DataFrame (依風控規則放棄，不跨標的備援)。
    """
    candidate_files = []
    if csv_path_input:
        if isinstance(csv_path_input, list):
            for p in csv_path_input:
                if p and os.path.exists(p) and p not in candidate_files:
                    candidate_files.append(p)
        elif isinstance(csv_path_input, str) and os.path.exists(csv_path_input):
            candidate_files.append(csv_path_input)

    # 若無指定輸入檔案，則自目錄中搜尋 (若有 target_symbols 優先搜尋對應標的，否則搜尋全域)
    if not candidate_files:
        if target_symbols:
            for s in target_symbols:
                cands = (
                    glob.glob(os.path.join(BARCHART_DIR, f"{str(s).lower()}-*bull-put*.csv"))
                    + glob.glob(os.path.join(BARCHART_DIR, f"*{str(s).lower()}_*bull-put*.csv"))
                    + glob.glob(os.path.join(OLD_DIR, f"{str(s).lower()}-*bull-put*.csv"))
                    + glob.glob(os.path.join(OLD_DIR, f"*{str(s).lower()}_*bull-put*.csv"))
                )
                cands.sort(key=os.path.getmtime, reverse=True)
                for f in cands:
                    if f not in candidate_files:
                        candidate_files.append(f)
        else:
            all_bp = (
                glob.glob(os.path.join(BARCHART_DIR, "*bull-put*.csv"))
                + glob.glob(os.path.join(OLD_DIR, "*bull-put*.csv"))
            )
            all_bp.sort(key=os.path.getmtime, reverse=True)
            for f in all_bp:
                if f not in candidate_files:
                    candidate_files.append(f)

    all_dfs = []
    today_str = datetime.date.today().strftime("%Y-%m-%d")

    for f in candidate_files:
        if not os.path.exists(f) or os.path.getsize(f) < 350:
            continue
        # 嚴格排除包含 bull-call 的檔案
        if "bull-call" in os.path.basename(f).lower():
            continue
        try:
            df = pd.read_csv(f)
            cols_map = {c.strip(): c.strip() for c in df.columns}
            df = df.rename(columns=cols_map)
            # 若個股價差 CSV 內無 Symbol 欄位，從檔名提取並自動補齊 (例如 nke-bull-put... -> NKE)
            if "Symbol" not in df.columns:
                fname = os.path.basename(f)
                extracted_sym = fname.split("-")[0].upper()
                df["Symbol"] = extracted_sym

            df = df.dropna(subset=["Symbol"])
            df["Symbol"] = df["Symbol"].astype(str).str.strip().str.upper()
            df = df[~df["Symbol"].str.contains("DOWNLOADED", case=False, na=False)]
            if "Exp Date" in df.columns:
                df = df[df["Exp Date"].astype(str) >= today_str]
            if not df.empty:
                all_dfs.append(df)
        except Exception:
            continue

    if not all_dfs:
        print("[WARN] [BullPutSkill] 未找到任何含有未過期合約之 Bull Put Spread 數據檔案。")
        return pd.DataFrame()

    combined_bp = pd.concat(all_dfs, ignore_index=True)
    dedup_cols = [c for c in ["Symbol", "Exp Date", "Leg1 Strike", "Leg2 Strike"] if c in combined_bp.columns]
    if dedup_cols:
        combined_bp = combined_bp.drop_duplicates(subset=dedup_cols)

    # 確保 Net Credit 欄位存在 (Barchart 個股垂直價差常以 Max Profit 代表每口最大淨權利金收入)
    if "Net Credit" not in combined_bp.columns and "Max Profit" in combined_bp.columns:
        combined_bp["Net Credit"] = combined_bp["Max Profit"]

    # 排序：以 Loss Prob 昇序為主
    if "Loss Prob" in combined_bp.columns:
        loss_clean = combined_bp["Loss Prob"].astype(str).str.replace("%", "").str.strip()
        combined_bp["_loss_sort"] = pd.to_numeric(loss_clean, errors="coerce")
        combined_bp = combined_bp.sort_values(by="_loss_sort", ascending=True)
        combined_bp = combined_bp.drop(columns=["_loss_sort"])

    # 若指定 target_symbols (如 UOA 預選標的)，嚴格只保留該等標的之價差數據
    if target_symbols:
        target_set = {str(s).strip().upper() for s in target_symbols if s}
        sym_match = combined_bp[combined_bp["Symbol"].astype(str).str.upper().isin(target_set)]
        if not sym_match.empty:
            matched_syms = list(sym_match["Symbol"].unique())
            print(f"[INFO] [BullPutSkill] 成功鎖定預選標的 {matched_syms} 之 Bull Put 垂直價差數據 (共 {len(sym_match)} 筆)！")
            return sym_match.head(top_count).copy()
        else:
            print(f"[WARN] [BullPutSkill] 數據中無任何符合預選標的 {list(target_set)} 之 Bull Put 價差記錄。")
            return pd.DataFrame()

    top_df = combined_bp.head(top_count).copy()
    print(f"[INFO] [BullPutSkill] 數據清洗完成：篩選出 {len(top_df)} 筆未過期候選價差組合 (涵蓋標的: {list(top_df['Symbol'].unique())[:6]})")
    return top_df


def build_bull_put_prompt(top_df, target_symbols=None):
    """組裝專屬之 Bull Put Spread 提示詞"""
    display_cols = [
        c for c in [
            "Symbol", "Price~", "Exp Date", "DTE", "Leg1 Strike", "Leg1 Bid", "Bid1",
            "Leg2 Strike", "Leg2 Ask", "Ask2", "Net Credit", "Max Profit", "Max Loss",
            "Return", "Max Profit%", "Loss Prob", "BE (Buffer)", "BE%", "IV Rank"
        ] if c in top_df.columns
    ]
    table_str = top_df[display_cols].to_string(index=False)
    target_str = ", ".join(target_symbols) if target_symbols else "指定標的"
    target_note = (
        f"\n請注意：本輪分析專屬聚焦於標的【{target_str}】，請務必從清單中挑選出該標的最優質價差組合。"
        if target_symbols else ""
    )

    return f"""你是一名華爾街頂級期權量化經理人與波動率價差收租專家。
請根據以下來自 Barchart Bull Put Spread 官方篩選器針對【{target_str}】的未過期候選價差組合清單，進行深度的多因子量化評選，挑選出該標的「唯一最佳、勝率極高、安全緩衝極充足、性價比最高」的 1 組 Bull Put 垂直價差組合：{target_note}

======================================================================
【待評選之 {target_str} Bull Put 垂直價差候選清單】
======================================================================
{table_str}

======================================================================
【量化評比標準】
1. **安全防禦空間 (Break-Even Buffer %)**：
   優先挑選下檔緩衝空間充足（BE Buffer 較大），現貨價格遠高於賣出下翼 Put 履約價，具備堅實技術支撐的標的。
2. **極高勝率與低跌破機率 (Low Loss Probability)**：
   跌破虧損機率 (Loss Prob) 越低越優（通常建議小於 20%~25%），享有 75% 以上的高概率收租勝率。
3. **合理的淨權利金收入 (Net Credit & Return %)**：
   在確保極高安全邊際的前提下，提供健康的年化報酬率與每口淨權利金收入（Net Credit 建議至少 $0.30~$1.50 以上）。
4. **期限窗口最適性 (DTE Horizon)**：
   到期天數 DTE 介於 14 至 50 天（時間價值 Theta 加速衰退甜蜜期）。

======================================================================
【輸出格式規範】
請嚴格依據下列 Markdown 結構輸出，不得更動欄位名稱：

#### 📌 【最佳 Bull Put 垂直價差推薦】
- **推薦標的代號 (Symbol)**: [標的代號，如 {target_str}]
- **標的現價 (Price)**: $[現價]
- **建議策略**: Bull Put Spread (垂直賣權信用價差)
- **到期日 (Exp Date)**: [YYYY-MM-DD]
- **到期天數 (DTE)**: [數字] 天
- **賣出下翼 (Short Put Leg 1)**: 履約價 $[數字] @ Bid $[數字]
- **買入保護 (Long Put Leg 2)**: 履約價 $[數字] @ Ask $[數字]
- **淨權利金收入 (Net Credit)**: $[數字] (每口最大獲利)
- **最大虧損風險 (Max Loss)**: $[數字]
- **損益兩平點與安全空間 (Break-Even Buffer)**: $[數字] (緩衝約 [百分比]%)
- **最大報酬率 (Max Return %)**: [數字]%
- **跌破虧損機率 (Loss Prob %)**: [數字]%
- **進場推薦理由與深度量化分析**:
  1. [安全邊際與下檔支撐評估]
  2. [跌破機率與勝率優勢]
  3. [權利金收斂與 Theta 衰退性價比]
"""


def parse_best_bull_put(report_text, top_df, default_symbol=None):
    """自報告中解析獲勝之 Bull Put 價差合約參數"""
    if not report_text:
        return None

    sym_m = re.search(r"(?:推薦標的代號|標的代號|Symbol).*?[:：]\s*[*_`]*([A-Za-z0-9\.\-]+)", report_text, re.IGNORECASE)
    symbol = sym_m.group(1).strip().upper().replace("*", "") if sym_m else default_symbol

    exp_m = re.search(r"(?:到期日|Exp(?:iration)?\s*(?:Date)?).*?[:：]\s*[*_`]*([0-9\-\/]{8,10})", report_text, re.IGNORECASE)
    exp_date_norm = normalize_exp_date(exp_m.group(1)) if exp_m else None

    # Leg 1 Short Put
    leg1_m = re.search(r"(?:賣出下翼|Short Put|Leg 1).*?履約價.*?\$?([0-9\.]+)", report_text, re.IGNORECASE)
    if not leg1_m:
        leg1_m = re.search(r"Leg 1 Strike.*?\$?([0-9\.]+)", report_text, re.IGNORECASE)
    leg1_strike = float(leg1_m.group(1)) if leg1_m else None

    # Leg 2 Long Put
    leg2_m = re.search(r"(?:買入保護|Long Put|Leg 2).*?履約價.*?\$?([0-9\.]+)", report_text, re.IGNORECASE)
    if not leg2_m:
        leg2_m = re.search(r"Leg 2 Strike.*?\$?([0-9\.]+)", report_text, re.IGNORECASE)
    leg2_strike = float(leg2_m.group(1)) if leg2_m else None

    # Net Credit
    credit_m = re.search(r"(?:淨權利金收入|Net Credit).*?[:：]\s*[*_`]*\$?([0-9\.]+)", report_text, re.IGNORECASE)
    net_credit = float(credit_m.group(1)) if credit_m else 0.50

    # 備援：若解析缺失，自 top_df 首筆取出
    if not symbol or not exp_date_norm or leg1_strike is None or leg2_strike is None:
        if top_df is not None and not top_df.empty:
            top_row = top_df.iloc[0]
            symbol = str(top_row.get("Symbol", default_symbol or "SPY")).strip().upper()
            exp_date_norm = normalize_exp_date(top_row.get("Exp Date", ""))
            leg1_strike = float(top_row.get("Leg1 Strike", 0)) if "Leg1 Strike" in top_row else None
            leg2_strike = float(top_row.get("Leg2 Strike", 0)) if "Leg2 Strike" in top_row else None
            nc_raw = str(top_row.get("Net Credit", top_row.get("Max Profit", 0.5))).replace("$", "").replace(",", "")
            try:
                net_credit = float(nc_raw)
            except Exception:
                net_credit = 0.50

    if not symbol or not exp_date_norm or leg1_strike is None or leg2_strike is None:
        return None

    # 確保 Short Strike > Long Strike (Bull Put Spread 特性)
    if leg1_strike < leg2_strike:
        leg1_strike, leg2_strike = leg2_strike, leg1_strike

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


def save_bull_put_report_and_notify(report_text, spread_info, idx=None, total=None):
    """儲存 Bull Put 報告並推播至 LINE"""
    now = datetime.datetime.now()
    ts_str = now.strftime("%Y-%m-%d_%H%M%S")
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    sym = spread_info["symbol"]

    archive_fname = f"latest_bull_put_{sym}_{ts_str}.txt"
    archive_path = os.path.join(REPORTS_DIR, archive_fname)
    latest_sym_path = os.path.join(BARCHART_DIR, f"latest_bull_put_{sym}.txt")
    latest_path = os.path.join(BARCHART_DIR, "latest_bull_put.txt")

    content = f"==標的：{sym} 分析時間：{now_str}==\n{report_text}\n"

    for p in [archive_path, latest_sym_path, latest_path]:
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception:
            pass

    print(f"[INFO] [BullPutSkill] {sym} 完整報告已存檔至：{archive_path}")

    # 發送 LINE 推播
    if send_push_message:
        exp = spread_info["exp_date_disp"]
        s1 = spread_info["leg1_strike"]
        s2 = spread_info["leg2_strike"]
        nc = spread_info["net_credit"]
        idx_str = f" [{idx}/{total}]" if (idx and total) else ""

        line_msg = f"""🛡️【Barchart UOA -> {sym} Bull Put 垂直價差推薦{idx_str}】
🕒 時間：{now_str}
📋 標的：{sym} Bull Put Spread
📅 到期日：{exp}
📉 組合：賣出 Short Put ${s1} / 買入 Long Put ${s2}
💵 預估權利金收入：${nc:.2f}

📁 完整報告：{archive_fname}"""
        print(f"[INFO] [BullPutSkill] 正在發送 {sym} LINE 推播通知...")
        ok = send_push_message(line_msg.strip())
        if ok:
            print(f"[SUCCESS] [BullPutSkill] ✅ {sym} LINE 推播成功！")

    return archive_path


def execute_bull_put_scale_in_order(spread_info, dry_run=False):
    """
    連線 IBKR 建立下翼賣權 (Short Put) 與保護賣權 (Long Put) 之 BAG 組合單，
    並調用 ScaleInOrderSkill 執行下單與處理 Error 201 兩側衝突防禦。
    """
    symbol = spread_info["symbol"]
    exp_date = spread_info["exp_date"]
    s1 = spread_info["leg1_strike"]
    s2 = spread_info["leg2_strike"]
    credit = float(spread_info.get("net_credit", 0.5) or 0.5)

    print("\n" + "=" * 65)
    print(f"🚀 【IBKR 智能下單模組】執行 {symbol} Bull Put Spread 組合單 (ScaleInOrderSkill)")
    print("=" * 65)

    cfg = load_env_settings(ENV_FILE)
    host = cfg.get("IB_HOST", "127.0.0.1")
    port = cfg.get("IB_PORT", 4001)
    target_account = cfg.get("IB_TARGET_ACCOUNT", "")

    ib = None
    try:
        import random
        ib = create_fast_ib_connection(host=host, port=port, client_id=random.randint(9700, 9799))

        short_put = Option(symbol, exp_date, s1, "P", "SMART")
        long_put = Option(symbol, exp_date, s2, "P", "SMART")

        qualified = ib.qualifyContracts(short_put, long_put)
        if not short_put.conId or not long_put.conId:
            err = f"❌ [合約無效] 期權腿合約在 IBKR 查無定義 (short: {short_put.conId}, long: {long_put.conId})"
            print(f"[WARN] {err}，略過下單。")
            return {"status": "error", "message": err}

        print(f"[SUCCESS] ✅ 雙腿合約通過 IBKR 驗證: Short Put conId={short_put.conId}, Long Put conId={long_put.conId}")

        # 建立 BAG 垂直價差組合單
        c1 = ComboLeg(conId=short_put.conId, ratio=1, action="SELL", exchange="SMART")
        c2 = ComboLeg(conId=long_put.conId, ratio=1, action="BUY", exchange="SMART")
        bag_contract = Contract(
            symbol=symbol,
            secType="BAG",
            currency="USD",
            exchange="SMART",
            comboLegs=[c1, c2],
        )

        take_profit_price = round(credit * 0.5, 2)
        stop_loss_price = round(credit * 2.0, 2)

        print(f"  -> 附屬單規劃:")
        print(f"     • 🎯 Profit Taker (50%停利單): ${take_profit_price:.2f}")
        print(f"     • 🛑 Stop Loss    (2倍停損單):  ${stop_loss_price:.2f}")

        # 調用 ScaleInOrderSkill 執行下單與加碼防衝突處理
        scale_skill = ScaleInOrderSkill(ib_instance=ib)
        order_res = scale_skill.execute(
            contract=bag_contract,
            symbol=symbol,
            action="SELL",
            quantity=1.0,
            credit=credit,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
            target_account=target_account,
            dry_run=dry_run,
        )

        print(f"[INFO] ScaleInOrderSkill 處理結果: {order_res.get('status')} (總口數: {order_res.get('total_qty', 1)} 口)")
        return order_res

    except Exception as e:
        print(f"[ERROR] 執行 Bull Put 下單異常: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        if ib and ib.isConnected():
            ib.disconnect()
            print("[INFO] 已安全斷開 IBKR 連線。")


def run_uoa_pipeline(
    headless=False,
    dry_run=False,
    skip_download=False,
    skip_order=False,
    retries=3,
    #no_line=False,
    no_line=True,
):
    """
    執行 UOA 完整 5 大模組化技能管線：
      (1) 下載個股與 ETF 異常期權 CSV
      (2) 調用 UOA Analysis Skill 精選 4 檔預選合約
      (3) 下載預選標的之官方 Bull Put 垂直價差篩選數據 (Download Skill)
      (4) 有幾檔預選標的，就重複執行幾次 Bull Put 垂直價差自動化分析流程 (Gemini Helper)
      (5) 連線 IBKR 執行 ScaleInOrderSkill 建立各標的之 BAG 組合單與 Error 201 加碼衝突重組
    """
    global send_push_message
    if no_line:
        send_push_message = None

    start_time = time.time()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("\n" + "=" * 70)
    print(f"🌟 Barchart UOA 異常期權大單 AI 自動化選股與下單系統啟動 ({now_str})")
    print("=" * 70)

    driver = None
    stock_csv = None
    etf_csv = None
    spread_csv_list = []

    try:
        # ======================================================================
        # (1) 步驟 1: 下載個股與 ETF 異常期權數據
        # ======================================================================
        print("\n📥 【步驟 1】取得個股與 ETF 異常期權 CSV (Stocks & ETFs UOA)")
        print("-" * 70)
        if not skip_download:
            account, password = load_credentials_from_env(ENV_FILE)
            driver = init_driver(download_dir=BARCHART_DIR, profile_dir=PROFILE_DIR, headless=headless)
            login_if_needed(driver, account, password)

            stock_csv = download_uoa_stocks_csv(driver, target_dir=BARCHART_DIR)
            etf_csv = download_uoa_etfs_csv(driver, target_dir=BARCHART_DIR)
        else:
            candidates_stock = glob.glob(os.path.join(BARCHART_DIR, "*unusual-stock*.csv"))
            if not candidates_stock:
                candidates_stock = glob.glob(os.path.join(OLD_DIR, "*unusual-stock*.csv"))
            if candidates_stock:
                candidates_stock.sort(key=os.path.getmtime, reverse=True)
                stock_csv = candidates_stock[0]
                print(f"[INFO] (跳過下載) 使用現存個股 UOA CSV: {stock_csv}")
            else:
                raise FileNotFoundError("未找到任何現存個股 UOA CSV 檔案。")

            candidates_etf = glob.glob(os.path.join(BARCHART_DIR, "*unusual-etf*.csv"))
            if not candidates_etf:
                candidates_etf = glob.glob(os.path.join(OLD_DIR, "*unusual-etf*.csv"))
            if candidates_etf:
                candidates_etf.sort(key=os.path.getmtime, reverse=True)
                etf_csv = candidates_etf[0]
                print(f"[INFO] (跳過下載) 使用現存 ETF UOA CSV: {etf_csv}")
            else:
                raise FileNotFoundError("未找到任何現存 ETF UOA CSV 檔案。")

        # ======================================================================
        # (2) 步驟 2: UOA Analysis Skill 篩選 0DTE 並各挑選出 BUY CALL 與 BUY PUT (共4檔)
        # ======================================================================
        print("\n🎯 【步驟 2】呼叫 UOA Analysis Skill 篩選 0DTE 並精選 4 檔預選標的")
        print("-" * 70)
        api_key = load_gemini_api_key(ENV_FILE) if load_gemini_api_key else None
        uoa_skill = UoaAnalysisSkill(api_key=api_key, env_path=ENV_FILE)

        data_str, df_stock_f, df_etf_f = uoa_skill.clean_and_prepare_data(
            stock_csv=stock_csv,
            etf_csv=etf_csv,
        )

        analysis_report = uoa_skill.generate_analysis_report(data_str=data_str, max_retries=retries)
        arch_fn, arch_path = uoa_skill.save_report(analysis_report)
        uoa_skill.send_line_summary(analysis_report, archive_filename=arch_fn)

        preselected_targets = uoa_skill.parse_preselected_targets(
            analysis_text=analysis_report,
            df_stock=df_stock_f,
            df_etf=df_etf_f,
        )

        unique_symbols = list(dict.fromkeys([t["symbol"] for t in preselected_targets]))
        print(f"[INFO] 預選涵蓋標的代號: {unique_symbols}")

        # ======================================================================
        # (3) 步驟 3: Download Skill 下載這 4 檔預選標的之官方 Bull Put 垂直價差篩選數據
        # ======================================================================
        print("\n📥 【步驟 3】取得這 4 檔預選標的之官方 Bull Put 垂直價差篩選數據 (Download Skill)")
        print(f"    • 目標 URL: https://www.barchart.com/stocks/quotes/{{symbol}}/vertical-spreads/bull-put-spread")
        print(f"    • 儲存目錄: {BARCHART_DIR}")
        print("-" * 70)

        spread_csv_list = []
        valid_retained_symbols = []
        symbol_csv_map = {}

        for sym in unique_symbols:
            cur_csv = None
            if not skip_download:
                if not driver:
                    account, password = load_credentials_from_env(ENV_FILE)
                    driver = init_driver(download_dir=BARCHART_DIR, profile_dir=PROFILE_DIR, headless=headless)
                    login_if_needed(driver, account, password)
                try:
                    cur_csv = download_stock_vertical_spread_csv(driver, sym, target_dir=BARCHART_DIR)
                except Exception as dl_err:
                    print(f"[WARN] 下載 {sym} 垂直價差異常: {dl_err}")
            else:
                candidates = (
                    glob.glob(os.path.join(BARCHART_DIR, f"{sym.lower()}-*bull-put*.csv"))
                    + glob.glob(os.path.join(BARCHART_DIR, f"*{sym.lower()}_*bull-put*.csv"))
                )
                if not candidates:
                    candidates = (
                        glob.glob(os.path.join(OLD_DIR, f"{sym.lower()}-*bull-put*.csv"))
                        + glob.glob(os.path.join(OLD_DIR, f"*{sym.lower()}_*bull-put*.csv"))
                    )
                if candidates:
                    candidates.sort(key=os.path.getmtime, reverse=True)
                    valid_cands = [c for c in candidates if os.path.getsize(c) > 350 and "bull-call" not in os.path.basename(c).lower()]
                    if valid_cands:
                        monthly_cands = [c for c in valid_cands if "monthly" in os.path.basename(c).lower()]
                        cur_csv = monthly_cands[0] if monthly_cands else valid_cands[0]
                        print(f"[INFO] (跳過下載) 使用現存 {sym} Bull Put 價差 CSV: {cur_csv}")

            if cur_csv and os.path.exists(cur_csv) and os.path.getsize(cur_csv) > 350 and "bull-call" not in os.path.basename(cur_csv).lower():
                if cur_csv not in spread_csv_list:
                    spread_csv_list.append(cur_csv)
                symbol_csv_map[sym] = cur_csv
                valid_retained_symbols.append(sym)
                print(f"[SUCCESS] ✅ 標的 {sym} 成功取得專屬 Bull Put 垂直價差數據！")
            else:
                print(f"[WARN] ⚠️ 標的 {sym} 查無專屬 Bull Put 垂直價差 CSV，依風控規則放棄此預選標的。")

        # 關閉瀏覽器釋放資源
        if driver:
            try:
                driver.quit()
                driver = None
            except Exception:
                pass

        # 檢查是否有任何保留的預選標的
        if not spread_csv_list or not valid_retained_symbols:
            msg = (
                f"📊【UOA 垂直價差無數據提醒】\n"
                f"⚠️ 預選標的 ({', '.join(unique_symbols)}) 均無專屬之 Bull Put 垂直價差數據，已依風控規則放棄並略過期權價差下單程序。"
            )
            print(f"\n{msg}")
            if not no_line:
                try:
                    send_push_message(msg)
                except Exception:
                    pass
            print("\n######################################################################")
            print("# 🏁 Barchart UOA 流程結束 (無符合 Bull Put 垂直價差之標的)")
            print("######################################################################")
            return True

        # ======================================================================
        # (4) 步驟 4 & 5: 針對每檔預選標的，重複執行 Bull Put 垂直價差自動化選股與下單流程
        # ======================================================================
        total_targets = len(valid_retained_symbols)
        print("\n" + "=" * 70)
        print(f"🚀 啟動預選標的 Bull Put 垂直價差自動化分析與下單流程 (共 {total_targets} 檔標的: {', '.join(valid_retained_symbols)})")
        print("=" * 70)

        pipeline_results = []

        for idx, sym in enumerate(valid_retained_symbols, 1):
            print("\n" + "#" * 70)
            print(f"🎯 【標的 {idx}/{total_targets}】開始執行 {sym} 之 Bull Put 垂直價差自動化流程")
            print("#" * 70)

            # (4) 步驟 4: Gemini Helper 清洗價差數據、過濾已到期合約、量化評選最佳 Bull Put
            print(f"\n🎯 【步驟 4.{idx}】清洗 {sym} 價差數據並由 Gemini AI 精選最佳 Bull Put Spread (Gemini Helper)")
            print(f"[INFO] 正在處理標的: {sym}")
            print("-" * 70)

            sym_csv = symbol_csv_map.get(sym)
            csv_input = [sym_csv] if sym_csv else spread_csv_list
            try:
                top_bp_df = clean_and_prepare_bull_put_data(csv_input, target_symbols=[sym])
            except Exception as clean_err:
                print(f"[WARN] [BullPutSkill] 清洗 {sym} 價差數據異常: {clean_err}")
                top_bp_df = pd.DataFrame()

            if top_bp_df is None or top_bp_df.empty:
                msg = f"⚠️ 標的 {sym} 經清洗後無未過期之有效 Bull Put Spread 候選合約，略過此標的下單程序。"
                print(f"\n[WARN] [BullPutSkill] {msg}")
                pipeline_results.append({
                    "symbol": sym,
                    "status": "no_contracts",
                    "message": msg
                })
                continue

            bp_prompt = build_bull_put_prompt(top_bp_df, target_symbols=[sym])

            bp_report_text = None
            for attempt in range(1, retries + 1):
                print(f"[INFO] [BullPutSkill] 正在向 Gemini 請求 {sym} Bull Put 深度量化分析 (嘗試第 {attempt}/{retries} 次)...")
                try:
                    if call_gemini_for_skill:
                        res = call_gemini_for_skill(prompt=bp_prompt, api_key=api_key, min_chars=350, max_output_tokens=4096)
                        if res and len(res.strip()) > 250:
                            bp_report_text = res
                            print(f"[SUCCESS] [BullPutSkill] ✅ 第 {attempt} 次成功獲得 {sym} Bull Put 最佳量化評選報告！")
                            break
                except Exception as e:
                    print(f"[WARN] [BullPutSkill] 第 {attempt} 次呼叫異常: {e}")
                time.sleep(2)

            if not bp_report_text and top_bp_df is not None and not top_bp_df.empty:
                print(f"[WARN] [BullPutSkill] AI 分析未果，啟動 {sym} 規則型首位備援機制...")
                top_row = top_bp_df.iloc[0]
                bp_report_text = f"""#### 📌 【最佳 Bull Put 垂直價差推薦 (規則型備援)】
- **推薦標的代號 (Symbol)**: {top_row.get('Symbol', sym)}
- **標的現價 (Price)**: ${top_row.get('Price~', 0)}
- **建議策略**: Bull Put Spread (垂直賣權信用價差)
- **到期日 (Exp Date)**: {top_row.get('Exp Date', '')}
- **到期天數 (DTE)**: {top_row.get('DTE', 30)} 天
- **賣出下翼 (Short Put Leg 1)**: 履約價 ${top_row.get('Leg1 Strike', 0)} @ Bid ${top_row.get('Leg1 Bid', top_row.get('Bid1', 0))}
- **買入保護 (Long Put Leg 2)**: 履約價 ${top_row.get('Leg2 Strike', 0)} @ Ask ${top_row.get('Leg2 Ask', top_row.get('Ask2', 0))}
- **淨權利金收入 (Net Credit)**: ${top_row.get('Net Credit', top_row.get('Max Profit', 0.5))}
- **進場推薦理由與深度量化分析**:
  1. 依據 Barchart 官方篩選器，挑選 {sym} 安全緩衝最大與跌破虧損機率極低之最佳組合。
"""

            spread_info = parse_best_bull_put(bp_report_text, top_bp_df, default_symbol=sym)
            if not spread_info:
                print(f"[ERROR] 無法解析出 {sym} 有效之 Bull Put Spread 組合參數，略過此標的。")
                pipeline_results.append({
                    "symbol": sym,
                    "status": "parse_error",
                    "message": "無法解析合約參數"
                })
                continue

            save_bull_put_report_and_notify(bp_report_text, spread_info, idx=idx, total=total_targets)
            print(f"\n✨ 【{sym} Bull Put 精選價差組合】: {spread_info['symbol']} Exp={spread_info['exp_date_disp']} 賣出 ${spread_info['leg1_strike']} Put / 買入 ${spread_info['leg2_strike']} Put (預期收入: ${spread_info['net_credit']:.2f})")

            # (5) 步驟 5: Scale In Order Skill 連線 IBKR 執行下單與 Error 201 兩側衝突防禦
            print(f"\n⚡ 【步驟 5.{idx}】連線 IBKR 執行 {sym} Bull Put Spread 組合單 (整合 Scale In Order Skill)")
            print("-" * 70)

            if not skip_order:
                bp_order_res = execute_bull_put_scale_in_order(spread_info, dry_run=dry_run)
                order_status = bp_order_res.get('status', 'unknown')
                total_qty = bp_order_res.get('total_qty', 1)
                print(f"[INFO] {sym} Bull Put 下單處理結果: {order_status} - 總口數: {total_qty} 口")
                pipeline_results.append({
                    "symbol": sym,
                    "status": order_status,
                    "total_qty": total_qty,
                    "spread": spread_info,
                    "order_res": bp_order_res
                })
            else:
                print(f"[INFO] 已略過 {sym} 的 IBKR 下單步驟 (--skip-order)")
                pipeline_results.append({
                    "symbol": sym,
                    "status": "skipped_order",
                    "spread": spread_info
                })

        # ======================================================================
        # 彙總報告
        # ======================================================================
        print("\n" + "=" * 70)
        print(f"📊 【全體預選標的 Bull Put 垂直價差執行總結】 (共 {total_targets} 檔標的)")
        print("=" * 70)
        summary_lines = []
        for r in pipeline_results:
            sym = r["symbol"]
            st = r["status"]
            spread = r.get("spread")
            if spread:
                summary_line = f"  • {sym}: 下單狀態={st} | 到期日={spread['exp_date_disp']} | 賣出 ${spread['leg1_strike']}P / 買入 ${spread['leg2_strike']}P (Credit: ${spread['net_credit']:.2f})"
            else:
                summary_line = f"  • {sym}: 狀態={st} ({r.get('message', '')})"
            print(summary_line)
            summary_lines.append(summary_line)

        if not no_line and send_push_message and len(pipeline_results) > 1:
            final_push = f"🏁【Barchart UOA -> 全體 Bull Put 垂直價差執行彙總】\n" + "\n".join(summary_lines)
            try:
                send_push_message(final_push.strip())
            except Exception:
                pass

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    elapsed = time.time() - start_time
    print("\n" + "#" * 70)
    print(f"# ✅ Barchart UOA -> Bull Put 垂直價差自動化流程完成！(總耗時: {elapsed:.1f} 秒)")
    print("#" * 70 + "\n")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Barchart UOA 異常期權大單 AI 自動化選股與下單流水線 (Skills 架構版)")
    parser.add_argument("--headless", action="store_true", help="以無瀏覽器視窗模式執行")
    parser.add_argument("--dry-run", action="store_true", help="以模擬模式執行下單（預查市價並計算附屬單，不實際送單至 IBKR）")
    parser.add_argument("--skip-download", action="store_true", help="跳過下載步驟，使用現存 CSV")
    parser.add_argument("--skip-order", action="store_true", help="跳過向 IBKR 下單步驟")
    parser.add_argument("--retries", type=int, default=3, help="Gemini 請求最大重試次數")
    parser.add_argument("--no-line", action="store_true", help="不發送 LINE 通知")
    parser.add_argument("--no-sleep", action="store_true", help="執行完畢後直接退出，不常駐 sleep")
    args = parser.parse_args()

    run_uoa_pipeline(
        headless=args.headless,
        dry_run=args.dry_run,
        skip_download=args.skip_download,
        skip_order=args.skip_order,
        retries=args.retries,
        no_line=args.no_line,
    )

    if not args.no_sleep and not args.dry_run and sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
        try:
            time.sleep(60 * 60 * 20)
        except KeyboardInterrupt:
            print("\n[INFO] 使用者手動中斷常駐程序。")
