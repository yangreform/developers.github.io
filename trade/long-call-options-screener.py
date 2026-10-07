#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Barchart Long Call Options Screener Pipeline with Skills Architecture (trade/long-call-options-screener.py)
================================================================================
本模組為高階管線調度器 (High-level Pipeline Orchestrator)，導入模組化 Agentic Skills 設計：
  1. Memory Skill (trade/skills/memory_skill/):
     - 負責讀寫 trade/memory/long-call-options-screener_history.json
     - 追蹤歷史推薦標的，提供 14 天冷卻期 (Cooldown) 與永久黑名單過濾，杜絕重複推薦
  2. Selection Skill (trade/skills/long_call_skill/):
     - 前往 https://www.barchart.com/options/long-call-options-screener 下載官方 CSV
     - 真金白銀淨買入聚合、雙重防重複（資料層物理剔除 + Prompt 風控約束）
     - Gemini AI 深度量化分析，精選唯一最佳 Long Call 合約
  3. IBKR 智能下單模組:
     - 預查即時市價，發送 Adaptive Patient 市價母單，掛出 2 倍停利與一半停損 Attached Orders
     - 即時結果推播至手機 LINE
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
MEMORY_DIR = os.path.join(BASE_DIR, "memory")
HISTORY_FILE = os.path.join(MEMORY_DIR, "long-call-options-screener_history.json")

os.makedirs(BARCHART_DIR, exist_ok=True)
os.makedirs(OLD_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)
os.makedirs(MEMORY_DIR, exist_ok=True)

# 載入現有基礎輔助模組
try:
    from trade.skills import (
        init_driver,
        dismiss_popups,
        login_if_needed,
        load_credentials_from_env,
        wait_for_file_download,
        PROFILE_DIR,
        create_fast_ib_connection,
        load_env_settings,
    )
except ImportError:
    try:
        from skills.download_skill import (
            init_driver,
            dismiss_popups,
            login_if_needed,
            load_credentials_from_env,
            wait_for_file_download,
            PROFILE_DIR,
        )
        from skills.ibkr_skill import create_fast_ib_connection, load_env_settings
    except ImportError as e:
        print(f"[WARN] 無法自 skills 載入下載或下單輔助模組: {e}")
        init_driver = None
        create_fast_ib_connection = None

try:
    from trade.skills.gemini_helper import load_gemini_api_key
except Exception:
    try:
        from skills.gemini_helper import load_gemini_api_key
    except Exception:
        load_gemini_api_key = None

try:
    from notifier import send_push_message
except ImportError:
    send_push_message = None

try:
    from ib_insync import IB, Option, LimitOrder, StopOrder, TagValue
except ImportError:
    IB = None

try:
    from skills.walk_up_skill import walk_up_limit_price, execute_walk_up_order
except ImportError:
    try:
        from trade.skills.walk_up_skill import walk_up_limit_price, execute_walk_up_order
    except ImportError:
        walk_up_limit_price = None
        execute_walk_up_order = None

# 載入模組化 Skills
try:
    from trade.skills import SelectionMemory, LongCallSelectionSkill
except ImportError:
    try:
        from skills import SelectionMemory, LongCallSelectionSkill
    except ImportError:
        from skills.memory_skill import SelectionMemory
        from skills.long_call_skill import LongCallSelectionSkill


