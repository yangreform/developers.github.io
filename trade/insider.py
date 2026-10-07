#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Barchart Insider Trading & Options Flow Pipeline with Skills Architecture (trade/insider.py)
================================================================================
本模組為高階管線調度器 (High-level Pipeline Orchestrator)，導入模組化 Agentic Skills 設計：
  1. Memory Skill (trade/skills/memory_skill/):
     - 負責讀寫 trade/memory/selection_history.json
     - 追蹤歷史推薦標的，提供 14 天冷卻期 (Cooldown) 與永久黑名單過濾，杜絕重複推薦
  2. Insider Skill (trade/skills/insider_skill/):
     - 內部人交易數據清洗、真金白銀淨買入聚合、雙重防重複（資料層物理剔除 + Prompt 風控約束）
     - Gemini AI 深度量化分析，精選唯一最佳標的
  3. Flow Skill (trade/skills/flow_skill/):
     - 標的期權大單流向 (Options Flow) 清洗、過濾 0DTE 雜訊、權利金大單排序
     - Gemini AI 大單解讀，精選唯一最佳 CALL 合約規格 (Strike, Exp Date, Ref Price)
  4. IBKR 智能下單模組:
     - 預查即時市價，發送 Adaptive Patient 限價母單，掛出 2 倍停利與一半停損 Attached Orders
     - 即時結果推播至手機 LINE
  5. 期權大單無數據之 Bull Put 垂直價差自動備援流程:
     - (3") Download Skill (trade/skills/download_skill/):
           連線 Barchart 下載官方 Bull Put 垂直價差數據 (https://www.barchart.com/stocks/quotes/{symbol}/vertical-spreads/bull-put-spread)
     - (4") Gemini Helper (trade/skills/gemini_helper.py):
           清洗價差數據、過濾已到期合約、量化評選最佳 Bull Put 組合並推播 LINE
     - (5") Scale In Order Skill (trade/skills/scale_in_order_skill/):
           連線 IBKR 建立下翼賣權 (Short Put) 與保護賣權 (Long Put) 之 BAG 組合單，
           當加碼 (SELL) 遭遇 Error 201 兩側衝突時自動執行 4 大重組流程
================================================================================
"""

import os
import sys
import glob
import time
import math
import random
import datetime
import argparse
import re
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

# 載入現有基礎輔助模組
try:
    from trade.skills import (
        init_driver,
        dismiss_popups,
        login_if_needed,
        load_credentials_from_env,
        wait_for_file_download,
        download_bull_put_csv,
        download_stock_vertical_spread_csv,
        PROFILE_DIR,
        create_fast_ib_connection,
        load_env_settings,
        call_gemini_for_skill,
        load_gemini_api_key,
        SelectionMemory,
        InsiderSelectionSkill,
        OptionsFlowSkill,
        ScaleInOrderSkill,
    )
except ImportError:
    try:
        from skills.download_skill import (
            init_driver,
            dismiss_popups,
            login_if_needed,
            load_credentials_from_env,
            wait_for_file_download,
            download_bull_put_csv,
            download_stock_vertical_spread_csv,
            PROFILE_DIR,
        )
        from skills.ibkr_skill import create_fast_ib_connection, load_env_settings
        from skills.gemini_helper import call_gemini_for_skill, load_gemini_api_key
        from skills.memory_skill import SelectionMemory
        from skills.insider_skill import InsiderSelectionSkill
        from skills.flow_skill import OptionsFlowSkill
        from skills.scale_in_order_skill import ScaleInOrderSkill
    except ImportError as e:
        print(f"[WARN] 無法自 skills 載入下載或下單輔助模組: {e}")
        init_driver = None
        create_fast_ib_connection = None
        call_gemini_for_skill = None
        load_gemini_api_key = None
        SelectionMemory = None
        InsiderSelectionSkill = None
        OptionsFlowSkill = None
        ScaleInOrderSkill = None

try:
    from notifier import send_push_message
except ImportError:
    send_push_message = None

try:
    from ib_insync import IB, Option, Contract, ComboLeg, LimitOrder, StopOrder, TagValue
except ImportError:
    IB = None
    Option = None
    Contract = None
    ComboLeg = None

try:
    from skills.walk_up_skill import walk_up_limit_price, execute_walk_up_order
except ImportError:
    try:
        from trade.skills.walk_up_skill import walk_up_limit_price, execute_walk_up_order
    except ImportError:
        walk_up_limit_price = None
        execute_walk_up_order = None


# ==============================================================================
# 1. 網頁自動化下載輔助
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
        old_dir = os.path.join(target_dir, "old")
        if not candidates and os.path.exists(old_dir):
            candidates = glob.glob(os.path.join(old_dir, f"*{sym_clean.lower()}*options-flow*.csv"))
        if candidates:
            candidates.sort(key=os.path.getmtime, reverse=True)
            downloaded_file = candidates[0]
            print(f"[INFO] 沿用現存 {sym_clean} Options Flow CSV: {downloaded_file}")

    if downloaded_file and os.path.exists(downloaded_file):
        print(f"[SUCCESS] ✅ {sym_clean} Options Flow CSV 準備就緒: {downloaded_file} (大小: {os.path.getsize(downloaded_file):,} 位元組)")
        return downloaded_file

    raise RuntimeError(f"無法成功下載或取得 {sym_clean} Options Flow CSV 表格 (該標的可能無期權合約或無大單資料)。")


# ==============================================================================
# 2. 存檔與 LINE 推播通知輔助
# ==============================================================================
def save_report_and_notify(report_text, filename_prefix, line_title):
    """
    儲存分析報告並推播至手機 LINE
    """
    report_text = (report_text or "").strip()
    now = datetime.datetime.now()
    ts_str = now.strftime("%Y-%m-%d_%H%M%S")
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    archive_fname = f"{filename_prefix}_{ts_str}.txt"
    archive_path = os.path.join(REPORTS_DIR, archive_fname)

    content = f"==分析時間：{now_str}==\n{report_text}\n"

    with open(archive_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"[INFO] 報告已存檔至：{archive_path}")

    # 組裝推播訊息 (前 15 行重點)
    lines = report_text.split("\n")
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


def check_ibkr_contract_validity(symbol, exp_date, strike, right="C", host="127.0.0.1", port=4001):
    """
    快速向 IBKR 驗證期權合約是否存在且可交易。
    若 IBKR 連線成功且合約合法，回傳 (True, contract)。
    若 IBKR 回報無該合約定義 (No security definition) 或已過期，回傳 (False, None)。
    若 IBKR 未開啟或無法連線，回傳 (True, None) 允許離線流程通行。
    """
    ib = None
    try:
        from ib_insync import Option
        ib = create_fast_ib_connection(host=host, port=port, client_id=random.randint(9780, 9799))
        if not ib or not ib.isConnected():
            return True, None
    except Exception:
        return True, None

    try:
        contract = Option(symbol, exp_date, strike, right, "SMART")
        qualified = ib.qualifyContracts(contract)
        is_valid = bool(qualified and contract.conId and contract.conId > 0)
        return is_valid, contract if is_valid else None
    except Exception as e:
        print(f"[WARN] IBKR 合約驗證過程異常: {e}")
        return False, None
    finally:
        if ib and ib.isConnected():
            ib.disconnect()


def execute_ibkr_call_order(contract_info, dry_run=False):
    """
    連線 IBKR，建立 Call 期權合約，預查即時市價，
    送出 Adaptive Patient 限價買入母單，並掛出 Attached 訂單：
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
        # BUY CALL: Profit Taker 現價 2 倍，Stop Loss 現價 0.5 倍
        take_profit_price = round(current_price * 2.0, 2)
        stop_loss_price = max(0.01, round(current_price * 0.5, 2))

        print(f"  -> 附屬單規劃:")
        print(f"     • 🎯 Profit Taker (2倍停利單): ${take_profit_price:.2f}")
        print(f"     • 🛑 Stop Loss    (一半停損單): ${stop_loss_price:.2f}")

        # 組裝 Bracket Order (母單為市價 Adaptive Patient，附屬單為停利與停損)
        bracket = ib.bracketOrder(
            action="BUY",
            quantity=1,
            limitPrice=current_price,
            takeProfitPrice=take_profit_price,
            stopLossPrice=stop_loss_price,
        )

        # 母單設定為限價單並套用 Custom Walk-Up 步進修單
        bracket.parent.orderType = "LMT"
        bracket.parent.lmtPrice = current_price
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
            f"     委託: BUY 1口 @ Custom Walk-Up (起始限價 ${current_price:.2f})\n"
            f"     🎯 停利 (2倍): ${take_profit_price:.2f} | 🛑 停損 (一半): ${stop_loss_price:.2f}"
        )

        if not dry_run:
            parent_trade = None
            for o in order_list:
                tr = ib.placeOrder(contract, o)
                if o == bracket.parent:
                    parent_trade = tr
            ib.sleep(0.5)

            if walk_up_limit_price:
                print(f"⏳ [自適應步進修單] 啟動 Custom Walk-Up 步進調整 {contract.localSymbol} 括號母單...")
                is_filled = walk_up_limit_price(
                    ib=ib,
                    contract=contract,
                    order=bracket.parent,
                    current_mid=current_price,
                    max_slippage=0.15,
                    step=0.05,
                    step_time=3.0,
                    max_steps=3,
                    symbol=f"{contract.localSymbol}-Bracket",
                    trade=parent_trade
                )
                status = "Filled" if is_filled else (parent_trade.orderStatus.status if parent_trade else "Cancelled")
            else:
                ib.sleep(1)
                status = parent_trade.orderStatus.status if (parent_trade and hasattr(parent_trade, "orderStatus")) else "Submitted"
            print(f"  -> ✅ [已送出自適應步進委託至 IBKR] 母單狀態: {status}")
        else:
            print(f"  -> 🔍 [模擬模式 (Dry-Run)] 不實際送單至交易所")
            status = "DryRun"

        # 推播下單詳情報告至 LINE
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line_msg = f"""🚀【IBKR 內部人期權自動下單回報】
🕒 時間：{now_str}
⚙️ 模式：{'模擬測試 (Dry-Run)' if dry_run else '正式送單 (Live)'}
📋 合約：{contract.localSymbol} (conId: {contract.conId})
💵 參考現價：${current_price:.2f}
🎯 停利單 (2倍)：${take_profit_price:.2f}
🛑 停損單 (一半)：${stop_loss_price:.2f}
⚡ 委託方式：自適應步進修單 Custom Walk-Up
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
# 3.5 Bull Put Spread 垂直價差輔助與 Scale In Order 下單
# ==============================================================================
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


def clean_and_prepare_bull_put_data(csv_path, top_count=35, target_symbol=None):
    """
    清洗 Bull Put 數據，過濾已過期合約，自動結合歷史 old/ 候選組合。
    若指定 target_symbol (內部人推薦標的)，優先保留該標的之價差組合；
    依跌破虧損機率 (Loss Prob) 昇序與最大報酬率降序排序。
    """
    candidate_files = []
    if csv_path and os.path.exists(csv_path):
        candidate_files.append(csv_path)

    all_bp = (
        glob.glob(os.path.join(BARCHART_DIR, "*spread*.csv"))
        + glob.glob(os.path.join(BARCHART_DIR, "*bull-put*.csv"))
        + glob.glob(os.path.join(OLD_DIR, "*spread*.csv"))
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
        raise ValueError("未找到任何含有未過期合約之 Bull Put Spread 數據檔案。")

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

    # 若指定 target_symbol，優先看是否有該標的數據
    if target_symbol:
        sym_match = combined_bp[combined_bp["Symbol"].astype(str).str.upper() == target_symbol.upper()]
        if not sym_match.empty:
            print(f"[INFO] [BullPutSkill] 成功鎖定目標標的 {target_symbol} 之垂直價差數據 (共 {len(sym_match)} 筆)！")
            return sym_match.head(top_count).copy()

    top_df = combined_bp.head(top_count).copy()
    print(f"[INFO] [BullPutSkill] 數據清洗完成：篩選出 {len(top_df)} 筆未過期候選價差組合 (涵蓋標的: {list(top_df['Symbol'].unique())[:6]})")
    return top_df


def build_bull_put_prompt(top_df, target_symbol=None):
    """組裝專屬之 Bull Put Spread 提示詞"""
    display_cols = [
        c for c in [
            "Symbol", "Price~", "Exp Date", "DTE", "Leg1 Strike", "Leg1 Bid", "Bid1",
            "Leg2 Strike", "Leg2 Ask", "Ask2", "Net Credit", "Max Profit", "Max Loss",
            "Return", "Max Profit%", "Loss Prob", "BE (Buffer)", "BE%", "IV Rank"
        ] if c in top_df.columns
    ]
    table_str = top_df[display_cols].to_string(index=False)
    target_note = f"\n請注意：優先聚焦於內部人推薦標的 {target_symbol}，若清單中包含 {target_symbol} 且條件合適，請優先挑選其最佳價差組合。" if target_symbol else ""

    return f"""你是一名華爾街頂級期權量化經理人與波動率價差收租專家。
請根據以下來自 Barchart Bull Put Spread 官方篩選器的未過期候選價差組合清單，進行深度的多因子量化評選，挑選出「唯一最佳、勝率極高、安全緩衝極充足、性價比最高」的 1 組 Bull Put 垂直價差組合：{target_note}

======================================================================
【待評選之 Bull Put 垂直價差候選清單】
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
- **推薦標的代號 (Symbol)**: [標的代號，如 QQQ]
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


# ==============================================================================
# 4. 主控管線入口 (Pipeline Orchestrator with Skills)
# ==============================================================================
def run_insider_pipeline(
    headless=False,
    dry_run=False,
    skip_download=False,
    skip_order=False,
    symbol_override=None,
    cooldown_days=14,
    retries=3,
    #no_line=False,
    no_line=True,
):
    global send_push_message
    if no_line:
        send_push_message = None
    """
    以模組化 Agentic Skills 流程執行內部人選股、大單解讀與期權下單：
      Step 0: 初始化 Memory Skill，讀取歷史標的與排除名單
      Step 1: 下載/定位全市場內部人交易表格
      Step 2: 呼叫 Selection Skill 進行籌碼清洗、物理過濾、AI 深度選股
      Step 3: 下載/定位所選標的之期權大單流向表格
      Step 4: 呼叫 Flow Skill 進行大單清洗、AI 挑選最佳 CALL 合約
      Step 5: 記錄選中標的至 Memory Skill 記憶庫 (防止近期再次重複推薦)
      Step 6: 連線 IBKR 預查現價並發送 Adaptive Bracket 限價單
    """
    start_time = time.time()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("\n" + "=" * 70)
    print(f"🌟 Barchart 內部人交易 & 期權大單 Skills 自動化系統啟動 ({now_str})")
    print("=" * 70)

    # 1. 初始化 Memory Skill
    print("\n🧠 【步驟 0】初始化 Memory Skill (歷史記錄與排除過濾)")
    print("-" * 70)
    memory = SelectionMemory(cooldown_days=cooldown_days)
    excluded_symbols = memory.get_excluded_symbols()
    print(f"[MemorySkill] 當前冷卻天數設定: {cooldown_days} 天")
    print(f"[MemorySkill] 歷史排除/黑名單標的 ({len(excluded_symbols)} 檔): {sorted(excluded_symbols) if excluded_symbols else '無'}")

    driver = None
    insider_csv = None
    options_flow_csv = None
    selected_symbol = symbol_override
    company_name = ""
    net_buy_total = 0.0

    try:
        # 2. 步驟 1: 下載全市場內部人交易數據
        print("\n📥 【步驟 1】取得全市場內部人交易 CSV (Insider Trading Activity)")
        print("-" * 70)
        if not skip_download:
            account, password = load_credentials_from_env(ENV_FILE)
            driver = init_driver(download_dir=BARCHART_DIR, profile_dir=PROFILE_DIR, headless=headless)
            login_if_needed(driver, account, password)
            insider_csv = download_insider_activity_csv(driver, target_dir=BARCHART_DIR)
        else:
            candidates = glob.glob(os.path.join(BARCHART_DIR, "*insider*.csv"))
            if not candidates:
                candidates = glob.glob(os.path.join(OLD_DIR, "*insider*.csv"))
            if candidates:
                candidates.sort(key=os.path.getmtime, reverse=True)
                insider_csv = candidates[0]
                print(f"[INFO] (跳過下載) 使用現存內部人交易 CSV: {insider_csv}")
            else:
                raise FileNotFoundError("未找到任何現存的內部人交易 CSV 檔案。")

        # 3. 步驟 2: Selection Skill 內部人籌碼分析與 AI 選股
        print("\n🎯 【步驟 2】呼叫 Selection Skill 進行數據清洗、防重過濾與 AI 選股")
        print("-" * 70)
        api_key = load_gemini_api_key(ENV_FILE) if load_gemini_api_key else None
        selection_skill = InsiderSelectionSkill(api_key=api_key, env_path=ENV_FILE)

        if not selected_symbol:
            selection_result = selection_skill.select_best_symbol(
                csv_path=insider_csv,
                memory_skill=memory,
                max_retries=retries
            )
            selected_symbol = selection_result["symbol"]
            company_name = selection_result.get("company_name", "")
            net_buy_total = selection_result.get("insider_net_buy", 0.0)
            insider_report = selection_result["report_text"]
        else:
            print(f"[INFO] 使用手動指定標的: {selected_symbol}")
            insider_report = f"手動指定分析標的: {selected_symbol}"

        print(f"\n✨ 【Selection Skill 選定成果】: 標的 {selected_symbol} ({company_name}), 內部人淨增持: ${net_buy_total:,.2f}")
        save_report_and_notify(insider_report, "latest_insider", f"Barchart 內部人交易 AI 最佳推薦 ({selected_symbol})")

        # 4. 步驟 3 & 4: 下載與解讀期權大單 (具備候選標的自動備援機制)
        candidates_to_try = [selected_symbol]
        if not symbol_override and "top_candidates" in selection_result:
            for c in selection_result["top_candidates"]:
                if c not in candidates_to_try and c not in memory.get_excluded_symbols():
                    candidates_to_try.append(c)

        flow_skill = OptionsFlowSkill(api_key=api_key, env_path=ENV_FILE)
        active_symbol = selected_symbol
        contract_info = None
        call_report = None

        for sym in candidates_to_try:
            print(f"\n📥 【步驟 3】取得 {sym} 期權大單流向 CSV (Options Flow)")
            print("-" * 70)
            cur_csv = None
            try:
                if not skip_download:
                    if not driver:
                        account, password = load_credentials_from_env(ENV_FILE)
                        driver = init_driver(download_dir=BARCHART_DIR, profile_dir=PROFILE_DIR, headless=headless)
                        login_if_needed(driver, account, password)
                    cur_csv = download_options_flow_csv(driver, sym, target_dir=BARCHART_DIR)
                else:
                    candidates = glob.glob(os.path.join(BARCHART_DIR, f"*{sym.lower()}*options-flow*.csv"))
                    if not candidates:
                        candidates = glob.glob(os.path.join(OLD_DIR, f"*{sym.lower()}*options-flow*.csv"))
                    if candidates:
                        candidates.sort(key=os.path.getmtime, reverse=True)
                        cur_csv = candidates[0]
                        print(f"[INFO] (跳過下載) 使用現存 {sym} Options Flow CSV: {cur_csv}")
                    else:
                        print(f"[WARN] 未找到任何現存的 {sym} Options Flow CSV 檔案，嘗試下一候選標的...")
                        continue
            except Exception as dl_err:
                print(f"[WARN] 下載或尋找 {sym} Options Flow 異常: {dl_err}")
                continue

            if not cur_csv or not os.path.exists(cur_csv):
                continue

            print(f"\n🐋 【步驟 4】呼叫 Flow Skill 解讀 {sym} 期權大單並挑選最佳 CALL")
            print("-" * 70)
            flow_result = flow_skill.select_best_call(
                csv_path=cur_csv,
                symbol=sym,
                max_retries=retries
            )

            if flow_result.get("status") == "ok" and flow_result.get("contract"):
                c_info = flow_result["contract"]
                # 實時向 IBKR 驗證合約是否真實存在 (避免非標準履約價或無期權合約標的)
                cfg = load_env_settings(ENV_FILE)
                host = cfg.get("IB_HOST", "127.0.0.1")
                port = cfg.get("IB_PORT", 4001)
                is_valid, q_contract = check_ibkr_contract_validity(
                    symbol=c_info["symbol"],
                    exp_date=c_info["exp_date"],
                    strike=c_info["strike"],
                    right="C",
                    host=host,
                    port=port
                )
                if not is_valid:
                    print(f"[WARN] ⚠️ 標的 {sym} 之期權合約 ({c_info['symbol']} {c_info['exp_date']} C{c_info['strike']}) 在 IBKR 查無定義，合約無效！切換至下一位候選標的...")
                    continue

                active_symbol = sym
                contract_info = c_info
                call_report = flow_result["report_text"]
                con_id_str = f" (conId: {q_contract.conId})" if (q_contract and getattr(q_contract, "conId", 0)) else ""
                print(f"[SUCCESS] ✅ 成功鎖定具備主力 CALL 大單且通過 IBKR 驗證之標的: {active_symbol}{con_id_str}")
                break
            else:
                print(f"[WARN] 標的 {sym} 無任何 CALL 買權大單 (可能無期權合約或無大單)，嘗試下一候選標的...")

        # 關閉瀏覽器釋放資源
        if driver:
            try:
                driver.quit()
                driver = None
            except Exception:
                pass

        if contract_info:
            selected_symbol = active_symbol
            c_meta = selection_result.get("candidates_meta", {}).get(active_symbol, {})
            if c_meta:
                company_name = c_meta.get("Company_Name", company_name)
                net_buy_total = float(c_meta.get("Net_Buy_Total", net_buy_total))

            save_report_and_notify(call_report, "latest_insider_call", f"{selected_symbol} 期權大單 AI 最佳 CALL 推薦")
            print(f"\n✨ 【Flow Skill 精選合約】: {contract_info['symbol']} Strike=${contract_info['strike']} Exp={contract_info['exp_date']} RefPrice=${contract_info['ref_price']}")

            # 6. 步驟 5: 寫入 Memory Skill (記錄本次推薦與合約，納入冷卻名單)
            print("\n💾 【步驟 5】更新 Memory Skill 歷史推薦記憶庫")
            print("-" * 70)
            memory.record_selection(
                symbol=selected_symbol,
                company_name=company_name,
                net_buy_total=net_buy_total,
                contract=contract_info,
                reason=f"Selected by Skills Pipeline on {now_str}"
            )

            # 7. 步驟 6: 連線 IBKR 下單 (預查限價 + Bracket Adaptive Patient)
            print("\n⚡ 【步驟 6】連線 IBKR 執行自動化期權委託")
            print("-" * 70)
            if not skip_order:
                order_res = execute_ibkr_call_order(contract_info, dry_run=dry_run)
                print(f"[INFO] 下單處理結果: {order_res.get('status')} - {order_res.get('desc', order_res.get('message'))}")
            else:
                print("[INFO] 已略過 IBKR 下單步驟 (--skip-order)")
        else:
            msg = f"⚠️ 內部人推薦標的 ({', '.join(candidates_to_try)}) 均無足夠之期權 CALL 大單數據，已跳過期權下單程序。"
            print(f"\n{msg}")
            save_report_and_notify(msg, "latest_insider_call", f"{selected_symbol} 期權大單無數據提醒")
            # 仍記錄選股標的至記憶庫
            memory.record_selection(
                symbol=selected_symbol,
                company_name=company_name,
                net_buy_total=net_buy_total,
                contract={},
                status="no_options_flow",
                reason=f"Selected by Skills Pipeline on {now_str} (no options flow)"
            )

            # ==================================================================
            # 觸發用戶指定之 Bull Put 垂直價差備援流程 (3", 4", 5")
            # ==================================================================
            print("\n" + "=" * 70)
            print(f"🔄 【期權大單無數據備援】啟動 {selected_symbol} 之 Bull Put 垂直價差自動化策略")
            print("=" * 70)

            # (3") Download Skill: 自動連線 Barchart 下載官方 Bull Put 垂直價差篩選數據
            print(f"\n📥 【步驟 3\"】取得 {selected_symbol} Bull Put 垂直價差數據 (Download Skill)")
            print(f"    • URL: https://www.barchart.com/stocks/quotes/{selected_symbol}/vertical-spreads/bull-put-spread")
            print(f"    • Target Folder: {BARCHART_DIR}")
            print("-" * 70)

            bp_csv = None
            if not skip_download:
                bp_driver = None
                try:
                    account, password = load_credentials_from_env(ENV_FILE)
                    bp_driver = init_driver(download_dir=BARCHART_DIR, profile_dir=PROFILE_DIR, headless=headless)
                    login_if_needed(bp_driver, account, password)
                    bp_csv = download_stock_vertical_spread_csv(bp_driver, selected_symbol, target_dir=BARCHART_DIR)
                except Exception as bp_dl_err:
                    print(f"[WARN] [DownloadSkill] 下載 {selected_symbol} 垂直價差異常: {bp_dl_err}")
                finally:
                    if bp_driver:
                        try:
                            bp_driver.quit()
                        except Exception:
                            pass
            else:
                candidates = glob.glob(os.path.join(BARCHART_DIR, f"*{selected_symbol.lower()}*spread*.csv"))
                if not candidates:
                    candidates = glob.glob(os.path.join(BARCHART_DIR, "*bull-put*.csv"))
                if not candidates:
                    candidates = glob.glob(os.path.join(OLD_DIR, "*bull-put*.csv"))
                if candidates:
                    candidates.sort(key=os.path.getmtime, reverse=True)
                    bp_csv = candidates[0]
                    print(f"[INFO] (跳過下載) 使用現存價差 CSV: {bp_csv}")
                else:
                    print(f"[WARN] 未找到任何現存的 Bull Put Spread CSV 檔案。")

            # (4") Gemini Helper: 清洗價差數據、過濾已到期合約、按跌破機率與報酬率排序，精選唯一最佳 Bull Put Spread
            print(f"\n🎯 【步驟 4\"】清洗價差數據並呼叫 Gemini AI 精選唯一最佳 Bull Put Spread (Gemini Helper)")
            print("-" * 70)
            try:
                top_bp_df = clean_and_prepare_bull_put_data(bp_csv, target_symbol=selected_symbol)
            except Exception as clean_err:
                print(f"[WARN] [BullPutSkill] 清洗價差數據異常: {clean_err}，改用全域備援...")
                top_bp_df = clean_and_prepare_bull_put_data(None, target_symbol=None)

            bp_prompt = build_bull_put_prompt(top_bp_df, target_symbol=selected_symbol)

            bp_report_text = None
            for attempt in range(1, retries + 1):
                print(f"[INFO] [BullPutSkill] 正在向 Gemini 請求 Bull Put 深度量化分析 (嘗試第 {attempt}/{retries} 次)...")
                try:
                    if call_gemini_for_skill:
                        res = call_gemini_for_skill(prompt=bp_prompt, api_key=api_key, min_chars=500, max_output_tokens=80096)
                        if res and len(res.strip()) > 300:
                            bp_report_text = res
                            print(f"[SUCCESS] [BullPutSkill] ✅ 第 {attempt} 次成功獲得 Bull Put 最佳量化評選報告！")
                            break
                except Exception as e:
                    print(f"[WARN] [BullPutSkill] 第 {attempt} 次呼叫異常: {e}")
                time.sleep(2)

            if not bp_report_text and top_bp_df is not None and not top_bp_df.empty:
                print("[WARN] [BullPutSkill] AI 分析未果，啟動規則型首位備援機制...")
                top_row = top_bp_df.iloc[0]
                bp_report_text = f"""#### 📌 【最佳 Bull Put 垂直價差推薦 (規則型備援)】
- **推薦標的代號 (Symbol)**: {top_row.get('Symbol', selected_symbol)}
- **標的現價 (Price)**: ${top_row.get('Price~', 0)}
- **建議策略**: Bull Put Spread (垂直賣權信用價差)
- **到期日 (Exp Date)**: {top_row.get('Exp Date', '')}
- **到期天數 (DTE)**: {top_row.get('DTE', 30)} 天
- **賣出下翼 (Short Put Leg 1)**: 履約價 ${top_row.get('Leg1 Strike', 0)} @ Bid ${top_row.get('Leg1 Bid', top_row.get('Bid1', 0))}
- **買入保護 (Long Put Leg 2)**: 履約價 ${top_row.get('Leg2 Strike', 0)} @ Ask ${top_row.get('Leg2 Ask', top_row.get('Ask2', 0))}
- **淨權利金收入 (Net Credit)**: ${top_row.get('Net Credit', top_row.get('Max Profit', 0.5))}
- **進場推薦理由與深度量化分析**:
  1. 內部人推薦標的期權大單無足夠 CALL 數據，依風控規則自動轉向 Bull Put 垂直價差策略首選組合。
"""

            spread_info = parse_best_bull_put(bp_report_text, top_bp_df, default_symbol=selected_symbol)
            if spread_info:
                save_bull_put_report_and_notify(bp_report_text, spread_info)
                print(f"\n✨ 【Bull Put 精選價差組合】: {spread_info['symbol']} Exp={spread_info['exp_date_disp']} 賣出 ${spread_info['leg1_strike']} Put / 買入 ${spread_info['leg2_strike']} Put (預期收入: ${spread_info['net_credit']:.2f})")

                # 更新記憶庫記錄為 Bull Put 價差合約
                memory.record_selection(
                    symbol=spread_info['symbol'],
                    company_name=company_name,
                    net_buy_total=net_buy_total,
                    contract=spread_info,
                    status="bull_put_spread",
                    reason=f"Selected by Bull Put Fallback Pipeline on {now_str}"
                )

                # (5") Scale In Order Skill: 連線 IBKR 建立下翼賣權與保護賣權之 BAG 組合單，防禦 Error 201
                print(f"\n⚡ 【步驟 5\"】連線 IBKR 執行 Bull Put Spread 組合單 (整合 Scale In Order Skill)")
                print("-" * 70)
                if not skip_order:
                    bp_order_res = execute_bull_put_scale_in_order(spread_info, dry_run=dry_run)
                    print(f"[INFO] Bull Put 下單處理結果: {bp_order_res.get('status')} - 總口數: {bp_order_res.get('total_qty', 1)} 口")
                else:
                    print("[INFO] 已略過 IBKR 下單步驟 (--skip-order)")
            else:
                print(f"[WARN] 未能解析出有效之 Bull Put Spread 組合參數。")

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    elapsed = time.time() - start_time
    print("\n" + "#" * 70)
    print(f"# ✅ Barchart Skills 內部人 & 期權大單自動化流程完成！(總耗時: {elapsed:.1f} 秒)")
    print("#" * 70 + "\n")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Barchart 內部人交易與期權大單 AI 自動化選股與下單流水線 (Skills 架構版)")
    parser.add_argument("--headless", action="store_true", help="以無瀏覽器視窗模式執行")
    parser.add_argument("--dry-run", action="store_true", help="以模擬模式執行下單（預查市價並計算附屬單，不實際送單至 IBKR）")
    parser.add_argument("--skip-download", action="store_true", help="跳過下載步驟，使用現存 CSV")
    parser.add_argument("--skip-order", action="store_true", help="跳過向 IBKR 下單步驟")
    parser.add_argument("--symbol", type=str, default=None, help="手動指定分析標的（覆蓋 Gemini 自內部人選出之標的）")
    parser.add_argument("--cooldown-days", type=int, default=14, help="標的冷卻天數（在此天數內不重複推薦同一標的，預設: 14天）")
    parser.add_argument("--retries", type=int, default=3, help="Gemini 請求最大重試次數")
    parser.add_argument("--no-line", action="store_true", help="不發送 LINE 通知")
    parser.add_argument("--no-sleep", action="store_true", help="執行完畢後直接退出，不常駐 sleep")
    args = parser.parse_args()

    run_insider_pipeline(
        headless=args.headless,
        dry_run=args.dry_run,
        skip_download=args.skip_download,
        skip_order=args.skip_order,
        symbol_override=args.symbol,
        cooldown_days=args.cooldown_days,
        retries=args.retries,
        no_line=args.no_line,
    )

    import sys
    if not args.no_sleep and not args.dry_run and sys.stdin and hasattr(sys.stdin, 'isatty') and sys.stdin.isatty():
        try:
            time.sleep(60 * 60 * 20)
        except KeyboardInterrupt:
            print("\n[INFO] 使用者手動中斷常駐程序。")
