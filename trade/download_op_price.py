#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Barchart SPX Options Prices Downloader (trade/download_op_price.py)
================================================================================
本模組依據 Skills 架構設計，負責定期擷取 SPX 當日到期期權價格報價鏈：
  (1) 導航至官方 SPX 期權鏈頁面：
      • URL: https://www.barchart.com/stocks/quotes/$SPX/options?expiration={當天日期}-w
  (2) 每 10 分鐘自動執行一次下載，取得最新 SPX 官方 Options Prices CSV
  (3) 儲存至指定目標路徑：
      • Target Folder: trade/Barchart/Options_Prices/SPX_當天日期.csv
  (4) 支援命令列參數：
      --interval: 輪詢間隔秒數 (預設 600 秒 = 10 分鐘)
      --expiration: 自訂到期日代碼 (預設為 {當天日期}-w)
      --headless: 以無瀏覽器視窗模式執行
      --once: 僅執行單次下載即退出
      --no-line: 不發送 LINE 推播通知
================================================================================
"""

import os
import sys
import time
import glob
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
for p in [BASE_DIR, PROJECT_ROOT]:
    if p not in sys.path:
        sys.path.insert(0, p)

ENV_FILE = os.path.join(BASE_DIR, ".env")
BARCHART_DIR = os.path.join(BASE_DIR, "Barchart")
OPTIONS_PRICES_DIR = os.path.join(BARCHART_DIR, "Options_Prices")
HISTORY_DIR = os.path.join(OPTIONS_PRICES_DIR, "history")
STAGING_DIR = os.path.join(OPTIONS_PRICES_DIR, "_staging")
PROFILE_DIR = os.path.join(BARCHART_DIR, "chrome_profile")

os.makedirs(OPTIONS_PRICES_DIR, exist_ok=True)
os.makedirs(HISTORY_DIR, exist_ok=True)
os.makedirs(STAGING_DIR, exist_ok=True)

# 載入模組化 Skills
try:
    from trade.skills import (
        init_driver,
        dismiss_popups,
        login_if_needed,
        load_credentials_from_env,
        download_options_prices_csv,
        kill_stale_chrome_for_profile,
        PROFILE_DIR,
    )
except ImportError:
    from skills.download_skill import (
        init_driver,
        dismiss_popups,
        login_if_needed,
        load_credentials_from_env,
        download_options_prices_csv,
        kill_stale_chrome_for_profile,
        PROFILE_DIR,
    )

try:
    from notifier import send_push_message
except ImportError:
    send_push_message = None


def download_spx_once(driver, expiration=None, no_line=False):
    """
    執行單次 SPX Options Prices 下載任務：
      - 導航至 https://www.barchart.com/stocks/quotes/$SPX/options?expiration={當天日期}-w
      - 儲存至 trade/Barchart/Options_Prices/SPX_當天日期.csv
      - 同步備份歷史記錄至 history 目錄
    """
    now = datetime.datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    now_ts = now.strftime("%Y-%m-%d %H:%M:%S")
    time_tag = now.strftime("%Y%m%d_%H%M%S")

    exp_code = expiration or f"{today_str}-w"
    target_csv = os.path.join(OPTIONS_PRICES_DIR, f"SPX_{today_str}.csv")
    history_csv = os.path.join(HISTORY_DIR, f"SPX_{today_str}_{time_tag}.csv")

    print("\n" + "=" * 70)
    print(f"📥 【SPX 期權報價下載】執行時間: {now_ts}")
    print(f"   • 目標標的: $SPX")
    print(f"   • 到期日代碼: {exp_code}")
    print(f"   • 目標檔案: {target_csv}")
    print("=" * 70)

    try:
        result_file = download_options_prices_csv(
            driver=driver,
            symbol="$SPX",
            expiration=exp_code,
            target_dir=OPTIONS_PRICES_DIR
        )

        if result_file and os.path.exists(result_file) and os.path.getsize(result_file) > 100:
            file_size = os.path.getsize(result_file)

            # 備份一份帶有時間戳記的歷史檔案供比對分析
            try:
                shutil.copy2(result_file, history_csv)
            except Exception as copy_err:
                print(f"[WARN] [OpPrice] 歷史備份寫入異常: {copy_err}")

            print(f"[SUCCESS] [OpPrice] ✅ SPX Options Prices 下載成功！")
            print(f"   • 檔案路徑: {result_file}")
            print(f"   • 檔案大小: {file_size:,} 位元組")
            print(f"   • 歷史備份: {history_csv}")

            # 發送 LINE 推播通知
            if send_push_message and not no_line:
                msg = f"""📊【SPX Options Prices 下載完成】
