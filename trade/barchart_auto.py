#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Barchart Automated Pipeline (trade/barchart_auto.py)
================================================================================
自動化整合流程：
  1. 歸檔舊檔：先把 trade/Barchart 根目錄下所有現存的 .csv 檔案移到 trade/Barchart/old/
  2. 下載數據：執行與 trade/barchart_download.py 相同的機制，下載最新 3 個 CSV：
     - 個股異常期權 (Stocks Unusual Options Activity)
     - ETF 異常期權 (ETFs Unusual Options Activity)
     - Bull Put 垂直價差篩選 (Bull Put Spread Screener)
  3. 量化分析：執行與 trade/barchart_analysis.py 相同的分析邏輯：
     - 載入 trade/.env 中的 gemini_api
     - 傳入 3 個 CSV 數據給 Gemini 模型獲得詳細深度量化分析報告
     - 若取不到報告，自動重試三次
     - 報告存檔至 trade/Barchart/reports/ 供 Web 看板查看
     - 自動推播最後核心摘要訊息到手機 LINE
================================================================================
"""

import os
import sys
import glob
import time
import shutil
import datetime
import argparse

# 確保 Windows 主控台正確輸出 UTF-8 字符
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 目錄與路徑設定
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")
BARCHART_DIR = os.path.join(BASE_DIR, "Barchart")
OLD_DIR = os.path.join(BARCHART_DIR, "old")
REPORTS_DIR = os.path.join(BARCHART_DIR, "reports")

os.makedirs(BARCHART_DIR, exist_ok=True)
os.makedirs(OLD_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)

# 加入模組搜尋路徑
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# 載入現有模組函式
try:
    from barchart_download import run_download
except ImportError as e:
    print(f"[ERROR] 無法自 barchart_download 載入 run_download: {e}")
    run_download = None

try:
    from barchart_analysis import (
        load_gemini_api_key,
        get_latest_csv_file,
        clean_and_prepare_data,
        build_analysis_prompt,
        call_gemini_rest,
        save_analysis_to_text_file,
        send_line_notification,
        is_report_complete,
    )
except ImportError as e:
    print(f"[ERROR] 無法自 barchart_analysis 載入分析函式: {e}")
    load_gemini_api_key = None
    is_report_complete = None

try:
    from notifier import send_push_message
except ImportError:
    send_push_message = None

try:
    from trade.skills import UoaAnalysisSkill
except ImportError:
    try:
        from skills import UoaAnalysisSkill
    except ImportError:
        UoaAnalysisSkill = None


# ==============================================================================
# 1. 歸檔舊檔案：將 trade/Barchart 根目錄下所有 CSV 移至 trade/Barchart/old/
# ==============================================================================
def archive_existing_csvs(barchart_dir=BARCHART_DIR, old_dir=OLD_DIR):
    """
    將 trade/Barchart 根目錄下所有 .csv 移至 trade/Barchart/old/。
    若 old/ 中已有同名檔案，則在舊檔案檔名加上時間戳記保留，確保歷史數據完整。
    """
    os.makedirs(old_dir, exist_ok=True)
    csv_files = [f for f in glob.glob(os.path.join(barchart_dir, "*.csv")) if os.path.isfile(f)]

    print("\n" + "=" * 65)
    print("【步驟 1】清理與歸檔現有 Barchart CSV 舊檔")
    print("=" * 65)

    if not csv_files:
        print(f"[INFO] 目錄 {barchart_dir} 中目前無任何 .csv 舊檔需要歸檔。")
        return []

    print(f"[INFO] 偵測到 {len(csv_files)} 個舊 CSV 檔案，準備移至 {old_dir} ...")
    archived = []
    for src in sorted(csv_files):
        fname = os.path.basename(src)
        dest = os.path.join(old_dir, fname)

        # 若 old/ 中已存在同名檔案，則在舊檔名附加時間戳記保留
        if os.path.exists(dest):
            base, ext = os.path.splitext(fname)
            mtime_str = datetime.datetime.fromtimestamp(os.path.getmtime(dest)).strftime("%Y%m%d_%H%M%S")
            backup_name = f"{base}_{mtime_str}{ext}"
            backup_path = os.path.join(old_dir, backup_name)
            try:
                if not os.path.exists(backup_path):
                    os.rename(dest, backup_path)
                else:
                    os.remove(dest)
            except Exception as e:
                print(f"[WARN] 處理備份檔名異常: {e}")

        try:
            shutil.move(src, dest)
            print(f"  -> 📦 已歸檔: {fname} -> old/")
            archived.append(dest)
        except Exception as e:
            print(f"[ERROR] 移動檔案 {fname} 失敗: {e}")

    print(f"[SUCCESS] 步驟 1 完成：共移動 {len(archived)} 個舊檔案至 old/ 目錄。")
    return archived


# ==============================================================================
# 2. 下載最新 3 個 CSV 檔案
# ==============================================================================
def download_new_csvs(headless=False):
    """
    執行與 trade/barchart_download.py 相同的下載機制，依序下載：
      1. 個股異常選擇權活動 (Stocks UOA)
      2. ETF 異常選擇權活動 (ETFs UOA)
      3. Bull Put 垂直價差篩選 (Bull Put Spread)
    """
    print("\n" + "=" * 65)
    print("【步驟 2】下載 Barchart 最新 3 個 CSV 檔案")
    print("=" * 65)

    if not run_download:
        raise RuntimeError("無法執行下載流程：barchart_download 模組未正確載入。")

    downloaded = run_download(headless=headless, category="all")
    return downloaded


# ==============================================================================
# 3. 量化 AI 分析：呼叫 UoaAnalysisSkill，傳 3 個 CSV 獲得深度報告，重試三次，推播 LINE
# ==============================================================================
def analyze_and_report_with_retry(downloaded=None, max_retries=3):
    """
    載入 gemini_api，透過 UoaAnalysisSkill 傳送 3 個 CSV 獲得詳細深度量化分析報告。
    若取不到報告，就再試三次；取得成功後儲存完整文字檔並推播最後摘要到手機 LINE。
    """
    print("\n" + "=" * 65)
    print("【步驟 3】呼叫 UoaSkill 進行 Gemini AI 深度量化分析與手機推播")
    print("=" * 65)

    # 1. 載入 Gemini API Key
    api_key = load_gemini_api_key(ENV_FILE)
    if not api_key:
        err_msg = "[ERROR] 無法在 trade/.env 找到 gemini_api 設定，請確認 .env 設定。"
        print(err_msg)
        if send_push_message:
            try:
                send_push_message(f"❌【Barchart AI 執行失敗】{err_msg}")
            except Exception:
                pass
        raise ValueError(err_msg)

    print(f"[INFO] 成功自 trade/.env 載入 gemini_api (金鑰前綴: {api_key[:8]}...)")

    if UoaAnalysisSkill:
        uoa_skill = UoaAnalysisSkill(api_key=api_key)
        try:
            analysis_text, archive_fname = uoa_skill.run_pipeline(
                downloaded=downloaded,
                max_retries=max_retries,
                push_line=True,
            )
        except Exception as e:
            err_report = (
                f"❌【Barchart AI 分析異常】\n"
                f"已連續嘗試 {max_retries} 次均無法取得 Gemini 深度量化分析報告。\n"
                f"最後錯誤訊息: {e}\n"
                f"時間: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            print(f"\n[FATAL] {err_report}")
            if send_push_message:
                try:
                    send_push_message(err_report)
                    print(f"[INFO] 已發送失敗警示推播至手機 LINE")
                except Exception as pe:
                    print(f"[WARN] LINE 警示推播失敗: {pe}")
            raise RuntimeError(f"連續嘗試 {max_retries} 次均無法取得分析報告: {e}")

        # 終端機印出報告完整內容
        print("\n" + "=" * 65)
        print("=== 今日 AI 三大投資建議詳細報告 ===")
        print("=" * 65)
        print(analysis_text)
        print("=" * 65)
        return analysis_text, archive_fname

    # 2. 確認 3 個 CSV 檔案路徑
    stock_file = (downloaded.get("stocks") if downloaded else None) or get_latest_csv_file("unusual-stock-options-activity-*.csv")
    etf_file = (downloaded.get("etfs") if downloaded else None) or get_latest_csv_file("unusual-etf-options-activity-*.csv")
    bull_put_file = (downloaded.get("bull_put") if downloaded else None) or get_latest_csv_file("*bull-put*.csv")

    if not stock_file or not os.path.exists(stock_file):
        raise FileNotFoundError(f"[ERROR] 找不到個股 CSV 檔案 (stock_file={stock_file})")
    if not etf_file or not os.path.exists(etf_file):
        raise FileNotFoundError(f"[ERROR] 找不到 ETF CSV 檔案 (etf_file={etf_file})")

    print(f"[INFO] 準備傳入量化分析的 3 個 CSV 檔案:")
    print(f"  1. 個股 UOA CSV   : {os.path.basename(stock_file)} ({os.path.getsize(stock_file):,} bytes)")
    print(f"  2. ETF UOA CSV    : {os.path.basename(etf_file)} ({os.path.getsize(etf_file):,} bytes)")
    if bull_put_file and os.path.exists(bull_put_file):
        print(f"  3. Bull Put CSV   : {os.path.basename(bull_put_file)} ({os.path.getsize(bull_put_file):,} bytes)")
    else:
        print(f"  3. Bull Put CSV   : (未找到，將僅分析個股與 ETF)")
        bull_put_file = None

    # 3. 清洗與篩選數據
    data_str = clean_and_prepare_data(stock_file, etf_file, bull_put_file)

    # 4. 組裝量化 Prompt
    prompt = build_analysis_prompt(data_str)

    # 5. 呼叫 Gemini（取不到報告，就再試三次）
    analysis_text = None
    last_error = None

    for attempt in range(1, max_retries + 1):
        print(f"\n[INFO] 正在向 Gemini 請求深度量化分析報告 (嘗試第 {attempt}/{max_retries} 次)...")
        try:
            res = call_gemini_rest(prompt, api_key)
            complete_check = is_report_complete(res) if is_report_complete else (len(res.strip()) > 1000)
            if res and complete_check:
                analysis_text = res
                print(f"[SUCCESS] ✅ 成功取得 Gemini 深度量化分析報告 (第 {attempt} 次嘗試成功，長度: {len(analysis_text)} 字)！")
                break
            else:
                raise ValueError("Gemini 回傳內容為空、過短或缺少三大投資建議完整區塊")
        except Exception as e:
            last_error = e
            print(f"[WARN] ⚠️ 第 {attempt}/{max_retries} 次取得報告失敗: {e}")
            if attempt < max_retries:
                sleep_sec = attempt * 5
                print(f"[INFO] 等待 {sleep_sec} 秒後進行第 {attempt + 1} 次重試...")
                time.sleep(sleep_sec)

    # 若重試三次均失敗
    if not analysis_text:
        err_report = (
            f"❌【Barchart AI 分析異常】\n"
            f"已連續嘗試 {max_retries} 次均無法取得 Gemini 深度量化分析報告。\n"
            f"最後錯誤訊息: {last_error}\n"
            f"時間: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        print(f"\n[FATAL] {err_report}")
        if send_push_message:
            try:
                send_push_message(err_report)
                print(f"[INFO] 已發送失敗警示推播至手機 LINE")
            except Exception as pe:
                print(f"[WARN] LINE 警示推播失敗: {pe}")
        raise RuntimeError(f"連續嘗試 {max_retries} 次均無法取得分析報告: {last_error}")

    # 6. 儲存報告至 reports 目錄與 latest_ai_analysis.txt (提供 trade/q.py 看板顯示)
    archive_fname, archive_path = save_analysis_to_text_file(analysis_text)

    # 7. 終端機印出報告完整內容
    print("\n" + "=" * 65)
    print("=== 今日 AI 三大投資建議詳細報告 ===")
    print("=" * 65)
    print(analysis_text)
    print("=" * 65)

    # 8. 推播最後摘要訊息到手機 LINE
    send_line_notification(analysis_text, archive_fname)
    return analysis_text, archive_fname



# ==============================================================================
# 4. 主程式排程入口
# ==============================================================================
def run_barchart_auto(headless=False, max_retries=3, skip_download=False, skip_archive=False, skip_order=False, dry_run=False, no_line=False):
    global send_push_message
    if no_line:
        send_push_message = None
    """
    完整執行自動化 4 步驟流程：
      1. 歸檔舊檔 (archive_existing_csvs)
      2. 下載數據 (download_new_csvs)
      3. AI 量化分析 (analyze_and_report_with_retry)
      4. 自動向 IBKR 下單 (barchart_placeOrder)
    """
    start_time = time.time()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("\n" + "#" * 65)
    print(f"# Barchart 自動化下載、分析與推播系統啟動 [{now_str}]")
    print("#" * 65)

    # 步驟 1: 歸檔舊 CSV 檔案
    if not skip_archive:
        archive_existing_csvs()
    else:
        print("[INFO] 已跳過步驟 1 (歸檔舊檔案)")

    # 步驟 2: 下載最新 3 個 CSV
    downloaded = {}
    if not skip_download:
        downloaded = download_new_csvs(headless=headless)
    else:
        print("[INFO] 已跳過步驟 2 (下載最新 CSV)，將直接使用目錄中現有 CSV")

    # 步驟 3: 量化 AI 分析與推播 (失敗自動重試三次)
    analysis_text, archive_fname = analyze_and_report_with_retry(downloaded=downloaded, max_retries=max_retries)

    # 步驟 4: 根據 AI 建議自動向 IBKR 下單 (Adaptive Patient + Attached Profit Taker / Stop Loss)
    if not skip_order:
        print("\n" + "=" * 65)
        print("【步驟 4】調用 barchart_placeOrder 執行 IBKR 三大建議自動下單")
        print("=" * 65)
        try:
            from barchart_placeOrder import place_barchart_orders
            place_barchart_orders(dry_run=dry_run)
        except Exception as e:
            print(f"[ERROR] 執行自動下單階段發生異常: {e}")
    else:
        print("[INFO] 已跳過步驟 4 (自動向 IBKR 下單)")

    elapsed = time.time() - start_time
    print("\n" + "#" * 65)
    print(f"# ✅ Barchart 自動化流程全部順利完成！(總耗時: {elapsed:.1f} 秒)")
    print(f"# 📁 最新報告檔名: {archive_fname}")
    print("#" * 65 + "\n")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Barchart Options 自動化下載、量化分析、推播與 IBKR 自動下單整合腳本")
    parser.add_argument("--headless", action="store_true", help="以 Headless 無瀏覽器介面模式執行下載")
    parser.add_argument("--skip-download", action="store_true", help="跳過下載步驟，直接使用現有 CSV 進行分析")
    parser.add_argument("--skip-archive", action="store_true", help="跳過舊檔案歸檔步驟")
    parser.add_argument("--skip-order", action="store_true", help="跳過向 IBKR 下單步驟")
    parser.add_argument("--dry-run", action="store_true", help="以模擬模式執行下單（預查現價與計算附屬單，不實際送單至 IBKR）")
    parser.add_argument("--no-line", action="store_true", help="不發送 LINE 通知")
    parser.add_argument("--retries", type=int, default=3, help="Gemini 取得報告之最大重試次數 (預設: 3 次)")
    args = parser.parse_args()

    run_barchart_auto(
        headless=args.headless,
        max_retries=args.retries,
        skip_download=args.skip_download,
        skip_archive=args.skip_archive,
        skip_order=args.skip_order,
        dry_run=args.dry_run,
        no_line=args.no_line
    )
    import sys
    if not args.dry_run and sys.stdin and hasattr(sys.stdin, 'isatty') and sys.stdin.isatty():
        try:
            time.sleep(60*60*20)
        except KeyboardInterrupt:
            pass
