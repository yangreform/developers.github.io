#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Barchart Opinion Scraper Skill (trade/skills/opinion_skill/opinion_scraper.py)
================================================================================
職責：
  1. 接收 Barchart Opinion 網址 (或標的代號)，自動解析目標標的 SYMBOL (例如 TAV26)
  2. 透過瀏覽器安全載入頁面，自動處理 Cookie 與彈跳提示
  3. 精準定位「7 Day Average Directional Indicator」欄位，並抓取其右側數值 ("Buy" 或 "Sell")
  4. 回傳標準化分析結果字典
================================================================================
"""

import os
import sys
import re
import time

# 確保路徑正常匯入
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
except ImportError:
    pass

try:
    from barchart_download import init_driver, dismiss_popups, login_if_needed, load_credentials_from_env, ENV_FILE
except ImportError:
    init_driver = None
    dismiss_popups = None
    login_if_needed = None
    load_credentials_from_env = None
    ENV_FILE = os.path.join(BASE_DIR, ".env")


class BarchartOpinionSkill:
    """
    Barchart 技術觀點頁面方向性指標萃取技能
    """

    def __init__(self, driver=None, env_path=None):
        self.driver = driver
        self.owns_driver = False
        self.env_path = env_path or os.path.join(BASE_DIR, ".env")

    @staticmethod
    def extract_symbol_from_url(url_or_symbol: str) -> str:
        """
        從網址中擷取標的代碼，例如：
        https://www.barchart.com/futures/quotes/TAV26/opinion -> TAV26
        若傳入純代碼則直接清理回傳。
        """
        if not url_or_symbol:
            return ""

        text = url_or_symbol.strip()
        if text.startswith("http://") or text.startswith("https://"):
            # 匹配 /quotes/XXXX/ 或 /quotes/XXXX
            m = re.search(r'/quotes/([^/?#]+)(?:/opinion)?', text, re.IGNORECASE)
            if m:
                return m.group(1).strip().upper()
        # 純代碼或未能從 URL 匹配到的回退
        return text.split("/")[-1].replace("opinion", "").strip().upper()

    @staticmethod
    def build_opinion_url(symbol_or_url: str) -> str:
        """
        根據輸入字串轉換為合法的 Barchart Opinion 網址
        """
        text = symbol_or_url.strip()
        if text.startswith("http://") or text.startswith("https://"):
            if not text.endswith("/opinion"):
                text = text.rstrip("/") + "/opinion"
            return text

        sym = text.upper()
        # 預設為期貨路徑 /futures/quotes/{sym}/opinion
        return f"https://www.barchart.com/futures/quotes/{sym}/opinion"

    def ensure_driver(self):
        """
        確保有可用的 Selenium Driver
        """
        if self.driver is None:
            if init_driver is None:
                raise RuntimeError("無法匯入 barchart_download.init_driver，請確認 selenium 與 undetected_chromedriver 已安裝。")
            print("[INFO] [OpinionSkill] 正在啟動瀏覽器驅動...")
            self.driver = init_driver(headless=False)
            self.owns_driver = True

            # 嘗試登入或確認登入狀態
            if login_if_needed and load_credentials_from_env:
                try:
                    acct, pwd = load_credentials_from_env(self.env_path)
                    login_if_needed(self.driver, acct, pwd)
                except Exception as e:
                    print(f"[WARN] [OpinionSkill] 檢查登入狀態提示: {e}")

        return self.driver

    def close(self):
        """
        若本實例自行建立了 Driver，則予以關閉
        """
        if self.owns_driver and self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
            self.owns_driver = False

    def fetch_opinion(self, url_or_symbol: str, target_indicator: str = "7 Day Average Directional Indicator") -> dict:
        """
        擷取 Barchart Opinion 頁面中的指定指標方向 (預設: 7 Day Average Directional Indicator)

        回傳格式：
        {
            "status": "ok" / "error",
            "symbol": "TAV26",
            "url": "https://www.barchart.com/futures/quotes/TAV26/opinion",
            "target_indicator": "7 Day Average Directional Indicator",
            "signal": "BUY" / "SELL" / "HOLD" / "UNKNOWN",
            "raw_signal": "Buy",
            "overall_opinion": "100% BUY",
            "title": "...",
            "message": "..."
        }
        """
        symbol = self.extract_symbol_from_url(url_or_symbol)
        url = self.build_opinion_url(url_or_symbol)

        print(f"\n" + "=" * 60)
        print(f"🧭 [OpinionSkill] 開始分析 Barchart 觀點指標")
        print(f"   • 目標標的: {symbol}")
        print(f"   • 網址: {url}")
        print(f"   • 目標欄位: {target_indicator}")
        print("=" * 60)

        driver = self.ensure_driver()

        try:
            print(f"[INFO] [OpinionSkill] 正在導航至: {url} ...")
            driver.get(url)
            time.sleep(5)

            if dismiss_popups:
                dismiss_popups(driver)

            page_title = driver.title
            print(f"[INFO] [OpinionSkill] 頁面標題: {page_title}")

            # 搜尋目標指標列
            # 支援精準定位與寬鬆搜尋
            found_signal = None
            raw_signal_text = ""
            overall_opinion = ""

            # 1. 嘗試尋找整體 Opinion (Overall Opinion)
            try:
                ov_els = driver.find_elements(By.CSS_SELECTOR, ".opinion-percent, .opinion-signal, .average-opinion-text, [data-opinion]")
                if ov_els:
                    overall_opinion = " ".join([e.text.strip() for e in ov_els if e.text.strip()])
            except Exception:
                pass

            # 2. 尋找表格列 (TR)
            target_clean = target_indicator.strip().lower()
            trs = driver.find_elements(By.TAG_NAME, "tr")
            print(f"[INFO] [OpinionSkill] 頁面載入完成，正在掃描 {len(trs)} 個表格列...")

            matched_tr = None
            for tr in trs:
                text = tr.text
                if not text:
                    continue
                if target_clean in text.lower():
                    matched_tr = tr
                    break

            if matched_tr:
                tds = matched_tr.find_elements(By.TAG_NAME, "td")
                # 預期結構：
                # TD 0: 圖表連結
                # TD 1: 指標名稱 (7 Day Average Directional Indicator)
                # TD 2: 指標方向 (BUY / SELL / HOLD)
                signal_td = None
                # 先依 class 搜尋
                sig_candidates = matched_tr.find_elements(By.CSS_SELECTOR, ".indicator-item-signal, .signal, td:last-child")
                if sig_candidates:
                    signal_td = sig_candidates[0]
                elif len(tds) >= 2:
                    signal_td = tds[-1]

                if signal_td:
                    raw_signal_text = signal_td.text.strip()
                    clean_sig = raw_signal_text.upper()
                    if "BUY" in clean_sig:
                        found_signal = "BUY"
                    elif "SELL" in clean_sig:
                        found_signal = "SELL"
                    elif "HOLD" in clean_sig or "NEUTRAL" in clean_sig:
                        found_signal = "HOLD"
                    else:
                        found_signal = clean_sig

                    print(f"[SUCCESS] ✅ 成功定位指標 [{target_indicator}] -> 方向: 【{found_signal}】 (原始值: {raw_signal_text})")
            else:
                # 備援：正則表達式從頁面文字或原始碼匹配
                page_source = driver.page_source
                m_regex = re.search(r'7\s*Day\s*Average\s*Directional\s*Indicator[^\w<>]*(?:<[^>]+>)*\s*(BUY|SELL|HOLD)', page_source, re.IGNORECASE)
                if m_regex:
                    found_signal = m_regex.group(1).upper()
                    raw_signal_text = m_regex.group(1)
                    print(f"[SUCCESS] ✅ (正則備援) 成功提取 [{target_indicator}] -> 方向: 【{found_signal}】")

            if not found_signal:
                err_msg = f"在頁面中未找到指標 [{target_indicator}] 的方向訊號"
                print(f"[WARN] [OpinionSkill] {err_msg}")
                return {
                    "status": "error",
                    "symbol": symbol,
                    "url": url,
                    "target_indicator": target_indicator,
                    "signal": "UNKNOWN",
                    "raw_signal": "",
                    "overall_opinion": overall_opinion,
                    "title": page_title,
                    "message": err_msg,
                }

            return {
                "status": "ok",
                "symbol": symbol,
                "url": url,
                "target_indicator": target_indicator,
                "signal": found_signal,
                "raw_signal": raw_signal_text,
                "overall_opinion": overall_opinion,
                "title": page_title,
                "message": f"成功提取 {symbol} 之 {target_indicator} 方向為 {found_signal}",
            }

        except Exception as e:
            err = f"擷取 Barchart Opinion 失敗: {e}"
            print(f"[ERROR] [OpinionSkill] {err}")
            return {
                "status": "error",
                "symbol": symbol,
                "url": url,
                "target_indicator": target_indicator,
                "signal": "ERROR",
                "raw_signal": "",
                "overall_opinion": "",
                "title": "",
                "message": err,
            }