# ==============================================================================
# 1. 網頁自動化下載輔助
# ==============================================================================
def download_long_call_screener_csv(driver, target_dir=BARCHART_DIR):
    """
    導航至 https://www.barchart.com/options/long-call-options-screener
    點擊下載按鈕下載官方 CSV。
    """
    from selenium.webdriver.common.by import By

    url = "https://www.barchart.com/options/long-call-options-screener"
    print(f"\n[INFO] 正在導航至 Long Call Options Screener 頁面: {url} ...")
    driver.get(url)
    time.sleep(6)
    dismiss_popups(driver)

    before_snapshot = set(glob.glob(os.path.join(target_dir, "*.csv")))
    dl_buttons = driver.find_elements(
        By.CSS_SELECTOR,
        "a.toolbar-button.download, [data-bc-download-button], button.download"
    )

    downloaded_file = None
    if dl_buttons:
        btn = dl_buttons[0]
        print(f"[INFO] 找到下載按鈕 (text='{btn.text.strip()}'), 點擊下載 Long Call Screener CSV ...")
        driver.execute_script("arguments[0].click();", btn)
        downloaded_file = wait_for_file_download(target_dir, before_snapshot, timeout=20)

    if not downloaded_file or not os.path.exists(downloaded_file):
        candidates = glob.glob(os.path.join(target_dir, "*long-call*.csv"))
        old_dir = os.path.join(target_dir, "old")
        if not candidates and os.path.exists(old_dir):
            candidates = glob.glob(os.path.join(old_dir, "*long-call*.csv"))
        if candidates:
            candidates.sort(key=os.path.getmtime, reverse=True)
            downloaded_file = candidates[0]
            print(f"[INFO] 沿用現存 Long Call Screener CSV: {downloaded_file}")

    if downloaded_file and os.path.exists(downloaded_file):
        print(f"[SUCCESS] ✅ Long Call Screener CSV 準備就緒: {downloaded_file} (大小: {os.path.getsize(downloaded_file):,} 位元組)")
        return downloaded_file

    raise RuntimeError("無法成功下載或取得 Long Call Options Screener CSV 表格。")


