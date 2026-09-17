#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Barchart Insider Trading & Options Flow Pipeline with Skills Architecture (trade/insider.py)
================================================================================
本模組為高階管線調度器 (High-level Pipeline Orchestrator)，導入模組化 Agentic Skills 設計：
  1. Memory Skill (trade/skills/memory_skill/):
     - 負責讀寫 trade/memory/selection_history.json
     - 追蹤歷史推薦標的，提供 14 天冷卻期 (Cooldown) 與永久黑名單過濾，杜絕重複推薦
  2. Selection Skill (trade/skills/selection_skill/):
     - 內部人交易數據清洗、真金白銀淨買入聚合、雙重防重複（資料層物理剔除 + Prompt 風控約束）
     - Gemini AI 深度量化分析，精選唯一最佳標的
  3. Flow Skill (trade/skills/flow_skill/):
     - 標的期權大單流向 (Options Flow) 清洗、過濾 0DTE 雜訊、權利金大單排序
     - Gemini AI 大單解讀，精選唯一最佳 CALL 合約規格 (Strike, Exp Date, Ref Price)
  4. IBKR 智能下單模組:
     - 預查即時市價，發送 Adaptive Patient 限價母單，掛出 2 倍停利與一半停損 Attached Orders
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

os.makedirs(BARCHART_DIR, exist_ok=True)
os.makedirs(OLD_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)

# 載入現有基礎輔助模組
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
    from trade.skills.gemini_helper import load_gemini_api_key
except Exception:
    try:
        from skills.gemini_helper import load_gemini_api_key
    except Exception:
        try:
            from barchart_analysis import load_gemini_api_key
        except Exception as e:
            print(f"[WARN] 無法載入 load_gemini_api_key: {e}")
            load_gemini_api_key = None

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

# 載入三大模組化 Skills
try:
    from trade.skills import SelectionMemory, InsiderSelectionSkill, OptionsFlowSkill
except ImportError:
    from skills.memory_skill import SelectionMemory
    from skills.selection_skill import InsiderSelectionSkill
    from skills.flow_skill import OptionsFlowSkill


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
        if not candidates:
            candidates = glob.glob(os.path.join(target_dir, "*options-flow*.csv"))
        if candidates:
            candidates.sort(key=os.path.getmtime, reverse=True)
            downloaded_file = candidates[0]
            print(f"[INFO] 沿用現存 Options Flow CSV: {downloaded_file}")

    if downloaded_file and os.path.exists(downloaded_file):
        print(f"[SUCCESS] ✅ {sym_clean} Options Flow CSV 準備就緒: {downloaded_file} (大小: {os.path.getsize(downloaded_file):,} 位元組)")
        return downloaded_file

    raise RuntimeError(f"無法成功下載或取得 {sym_clean} Options Flow CSV 表格。")


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


# ==============================================================================
# 3. IBKR 下單：預查現價 + Adaptive Patient + 2倍停利 / 一半停損
# ==============================================================================
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
    no_line=False,
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
                    if not candidates:
                        candidates = glob.glob(os.path.join(BARCHART_DIR, "*options-flow*.csv"))
                    if not candidates:
                        candidates = glob.glob(os.path.join(OLD_DIR, "*options-flow*.csv"))
                    if candidates:
                        candidates.sort(key=os.path.getmtime, reverse=True)
                        cur_csv = candidates[0]
                        print(f"[INFO] (跳過下載) 使用現存 Options Flow CSV: {cur_csv}")
                    else:
                        print(f"[WARN] 未找到任何現存的 {sym} Options Flow CSV 檔案。")
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
                active_symbol = sym
                contract_info = flow_result["contract"]
                call_report = flow_result["report_text"]
                print(f"[SUCCESS] ✅ 成功鎖定具備主力 CALL 大單之標的: {active_symbol}")
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