🕒 時間：{now_ts}
🎯 標的：$SPX (Exp: {exp_code})
📁 檔案：SPX_{today_str}.csv ({file_size:,} bytes)
💾 路徑：trade/Barchart/Options_Prices/"""
                try:
                    send_push_message(msg.strip())
                except Exception:
                    pass

            return result_file
        else:
            print(f"[ERROR] [OpPrice] ❌ 下載失敗或檔案為空。")
            return None

    except Exception as e:
        print(f"[ERROR] [OpPrice] 下載過程發生異常: {e}")
        return None


def run_spx_download_service(
    interval=600,
    expiration=None,
    headless=False,
    once=False,
    no_line=False,
):
    """
    SPX Options Prices 排程下載常駐服務：
      - 預設每 10 分鐘 (600 秒) 循環下載一次
      - 自動管理瀏覽器生命週期與異常自我修復
    """
    print("\n" + "=" * 70)
    print("🌟 Barchart SPX Options Prices 定時下載服務啟動")
    print(f"   • 輪詢間隔: {interval} 秒 ({interval / 60:.1f} 分鐘)")
    print(f"   • 到期日模式: {expiration if expiration else '當日預設 ({當天日期}-w)'}")
    print(f"   • 瀏覽器模式: {'Headless (無視窗)' if headless else 'GUI (視窗可視)'}")
    print(f"   • 執行模式: {'單次執行 (--once)' if once else '持續定時排程'}")
    print("=" * 70)

    driver = None
    account, password = load_credentials_from_env(ENV_FILE)

    def get_or_create_driver():
        nonlocal driver
        if driver is not None:
            try:
                # 測試 driver 是否存活
                _ = driver.current_url
                return driver
            except Exception:
                print("[WARN] [OpPrice] 偵測到瀏覽器連線中斷，正在重新初始化 Chrome...")
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = None

        print("[INFO] [OpPrice] 正在啟動 Chrome Driver...")
        driver = init_driver(download_dir=STAGING_DIR, profile_dir=PROFILE_DIR, headless=headless)
        login_if_needed(driver, account, password)
        return driver

    iteration = 1
    try:
        while True:
            cycle_start = time.time()
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n🚀 【第 {iteration} 次下載循環】開始時間: {now_str}")

            try:
                active_driver = get_or_create_driver()
                download_spx_once(
                    driver=active_driver,
                    expiration=expiration,
                    no_line=no_line
                )
            except Exception as loop_err:
                print(f"[ERROR] [OpPrice] 第 {iteration} 次循環異常: {loop_err}")
                # 若發生嚴重例外，重設 driver 並清理孤兒 Chrome 行程供下次重試
                if driver:
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    driver = None
                kill_stale_chrome_for_profile(PROFILE_DIR)

            if once:
                print("\n[INFO] [OpPrice] 已依指令完成單次下載 (--once)，程序正常結束。")
                break

            iteration += 1
            next_run_dt = datetime.datetime.now() + datetime.timedelta(seconds=interval)
            next_run_str = next_run_dt.strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n⏳ [OpPrice] 等待 {interval} 秒 (10 分鐘)，預定下次下載時間: {next_run_str} ...")

            # 拆分為 1 秒小睡眠，確保能迅速響應 Ctrl+C 中斷
            sleep_start = time.time()
            while time.time() - sleep_start < interval:
                time.sleep(1)

    except KeyboardInterrupt:
        print("\n\n[INFO] [OpPrice] 接收到使用者中斷訊號 (Ctrl+C)，正在安全退出...")
    finally:
        if driver:
            try:
                driver.quit()
                print("[INFO] [OpPrice] Chrome 瀏覽器資源已安全釋放。")
            except Exception:
                pass

    print("\n🏁 SPX Options Prices 下載服務已停止。\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Barchart SPX Options Prices 定時自動下載模組 (每 10 分鐘下載當日報價 CSV)"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=600,
        help="下載輪詢間隔秒數 (預設: 600 秒 = 10 分鐘)"
    )
    parser.add_argument(
        "--expiration",
        type=str,
        default=None,
        help="自訂期權到期日參數 (例如: 2026-10-09-w，預設為當日 YYYY-MM-DD-w)"
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="以無瀏覽器視窗 (Headless) 模式執行"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="僅執行單次下載任務後退出"
    )
    parser.add_argument(
        "--no-line",
        action="store_true",
        help="不發送 LINE 推播通知"
    )

    args = parser.parse_args()

    run_spx_download_service(
        interval=args.interval,
        expiration=args.expiration,
        headless=args.headless,
        once=args.once,
        no_line=args.no_line,
    )