# ==============================================================================
# 2. 存檔與 LINE 推播通知輔助
# ==============================================================================
def save_report_and_notify(report_text, filename_prefix="latest_long_call", line_title="Barchart Long Call 選擇權 AI 最佳推薦"):
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
    keywords = [
        "【", "📌", "推薦", "代號", "履約價", "到期日", "金額", "理由",
        "現價", "均價", "主力", "權利金", "Profit", "Stop", "Symbol",
        "Delta", "DTE", "IV", "Ask", "勝率"
    ]
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
# 3. IBKR 合約驗證與下單模組
# ==============================================================================
def check_ibkr_contract_validity(symbol, exp_date, strike, right="C", host="127.0.0.1", port=4001):
    """
    快速向 IBKR 驗證期權合約是否存在且可交易。
    若 IBKR 連線成功且合約合法，回傳 (True, contract)。
    若 IBKR 回報無該合約定義 (No security definition) 或已過期，回傳 (False, None)。
    若 IBKR 未開啟或無法連線，回傳 (True, None) 允許離線流程通行。
    """
    try:
        from ib_insync import Option
        ib = create_fast_ib_connection(host=host, port=port, client_id=random.randint(9780, 9799))
        if not ib or not ib.isConnected():
            return True, None
    except Exception as conn_err:
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
    送出 Adaptive Patient 市價買入母單，並掛出 Attached 訂單：
      - Profit Taker: 2倍現價 (round(current_price * 2.0, 2))
      - Stop Loss:    一半現價 (max(0.01, round(current_price * 0.5, 2)))
    下單完成後推播結果至手機 LINE。
    """
    symbol = contract_info["symbol"]
    strike = contract_info["strike"]
    exp_date = contract_info["exp_date"]
    ref_price = contract_info.get("ref_price", contract_info.get("ask", 1.0))

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
        print(f"  -> 預查即時參考價成功: ${current_price:.2f} (即時Bid: {ticker.bid}, Ask: {ticker.ask}, 參考價: {ref_price})")

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
        line_msg = f"""🚀【IBKR Long Call 選擇權自動下單回報】
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
# 4. 主控管線入口 (Pipeline Orchestrator with Skills)
# ==============================================================================
def run_long_call_pipeline(
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

    start_time = time.time()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("\n" + "=" * 70)
    print(f"🌟 Barchart Long Call 選擇權量化選股 & 下單系統啟動 ({now_str})")
    print("=" * 70)

    # 1. 步驟 0: 初始化 Memory Skill (讀取專屬歷史記錄與 14 天冷卻期過濾)
    print("\n🧠 【步驟 0】初始化 Memory Skill (歷史記錄與排除過濾)")
    print("-" * 70)
    memory = SelectionMemory(file_path=HISTORY_FILE, cooldown_days=cooldown_days)
    excluded_symbols = memory.get_excluded_symbols()
    print(f"[MemorySkill] 記憶庫檔案位置: {HISTORY_FILE}")
    print(f"[MemorySkill] 當前冷卻天數設定: {cooldown_days} 天")
    print(f"[MemorySkill] 歷史排除/黑名單標的 ({len(excluded_symbols)} 檔): {sorted(excluded_symbols) if excluded_symbols else '無'}")

    driver = None
    screener_csv = None
    contract_info = None
    report_text = ""

    try:
        # 2. 步驟 1: 下載 Long Call Options Screener 官方數據
        print("\n📥 【步驟 1】取得 Long Call Options Screener CSV 表格")
        print("-" * 70)
        if not skip_download:
            account, password = load_credentials_from_env(ENV_FILE)
            driver = init_driver(download_dir=BARCHART_DIR, profile_dir=PROFILE_DIR, headless=headless)
            login_if_needed(driver, account, password)
            screener_csv = download_long_call_screener_csv(driver, target_dir=BARCHART_DIR)
        else:
            candidates = glob.glob(os.path.join(BARCHART_DIR, "*long-call*.csv"))
            if not candidates:
                candidates = glob.glob(os.path.join(OLD_DIR, "*long-call*.csv"))
            if candidates:
                candidates.sort(key=os.path.getmtime, reverse=True)
                screener_csv = candidates[0]
                print(f"[INFO] (跳過下載) 使用現存 Long Call Screener CSV: {screener_csv}")
            else:
                raise FileNotFoundError("未找到任何現存的 Long Call Screener CSV 檔案。")

        # 3. 步驟 2: Selection Skill 籌碼清洗、雙重防重複過濾與 Gemini AI 量化分析
        print("\n🎯 【步驟 2】呼叫 Selection Skill 進行數據清洗、防重過濾與 AI 選約")
        print("-" * 70)
        api_key = load_gemini_api_key(ENV_FILE) if load_gemini_api_key else None
        selection_skill = LongCallSelectionSkill(api_key=api_key, env_path=ENV_FILE)

        selection_result = selection_skill.select_best_contract(
            csv_path=screener_csv,
            memory_skill=memory,
            symbol_override=symbol_override,
            max_retries=retries,
        )

        if selection_result.get("status") != "ok" or not selection_result.get("contract"):
            msg = f"⚠️ [Selection Skill] 未能成功挑選出合法 Long Call 合約: {selection_result.get('message', '未知原因')}"
            print(msg)
            save_report_and_notify(msg, "latest_long_call", "Long Call Screener 無合適標的提醒")
            return False

        contract_info = selection_result["contract"]
        report_text = selection_result["report_text"]
        top_candidates = selection_result.get("top_candidates", [])

        # 4. 步驟 3: 實時向 IBKR 驗證合約有效性 (具備自動備援替換機制)
        print("\n🔍 【步驟 3】驗證期權合約在 IBKR 是否有效且存在")
        print("-" * 70)
        cfg = load_env_settings(ENV_FILE)
        host = cfg.get("IB_HOST", "127.0.0.1")
        port = cfg.get("IB_PORT", 4001)

        is_valid, q_contract = check_ibkr_contract_validity(
            symbol=contract_info["symbol"],
            exp_date=contract_info["exp_date"],
            strike=contract_info["strike"],
            right="C",
            host=host,
            port=port,
        )

        if not is_valid:
            print(f"[WARN] ⚠️ 首選合約 ({contract_info['symbol']} {contract_info['exp_date']} C{contract_info['strike']}) 在 IBKR 查無定義！啟動備援候選名單輪詢...")
            found_backup = False
            for cand in top_candidates:
                cand_sym = cand["symbol"]
                if cand_sym in memory.get_excluded_symbols():
                    continue
                v, qc = check_ibkr_contract_validity(
                    symbol=cand["symbol"],
                    exp_date=cand["exp_date"],
                    strike=cand["strike"],
                    right="C",
                    host=host,
                    port=port,
                )
                if v:
                    contract_info = cand
                    q_contract = qc
                    found_backup = True
                    print(f"[SUCCESS] ✅ 成功切換至 IBKR 有效備援合約: {contract_info['symbol']} {contract_info['exp_date']} C{contract_info['strike']}")
                    break

            if not found_backup:
                print("[WARN] ⚠️ 候選清單中皆無可被 IBKR 驗證之有效合約 (可能合約已過期或查無定義)，將略過下單步驟。")
                contract_valid_for_order = False
            else:
                contract_valid_for_order = True
        else:
            contract_valid_for_order = True
            con_id_str = f" (conId: {q_contract.conId})" if (q_contract and getattr(q_contract, "conId", 0)) else ""
            print(f"[SUCCESS] ✅ 首選期權合約通過 IBKR 驗證: {contract_info['symbol']} {contract_info['exp_date']} C{contract_info['strike']}{con_id_str}")

        print(f"\n✨ 【精選 Long Call 合約】: {contract_info['symbol']} Strike=${contract_info['strike']} Exp={contract_info['exp_date']} Ask=${contract_info.get('ask', contract_info.get('ref_price'))} Delta={contract_info.get('delta')}")

        # 5. 步驟 4: 儲存報告並推播至 LINE
        print("\n📊 【步驟 4】儲存分析報告並推播至 LINE")
        print("-" * 70)
        save_report_and_notify(
            report_text,
            "latest_long_call",
            f"Barchart Long Call 選擇權 AI 最佳推薦 ({contract_info['symbol']})"
        )

        # 6. 步驟 5: 寫入 Memory Skill (記錄本次推薦與合約，納入 14 天冷卻名單)
        print("\n💾 【步驟 5】更新 Memory Skill 歷史推薦記憶庫")
        print("-" * 70)
        memory.record_selection(
            symbol=contract_info["symbol"],
            company_name=contract_info.get("company_name", ""),
            contract=contract_info,
            reason=f"Selected by Long Call Pipeline on {now_str}"
        )
        print(f"[SUCCESS] [MemorySkill] 已成功將標的 {contract_info['symbol']} 記錄至 {HISTORY_FILE} (納入 {cooldown_days} 天冷卻名單)")

        # 7. 步驟 6: 連線 IBKR 下單 (預查現價 + 市價 Adaptive Patient + 2倍TP + 0.5倍SL)
        print("\n⚡ 【步驟 6】連線 IBKR 執行自動化期權委託")
        print("-" * 70)
        if not skip_order and contract_valid_for_order:
            order_res = execute_ibkr_call_order(contract_info, dry_run=dry_run)
            print(f"[INFO] 下單處理結果: {order_res.get('status')} - {order_res.get('desc', order_res.get('message'))}")
        elif not contract_valid_for_order:
            print("[INFO] 因期權合約在 IBKR 驗證無效或已過期，已自動略過下單程序以確保交易安全。")
        else:
            print("[INFO] 已略過 IBKR 下單步驟 (--skip-order)")

    finally:
        if driver:
            try:
                driver.quit()
                driver = None
            except Exception:
                pass

    elapsed = time.time() - start_time
    print("\n" + "#" * 70)
    print(f"# ✅ Barchart Long Call 選擇權自動化流程完成！(總耗時: {elapsed:.1f} 秒)")
    print("#" * 70 + "\n")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Barchart Long Call 選擇權 AI 自動化量化選股與下單流水線 (Skills 架構版)")
    parser.add_argument("--headless", action="store_true", help="以無瀏覽器視窗模式執行")
    parser.add_argument("--dry-run", action="store_true", help="以模擬模式執行下單（預查市價並計算附屬單，不實際送單至 IBKR）")
    parser.add_argument("--skip-download", action="store_true", help="跳過下載步驟，使用現存 CSV")
    parser.add_argument("--skip-order", action="store_true", help="跳過向 IBKR 下單步驟")
    parser.add_argument("--symbol", type=str, default=None, help="手動指定分析標的（覆蓋篩選清單之首選標的）")
    parser.add_argument("--cooldown-days", type=int, default=14, help="標的冷卻天數（在此天數內不重複推薦同一標的，預設: 14天）")
    parser.add_argument("--retries", type=int, default=3, help="Gemini 請求最大重試次數")
    parser.add_argument("--no-line", action="store_true", help="不發送 LINE 通知")
    parser.add_argument("--no-sleep", action="store_true", help="執行完畢後直接退出，不常駐 sleep")
    args = parser.parse_args()

    run_long_call_pipeline(
        headless=args.headless,
        dry_run=args.dry_run,
        skip_download=args.skip_download,
        skip_order=args.skip_order,
        symbol_override=args.symbol,
        cooldown_days=args.cooldown_days,
        retries=args.retries,
        no_line=args.no_line,
    )

    if not args.no_sleep and not args.dry_run and sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
        try:
            time.sleep(60 * 60 * 20)
        except KeyboardInterrupt:
            print("\n[INFO] 使用者手動中斷常駐程序。")
