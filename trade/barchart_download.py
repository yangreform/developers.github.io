#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Barchart Unusual Options Activity Downloader
--------------------------------------------
Downloads CSVs for:
  1. Stocks: https://www.barchart.com/options/unusual-activity/stocks
  2. ETFs:   https://www.barchart.com/options/unusual-activity/etfs
Target Folder: trade/Barchart/
Download trigger: https://www.barchart.com/my/download (Toolbar Download button)
Fallback: Authenticated in-browser Core API export matching exact official CSV format.
"""

import os
import sys
import time
import glob
import json
import csv
import datetime
import argparse
from pathlib import Path

try:
    import undetected_chromedriver as uc
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
except ImportError:
    print("[ERROR] Please ensure undetected_chromedriver and selenium are installed.")
    sys.exit(1)

# Base directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")
TARGET_DIR = os.path.join(BASE_DIR, "Barchart")
PROFILE_DIR = os.path.join(TARGET_DIR, "chrome_profile")

os.makedirs(TARGET_DIR, exist_ok=True)
os.makedirs(PROFILE_DIR, exist_ok=True)


def load_credentials_from_env(env_path):
    """
    Parse Barchart credentials from .env supporting formats:
      barchart_accout:jacky@weishun.cc
      barchart_account=jacky@weishun.cc
      barchart_password:aaaa1111
      barchart_password=aaaa1111
    """
    account = None
    password = None

    if not os.path.exists(env_path):
        print(f"[WARN] .env file not found at {env_path}")
        return None, None

    with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            delims = [":", "="]
            for d in delims:
                if d in line:
                    k, v = line.split(d, 1)
                    k = k.strip().lower()
                    v = v.strip().strip('"').strip("'")
                    if k in ["barchart_accout", "barchart_account", "barchart_user", "barchart_email"]:
                        account = v
                    elif k in ["barchart_password", "barchart_pass", "barchart_pwd"]:
                        password = v
                    break

    return account, password


def init_driver(download_dir=TARGET_DIR, profile_dir=PROFILE_DIR, headless=False):
    """
    Initialize undetected Chrome driver with persistent profile and download settings.
    """
    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"--user-data-dir={profile_dir}")
    if headless:
        options.add_argument("--headless=new")

    driver = uc.Chrome(options=options)
    driver.set_page_load_timeout(45)

    # Configure Chrome CDP to allow automatic file downloads directly into target directory
    driver.execute_cdp_cmd("Page.setDownloadBehavior", {
        "behavior": "allow",
        "downloadPath": os.path.abspath(download_dir)
    })

    return driver


def dismiss_popups(driver):
    """
    Dismiss Cookiebot dialogs, newsletter subscription modals, or notification banners.
    """
    try:
        cb = driver.find_elements(By.ID, "CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll")
        if cb and cb[0].is_displayed():
            driver.execute_script("arguments[0].click();", cb[0])
            time.sleep(0.5)
    except Exception:
        pass

    try:
        close_buttons = driver.find_elements(By.CSS_SELECTOR, "a.close-reveal-modal, button.close, .reveal-modal .close")
        for btn in close_buttons:
            if btn.is_displayed():
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(0.5)
    except Exception:
        pass


def login_if_needed(driver, account, password):
    """
    Check if user is logged into Barchart. If not, log in using provided credentials.
    """
    print("[INFO] Checking Barchart login status...")
    driver.get("https://www.barchart.com/login")
    time.sleep(4)
    dismiss_popups(driver)

    if "login" not in driver.current_url.lower():
        print(f"[SUCCESS] Already logged in (Current URL: {driver.current_url})")
        return True

    if not account or not password:
        print("[WARN] No Barchart account/password found in .env, continuing as guest...")
        return False

    print(f"[INFO] Logging in with account: {account} ...")
    try:
        email_el = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input.form-field-login, input[name='email']"))
        )
        pass_el = driver.find_element(By.CSS_SELECTOR, "input.form-field-password, #login-page-form-password, input[name='password']")

        email_el.click()
        email_el.clear()
        email_el.send_keys(account)
        driver.execute_script("""
            arguments[0].dispatchEvent(new Event('input', { bubbles: true }));
            arguments[0].dispatchEvent(new Event('change', { bubbles: true }));
        """, email_el)

        pass_el.click()
        pass_el.clear()
        pass_el.send_keys(password)
        driver.execute_script("""
            arguments[0].dispatchEvent(new Event('input', { bubbles: true }));
            arguments[0].dispatchEvent(new Event('change', { bubbles: true }));
        """, pass_el)

        time.sleep(0.5)

        login_btn = driver.find_element(By.CSS_SELECTOR, "button.login-button, button[type='submit']")
        driver.execute_script("arguments[0].click();", login_btn)

        for _ in range(12):
            time.sleep(1)
            if "login" not in driver.current_url.lower():
                print(f"[SUCCESS] Successfully logged in! Redirected to: {driver.current_url}")
                return True

        print(f"[WARN] Login redirect did not complete. URL is: {driver.current_url}")
        return False

    except Exception as e:
        print(f"[ERROR] Failed during login attempt: {e}")
        return False


def wait_for_file_download(directory, before_snapshot, timeout=20):
    """
    Wait for a new .csv file to finish downloading.
    Handles existing files, modification time updates, and browser duplicate names like 'name (1).csv'.
    """
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(1)
        # Check for in-progress download
        crdownloads = glob.glob(os.path.join(directory, "*.crdownload"))
        if crdownloads:
            continue

        current_files = glob.glob(os.path.join(directory, "*.csv"))
        # Check for duplicate suffix files created by Chrome (e.g. * (1).csv)
        for f in current_files:
            fname = os.path.basename(f)
            if " (" in fname and f not in before_snapshot:
                # Rename into clean target name
                clean_name = re.sub(r"\s*\(\d+\)\.csv$", ".csv", fname)
                clean_path = os.path.join(directory, clean_name)
                try:
                    if os.path.exists(clean_path):
                        os.remove(clean_path)
                    os.rename(f, clean_path)
                    return clean_path
                except Exception:
                    return f

        # Check for newly added file
        new_files = [f for f in current_files if f not in before_snapshot and os.path.getsize(f) > 500]
        if new_files:
            return new_files[0]

        # Check for file modified after start time
        for f in current_files:
            if os.path.getmtime(f) > start and os.path.getsize(f) > 500:
                return f

    return None


def fetch_and_save_via_api(driver, category="stocks", target_dir=TARGET_DIR):
    """
    High-reliability fallback:
    Directly extracts complete dataset via Barchart Core API inside the authenticated browser context,
    with pagination (up to 1,000 items per page), formatting exactly like Barchart's official CSV export.
    """
    symbol_type = "stock" if category == "stocks" else "etf"
    print(f"[INFO] Fetching {category.upper()} unusual options activity via Core API...")

    all_rows = []
    page = 1
    total_records = None
    limit = 1000

    while True:
        js_code = f"""
        const url = '/proxies/core-api/v1/options/get?' + new URLSearchParams({{
            fields: 'symbol,baseSymbol,baseLastPrice,baseSymbolType,expirationDate,daysToExpiration,symbolType,strikePrice,moneyness,bidPrice,lastPrice,askPrice,volume,openInterest,volumeOpenInterestRatio,weightedImpliedVolatility,volatility,delta,tradeTime,symbolCode,hasOptions',
            orderBy: 'volumeOpenInterestRatio',
            orderDir: 'desc',
            baseSymbolTypes: '{symbol_type}',
            'between(volumeOpenInterestRatio,1.24,)': '',
            'between(lastPrice,.10,)': '',
            'between(volume,500,)': '',
            'between(openInterest,100,)': '',
            'in(exchange,(AMEX,NYSE,NASDAQ,INDEX-CBOE))': '',
            meta: 'field.shortName,field.type,field.description',
            limit: '{limit}',
            page: '{page}',
            hasOptions: 'true',
            raw: '1'
        }}).toString();
        const res = await fetch(url);
        return await res.json();
        """

        try:
            resp = driver.execute_script(f"return (async () => {{ {js_code} }})()")
        except Exception as e:
            print(f"[ERROR] API fetch script failed on page {page}: {e}")
            break

        if not isinstance(resp, dict) or "data" not in resp:
            print(f"[ERROR] Invalid API response on page {page}: {resp}")
            break

        page_data = resp.get("data", [])
        if total_records is None:
            total_records = resp.get("total", len(page_data))

        all_rows.extend(page_data)
        print(f"  -> Page {page}: fetched {len(page_data)} records (Accumulated: {len(all_rows)}/{total_records})")

        if len(all_rows) >= total_records or len(page_data) == 0:
            break

        page += 1
        time.sleep(0.5)

    if not all_rows:
        print(f"[ERROR] No data retrieved for {category.upper()}")
        return None

    today_str = datetime.date.today().strftime("%m-%d-%Y")
    now_dt = datetime.datetime.now()
    time_footer_str = now_dt.strftime("%m-%d-%Y %I:%M%p").lower() + " CDT"

    cat_name = "stock" if category == "stocks" else "etf"
    filename = f"unusual-{cat_name}-options-activity-{today_str}.csv"
    output_path = os.path.join(target_dir, filename)

    headers = [
        "Symbol", "Price~", "Exp Date", "DTE", "Type", "Strike", "Moneyness",
        "Bid", "Latest", "Ask", "Volume", "Open Int", "Vol/OI", "Imp Vol", "IV", "Delta", "Time"
    ]

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)

        for item in all_rows:
            raw = item.get("raw", {})
            sym = raw.get("baseSymbol", "")
            price = raw.get("baseLastPrice", "")
            exp_date = raw.get("expirationDate", "")
            dte = raw.get("daysToExpiration", "")
            stype = raw.get("symbolType", "")
            strike_val = raw.get("strikePrice")
            strike = f"{float(strike_val):.2f}" if strike_val is not None else ""

            m = raw.get("moneyness")
            if m is not None:
                moneyness = f"{float(m) * 100:+.2f}%"
            else:
                moneyness = ""

            bid = raw.get("bidPrice", "")
            latest = raw.get("lastPrice", "")
            ask = raw.get("askPrice", "")
            vol = raw.get("volume", "")
            oi = raw.get("openInterest", "")
            vol_oi = raw.get("volumeOpenInterestRatio", "")

            wiv = raw.get("weightedImpliedVolatility")
            imp_vol = f"{float(wiv) * 100:.2f}%" if wiv is not None else ""

            iv_val = raw.get("volatility")
            iv = f"{float(iv_val) * 100:.2f}%" if iv_val is not None else ""

            delta = raw.get("delta", "")

            tt = raw.get("tradeTime")
            if isinstance(tt, (int, float)):
                t_str = datetime.datetime.fromtimestamp(tt).strftime("%Y-%m-%d")
            else:
                t_str = str(tt) if tt else ""

            writer.writerow([
                sym, price, exp_date, dte, stype, strike, moneyness,
                bid, latest, ask, vol, oi, vol_oi, imp_vol, iv, delta, t_str
            ])

        # Write official Barchart footer line
        writer.writerow([f"Downloaded from Barchart.com as of {time_footer_str}"])

    print(f"[SUCCESS] Exported {len(all_rows)} records to {output_path}")
    return output_path


def download_page_csv(driver, page_url, category="stocks", target_dir=TARGET_DIR):
    """
    Navigate to page, click the download button (https://www.barchart.com/my/download),
    and if direct download doesn't trigger or is hit by membership limit, use the authenticated API fallback.
    """
    print(f"\n[INFO] --------------------------------------------------")
    print(f"[INFO] Processing {category.upper()}: {page_url}")
    driver.get(page_url)
    time.sleep(5)
    dismiss_popups(driver)

    # Snapshot existing files before download
    before_snapshot = set(glob.glob(os.path.join(target_dir, "*.csv")))

    # Locate download button (triggers https://www.barchart.com/my/download)
    dl_buttons = driver.find_elements(By.CSS_SELECTOR, "a.toolbar-button.download, [data-bc-download-button]")
    downloaded_file = None

    if dl_buttons:
        btn = dl_buttons[0]
        print(f"[INFO] Found download button: '{btn.text.strip()}'. Clicking...")
        driver.execute_script("arguments[0].click();", btn)

        # Wait for file download
        downloaded_file = wait_for_file_download(target_dir, before_snapshot, timeout=12)

    if downloaded_file and os.path.exists(downloaded_file):
        print(f"[SUCCESS] Native CSV Download Succeeded: {downloaded_file} (Size: {os.path.getsize(downloaded_file):,} bytes)")
        return downloaded_file

    print(f"[INFO] Native download was not triggered or limit reached; executing high-reliability API exporter...")
    fallback_file = fetch_and_save_via_api(driver, category=category, target_dir=target_dir)
    return fallback_file


def run_download(headless=False):
    """
    Main entry point to download both Stocks and ETFs options activity.
    """
    print("=" * 60)
    print(" Barchart Unusual Options Activity Downloader Starting")
    print(f" Output Directory: {TARGET_DIR}")
    print("=" * 60)

    account, password = load_credentials_from_env(ENV_FILE)
    if account:
        print(f"[INFO] Loaded Barchart credentials from .env: {account}")
    else:
        print("[WARN] No Barchart credentials found in .env")

    driver = init_driver(download_dir=TARGET_DIR, profile_dir=PROFILE_DIR, headless=headless)

    downloaded = {}
    try:
        # Step 1: Ensure login
        login_if_needed(driver, account, password)

        # Step 2: Download Stocks unusual activity
        stocks_url = "https://www.barchart.com/options/unusual-activity/stocks"
        stocks_file = download_page_csv(driver, stocks_url, category="stocks", target_dir=TARGET_DIR)
        downloaded["stocks"] = stocks_file

        # Step 3: Download ETFs unusual activity
        etfs_url = "https://www.barchart.com/options/unusual-activity/etfs"
        etfs_file = download_page_csv(driver, etfs_url, category="etfs", target_dir=TARGET_DIR)
        downloaded["etfs"] = etfs_file

    finally:
        print("[INFO] Closing browser session...")
        driver.quit()

    print("\n" + "=" * 60)
    print(" Download Summary:")
    for cat, file_path in downloaded.items():
        if file_path and os.path.exists(file_path):
            size = os.path.getsize(file_path)
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = sum(1 for _ in f)
            print(f"  - {cat.upper():<7}: {os.path.basename(file_path)} ({lines:,} lines, {size:,} bytes)")
        else:
            print(f"  - {cat.upper():<7}: FAILED")
    print("=" * 60)
    return downloaded


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Barchart Unusual Options Activity Downloader")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    args = parser.parse_args()

    run_download(headless=args.headless)
