#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Barchart Download Skill (trade/skills/download_skill/download.py)
================================================================================
職責：
  1. 初始化 undetected_chromedriver 與自動登入 Barchart
  2. 下載個股與 ETF 異常期權交易數據 (UOA)
  3. 下載 Bull Put 垂直價差篩選器官方 CSV (支援 DOM / API 備援)
  4. 下載指定標的之期權大單流向 CSV (Options Flow)
  5. 下載內部人交易與 Long Call 篩選器 CSV 表格
================================================================================
"""

import os
import sys
import time
import glob
import json
import csv
import re
import datetime

try:
    import undetected_chromedriver as uc
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait, Select
    from selenium.webdriver.support import expected_conditions as EC
except ImportError:
    uc = None
    By = None
    WebDriverWait = None
    Select = None
    EC = None

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ENV_FILE = os.path.join(BASE_DIR, ".env")
TARGET_DIR = os.path.join(BASE_DIR, "Barchart")
PROFILE_DIR = os.path.join(TARGET_DIR, "chrome_profile")

os.makedirs(TARGET_DIR, exist_ok=True)
os.makedirs(PROFILE_DIR, exist_ok=True)


def get_chrome_major_version():
    """檢測 Windows 本機安裝之 Chrome 主版本號，避免驅動程式版本不符"""
    try:
        import winreg
        for key_path in (r'Software\Google\Chrome\BLBeacon', r'Software\Wow6432Node\Google\Chrome\BLBeacon'):
            for hkey in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hkey, key_path) as k:
                        v, _ = winreg.QueryValueEx(k, 'version')
                        return int(v.split('.')[0])
                except Exception:
                    pass
    except Exception:
        pass
    return None


def load_credentials_from_env(env_path=ENV_FILE):
    """自 .env 讀取 Barchart 登入帳密"""
    account = None
    password = None

    if not os.path.exists(env_path):
        return None, None

    with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            for d in [":", "="]:
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
    """啟動 undetected Chrome Driver 並設定自動下載目錄"""
    if uc is None:
        raise RuntimeError("請確保 undetected_chromedriver 與 selenium 已安裝。")

    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"--user-data-dir={profile_dir}")

    if headless:
        options.add_argument("--headless=new")

    kwargs = {"options": options}
    major_v = get_chrome_major_version()
    if major_v:
        kwargs["version_main"] = major_v

    try:
        driver = uc.Chrome(**kwargs)
    except Exception as e:
        print(f"[WARN] [DownloadSkill] Chrome version_main={major_v} 啟動異常: {e}，改用預設 uc.Chrome")
        driver = uc.Chrome(options=options)

    driver.set_page_load_timeout(45)

    driver.execute_cdp_cmd("Page.setDownloadBehavior", {
        "behavior": "allow",
        "downloadPath": os.path.abspath(download_dir)
    })

    return driver


def dismiss_popups(driver):
    """關閉 Cookie 同意對話框或活動視窗"""
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


def login_if_needed(driver, account=None, password=None):
    """若尚未登入則執行登入"""
    if account is None and password is None:
        account, password = load_credentials_from_env()

    print("[INFO] [DownloadSkill] 檢查 Barchart 登入狀態...")
    driver.get("https://www.barchart.com/login")
    time.sleep(4)
    dismiss_popups(driver)

    if "login" not in driver.current_url.lower():
        print(f"[SUCCESS] [DownloadSkill] 目前已為登入狀態 (URL: {driver.current_url})")
        return True

    if not account or not password:
        print("[WARN] [DownloadSkill] .env 未設定 Barchart 帳密，以訪客身份繼續...")
        return False

    print(f"[INFO] [DownloadSkill] 正在登入帳號: {account} ...")
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
                print(f"[SUCCESS] [DownloadSkill] 登入成功！已跳轉至: {driver.current_url}")
                return True

        print(f"[WARN] [DownloadSkill] 登入跳轉未完成，當前 URL: {driver.current_url}")
        return False
    except Exception as e:
        print(f"[ERROR] [DownloadSkill] 登入過程發生異常: {e}")
        return False


def wait_for_file_download(directory, before_snapshot, timeout=20):
    """等待 .csv 下載完成，處理快照、檔案重命名與多重序號"""
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(1)
        crdownloads = glob.glob(os.path.join(directory, "*.crdownload"))
        if crdownloads:
            continue

        current_files = glob.glob(os.path.join(directory, "*.csv"))
        for f in current_files:
            fname = os.path.basename(f)
            if " (" in fname and f not in before_snapshot:
                clean_name = re.sub(r"\s*\(\d+\)\.csv$", ".csv", fname)
                clean_path = os.path.join(directory, clean_name)
                try:
                    if os.path.exists(clean_path):
                        os.remove(clean_path)
                    os.rename(f, clean_path)
                    return clean_path
                except Exception:
                    return f

        new_files = [f for f in current_files if f not in before_snapshot and os.path.getsize(f) > 500]
        if new_files:
            return new_files[0]

        for f in current_files:
            if os.path.getmtime(f) > start and os.path.getsize(f) > 500:
                return f

    return None


def scrape_and_save_table_dom(driver, category="bull_put", target_dir=TARGET_DIR):
    """DOM 備援抓取機制"""
    try:
        tables = driver.find_elements(By.CSS_SELECTOR, "div.bc-table-scrollable table, table.bc-data-grid, table")
        if not tables:
            return None

        table = tables[0]
        rows = table.find_elements(By.TAG_NAME, "tr")
        if not rows:
            return None

        today_str = datetime.date.today().strftime("%m-%d-%Y")
        filename = f"bull-put-spread-option-screener-advanced-bull-put-screener-{today_str}.csv"
        output_path = os.path.join(target_dir, filename)

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            for row in rows:
                cols = row.find_elements(By.CSS_SELECTOR, "th, td")
                if cols:
                    row_text = [c.text.strip().replace("\n", " ") for c in cols]
                    writer.writerow(row_text)

        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            print(f"[SUCCESS] [DownloadSkill] DOM 成功抓取 {len(rows)} 列至 {output_path}")
            return output_path
    except Exception as e:
        print(f"[ERROR] [DownloadSkill] DOM 抓取備援異常: {e}")
    return None


def fetch_and_save_via_api(driver, category="stocks", target_dir=TARGET_DIR):
    """Core API 備援抓取機制"""
    symbol_type = "stock" if category == "stocks" else "etf"
    print(f"[INFO] [DownloadSkill] 使用 Core API 抓取 {category.upper()} 異常期權數據...")

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
            print(f"[ERROR] [DownloadSkill] API 腳本在第 {page} 頁執行失敗: {e}")
            break

        if not isinstance(resp, dict) or "data" not in resp:
            break

        page_data = resp.get("data", [])
        if total_records is None:
            total_records = resp.get("total", len(page_data))

        all_rows.extend(page_data)
        if len(all_rows) >= total_records or len(page_data) == 0:
            break

        page += 1
        time.sleep(0.5)

    if not all_rows:
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
            moneyness = f"{float(m) * 100:+.2f}%" if m is not None else ""
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
        writer.writerow([f"Downloaded from Barchart.com as of {time_footer_str}"])

    print(f"[SUCCESS] [DownloadSkill] API 導出 {len(all_rows)} 筆數據至 {output_path}")
    return output_path


def download_page_csv(driver, page_url, category="stocks", target_dir=TARGET_DIR):
    """導航至指定頁面並點擊官方下載按鈕，若失敗則啟動備援機制"""
    print(f"\n[INFO] [DownloadSkill] 處理 {category.upper()}: {page_url}")
    driver.get(page_url)
    time.sleep(7)
    dismiss_popups(driver)

    before_snapshot = set(glob.glob(os.path.join(target_dir, "*.csv")))
    dl_buttons = driver.find_elements(By.CSS_SELECTOR, "a.toolbar-button.download, [data-bc-download-button]")
    downloaded_file = None

    if dl_buttons:
        btn = dl_buttons[0]
        print(f"[INFO] [DownloadSkill] 找到下載按鈕 (text='{btn.text.strip()}'), 點擊下載...")
        driver.execute_script("arguments[0].click();", btn)
        downloaded_file = wait_for_file_download(target_dir, before_snapshot, timeout=18)

    if downloaded_file and os.path.exists(downloaded_file):
        print(f"[SUCCESS] [DownloadSkill] 成功下載官方 CSV: {downloaded_file} ({os.path.getsize(downloaded_file):,} 位元組)")
        return downloaded_file

    print(f"[INFO] [DownloadSkill] 原生下載未觸發，切換至備援機制...")
    if category == "bull_put":
        return scrape_and_save_table_dom(driver, category=category, target_dir=target_dir)
    else:
        return fetch_and_save_via_api(driver, category=category, target_dir=target_dir)


def download_uoa_stocks_csv(driver, target_dir=TARGET_DIR):
    """下載個股異常期權數據 (Stocks UOA)"""
    url = "https://www.barchart.com/options/unusual-activity/stocks"
    return download_page_csv(driver, url, category="stocks", target_dir=target_dir)


def download_uoa_etfs_csv(driver, target_dir=TARGET_DIR):
    """下載 ETF 異常期權數據 (ETFs UOA)"""
    url = "https://www.barchart.com/options/unusual-activity/etfs"
    return download_page_csv(driver, url, category="etfs", target_dir=target_dir)


def download_bull_put_csv(driver, target_dir=TARGET_DIR):
    """下載 Bull Put 垂直價差篩選數據 (Bull Put Spread)"""
    url = "https://www.barchart.com/options/vertical-spreads/bull-put-spread?popularScreener=221982&setPopularScreener=true&orderBy=lossProbability&orderDir=asc"
    return download_page_csv(driver, url, category="bull_put", target_dir=target_dir)


def download_options_flow_csv(driver, symbol, target_dir=TARGET_DIR):
    """下載指定標的之期權大單流向數據 (Options Flow)"""
    sym_clean = symbol.strip().upper()
    url = f"https://www.barchart.com/stocks/quotes/{sym_clean}/options-flow"
    print(f"\n[INFO] [DownloadSkill] 導航至 {sym_clean} Options Flow 頁面: {url} ...")
    driver.get(url)
    time.sleep(6)
    dismiss_popups(driver)

    before_snapshot = set(glob.glob(os.path.join(target_dir, "*.csv")))
    dl_buttons = driver.find_elements(By.CSS_SELECTOR, "a.toolbar-button.download, [data-bc-download-button], button.download")

    downloaded_file = None
    if dl_buttons:
        btn = dl_buttons[0]
        print(f"[INFO] [DownloadSkill] 找到下載按鈕 (text='{btn.text.strip()}'), 點擊下載 {sym_clean} Options Flow CSV ...")
        driver.execute_script("arguments[0].click();", btn)
        downloaded_file = wait_for_file_download(target_dir, before_snapshot, timeout=18)

    if not downloaded_file or not os.path.exists(downloaded_file):
        candidates = glob.glob(os.path.join(target_dir, f"{sym_clean.lower()}-options-flow*.csv"))
        old_dir = os.path.join(target_dir, "old")
        if not candidates and os.path.exists(old_dir):
            candidates = glob.glob(os.path.join(old_dir, f"{sym_clean.lower()}-options-flow*.csv"))
        if candidates:
            candidates.sort(key=os.path.getmtime, reverse=True)
            downloaded_file = candidates[0]
            print(f"[INFO] [DownloadSkill] 沿用現存 {sym_clean} Options Flow CSV: {downloaded_file}")

    if downloaded_file and os.path.exists(downloaded_file):
        print(f"[SUCCESS] [DownloadSkill] {sym_clean} Options Flow CSV 準備就緒: {downloaded_file} ({os.path.getsize(downloaded_file):,} 位元組)")
        return downloaded_file

    raise RuntimeError(f"無法下載或取得 {sym_clean} Options Flow CSV。")


def download_stock_vertical_spread_csv(driver, symbol, target_dir=TARGET_DIR, prefer_monthly=True):
    """
    下載指定標的之官方 Bull Put 垂直價差數據：
      - 連線至:
        https://www.barchart.com/stocks/quotes/{symbol}/vertical-spreads/bull-put-spread
      - 於 Expiration 下拉選單中切換至月到期 (Monthly 'm')，避開週到期 ('w') 以獲取更多 OI 與流動性
      - 點擊官方下載按鈕下載 CSV 並存至 target_dir (trade/Barchart/)
      - 若無下載按鈕或檔案為空，且本地亦無該標的專屬 bull-put CSV，回傳 None (放棄該預選標的，不進行全域備援)
    """
    sym_clean = symbol.strip().upper()
    url = f"https://www.barchart.com/stocks/quotes/{sym_clean}/vertical-spreads/bull-put-spread"

    print(f"\n[INFO] [DownloadSkill] 導航至 {sym_clean} Bull Put 垂直價差頁面: {url} ...")
    try:
        driver.get(url)
        time.sleep(7)
        dismiss_popups(driver)

        # 切換 Expiration 到期日下拉選單：優先選擇月到期 (Monthly 'm')，避開週到期 ('w')
        if prefer_monthly:
            try:
                select_selectors = [
                    "bc-filter-experation-drop-down select",
                    "div[data-ref='optSpreads'] select",
                    "div.expiration-name select",
                    "select[aria-label='set expiration date']",
                    "select[data-event-name='onExpirationDateChanged']",
                ]
                select_el = None
                for sel_css in select_selectors:
                    found = driver.find_elements(By.CSS_SELECTOR, sel_css)
                    if found:
                        select_el = found[0]
                        break

                if select_el:
                    sel = Select(select_el)
                    monthly_opt = None
                    for opt in sel.options:
                        val = (opt.get_attribute("value") or "").strip().lower()
                        txt = (opt.text or "").strip().lower()
                        if val.endswith("-m") or "(m)" in txt or "monthly" in txt or "-m" in val:
                            monthly_opt = opt
                            break

                    if monthly_opt:
                        target_val = monthly_opt.get_attribute("value")
                        target_text = monthly_opt.text.strip()
                        print(f"[INFO] [DownloadSkill] 找到月到期 (Monthly) 選項: '{target_text}' (value='{target_val}')，切換至月到期以獲取更多 OI 與流動性...")
                        try:
                            sel.select_by_value(target_val)
                        except Exception:
                            pass

                        driver.execute_script("""
                            var el = arguments[0];
                            var val = arguments[1];
                            el.value = val;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                            var p = el.closest('bc-filter-experation-drop-down, div[data-ref="optSpreads"]');
                            if (p) {
                                p.setAttribute('data-selected', val);
                            }
                        """, select_el, target_val)

                        time.sleep(4)
                        dismiss_popups(driver)
                    else:
                        print(f"[INFO] [DownloadSkill] {sym_clean} 下拉選單中未發現 (m) 月到期選項，維持頁面預設值。")
                else:
                    print(f"[WARN] [DownloadSkill] {sym_clean} 頁面未找到 Expiration 到期日下拉選單。")
            except Exception as sel_err:
                print(f"[WARN] [DownloadSkill] 切換月到期下拉選單異常: {sel_err}")

        before_snapshot = set(glob.glob(os.path.join(target_dir, "*.csv")))
        dl_buttons = driver.find_elements(By.CSS_SELECTOR, "a.toolbar-button.download, [data-bc-download-button], button.download")

        if dl_buttons:
            btn = dl_buttons[0]
            print(f"[INFO] [DownloadSkill] 找到下載按鈕 (text='{btn.text.strip()}'), 點擊下載 {sym_clean} Bull Put 垂直價差 CSV ...")
            driver.execute_script("arguments[0].click();", btn)
            downloaded_file = wait_for_file_download(target_dir, before_snapshot, timeout=18)
            if downloaded_file and os.path.exists(downloaded_file) and os.path.getsize(downloaded_file) > 350:
                print(f"[SUCCESS] [DownloadSkill] {sym_clean} Bull Put 垂直價差 CSV 準備就緒: {downloaded_file} ({os.path.getsize(downloaded_file):,} 位元組)")
                return downloaded_file
            else:
                print(f"[WARN] [DownloadSkill] {sym_clean} 下載之檔案為空或無有效數據。")
        else:
            print(f"[WARN] [DownloadSkill] {sym_clean} Bull Put 垂直價差頁面未找到下載按鈕 (可能無價差數據)。")
    except Exception as e:
        print(f"[WARN] [DownloadSkill] 下載 {url} 異常: {e}")

    # 檢查本地是否有現存的專屬 bull-put-spread 檔案 (嚴格排除 bull-call-spread，優先採用 monthly 檔)
    candidates = glob.glob(os.path.join(target_dir, f"*{sym_clean.lower()}*bull-put*.csv"))
    if not candidates:
        old_dir = os.path.join(target_dir, "old")
        if os.path.exists(old_dir):
            candidates = glob.glob(os.path.join(old_dir, f"*{sym_clean.lower()}*bull-put*.csv"))
    if candidates:
        candidates.sort(key=os.path.getmtime, reverse=True)
        valid_cands = [c for c in candidates if os.path.getsize(c) > 350 and "bull-call" not in os.path.basename(c).lower()]
        if valid_cands:
            # 優先挑選 monthly 檔案
            monthly_cands = [c for c in valid_cands if "monthly" in os.path.basename(c).lower()]
            selected_cand = monthly_cands[0] if monthly_cands else valid_cands[0]
            print(f"[INFO] [DownloadSkill] 沿用現存 {sym_clean} Bull Put 垂直價差 CSV: {selected_cand}")
            return selected_cand

    print(f"[WARN] [DownloadSkill] 標的 {sym_clean} 查無專屬 Bull Put 垂直價差數據，放棄此標的。")
    return None

