#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Barchart Opinion Auto Hedge / Position Adjuster (trade/opinion.py)
================================================================================
本模組透過二個核心 SKILLS 實現自動化方向性指標分析與倉位校準：

  1. 第一個 SKILL (trade/skills/opinion_skill):
     參考 uoa_skill 架構，造訪指定 Barchart 觀點頁面 (如 https://www.barchart.com/futures/quotes/TAV26/opinion)，
     精準萃取「7 Day Average Directional Indicator」欄位右側的 "Buy" 或 "Sell" 方向訊號，
     並自動自網址擷取目標標的代號 (例如 TAV26)。

  2. 第二個 SKILL (trade/skills/position_order_skill):
     參考 barchart_placeOrder.py 的合約解析與下單架構：
     - 若為 "Buy"：檢查當前 IBKR 帳戶中該標的之未平倉淨部位是否為 +1。若不是，送出 Adaptive Patient 委託調至 +1。
     - 若為 "Sell"：檢查當前 IBKR 帳戶中該標的之未平倉淨部位是否為 -1。若不是，送出 Adaptive Patient 委託調至 -1。
     - 若已達標則維持持倉不重複下單。
     - 自動推播最新調倉狀態至手機 LINE。

使用方式：
  python trade/opinion.py
  python trade/opinion.py --url https://www.barchart.com/futures/quotes/TAV26/opinion
  python trade/opinion.py --symbol TAV26 --dry-run
================================================================================
"""

import os
import sys
import argparse
import datetime

# 確保 Windows 主控台正確輸出 UTF-8
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from skills.opinion_skill import BarchartOpinionSkill
from skills.position_order_skill import PositionOrderSkill


def run_opinion_strategy(url: str = None, symbol: str = None, dry_run: bool = False) -> dict:
    """
    執行 Barchart Opinion 指標萃取與 IBKR 自動調倉流程
    """
    target_url = url or "https://www.barchart.com/futures/quotes/TAV26/opinion"
    if symbol:
        # 若指定了 symbol 且未指定自訂 url，組合預設 url
        if not url:
            target_url = f"https://www.barchart.com/futures/quotes/{symbol.strip().upper()}/opinion"

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("\n" + "=" * 70)
    print("🚀 【Barchart Opinion 方向指標與 IBKR Adaptive Patient 調倉系統】")
    print(f"🕒 啟動時間：{now_str}")
    print(f"🌐 目標網址：{target_url}")
    print(f"⚙️ 執行模式：{'模擬測試 (Dry-Run)' if dry_run else '正式交易 (Live)'}")
    print("=" * 70)

    # --------------------------------------------------------------------------
    # 步驟 1：調用第 1 個 SKILL 抓取 Barchart Opinion 指標
    # --------------------------------------------------------------------------
    print("\n👉 [步驟 1/2] 啟動 OpinionScraperSkill 擷取技術指標...")
    scraper = BarchartOpinionSkill()
    opinion_res = None
    try:
        opinion_res = scraper.fetch_opinion(
            url_or_symbol=target_url,
            target_indicator="7 Day Average Directional Indicator"
        )
    finally:
        scraper.close()

    if not opinion_res or opinion_res.get("status") != "ok":
        err_msg = opinion_res.get("message") if opinion_res else "未能取得技術指標"
        print(f"\n❌ [ERROR] 步驟 1 失敗: {err_msg}")
        return {"status": "error", "step": 1, "message": err_msg}

    parsed_symbol = symbol or opinion_res.get("symbol")
    signal = opinion_res.get("signal", "UNKNOWN").upper()
    raw_signal = opinion_res.get("raw_signal", "")

    print(f"\n✅ [步驟 1 完成] 成功取得方向訊號：")
    print(f"   • 標的代號：{parsed_symbol}")
    print(f"   • 7 Day Average Directional Indicator：【{signal}】 (原始文字: {raw_signal})")
    if opinion_res.get("overall_opinion"):
        print(f"   • Overall Opinion：{opinion_res.get('overall_opinion')}")

    # --------------------------------------------------------------------------
    # 步驟 2：調用第 2 個 SKILL 檢查未平倉並以 Adaptive Patient 委託調倉
    # --------------------------------------------------------------------------
    print("\n👉 [步驟 2/2] 啟動 PositionOrderSkill 檢查持倉並調度 IBKR 委託...")
    order_skill = PositionOrderSkill()
    order_res = None
    try:
        order_res = order_skill.adjust_position(
            symbol=parsed_symbol,
            signal=signal,
            dry_run=dry_run
        )
    finally:
        order_skill.close()

    # --------------------------------------------------------------------------
    # 步驟 3：綜合結果總結輸出
    # --------------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("📋 【執行結果總結】")
    print(f"  • 標的代號：{parsed_symbol}")
    print(f"  • 技術指標：7 Day Average Directional Indicator ➔ 【{signal}】")
    if order_res:
        current_pos = order_res.get("current_pos")
        target_pos = order_res.get("target_pos")
        order_placed = order_res.get("order_placed")
        print(f"  • 調倉前部位：{current_pos:+g} 口")
        print(f"  • 目標部位  ：{target_pos:+g} 口")
        if order_placed:
            print(f"  • 送單明細  ：{order_res.get('order_desc')}")
            print(f"  • 委託狀態  ：{order_res.get('order_status')}")
        else:
            print(f"  • 狀態判定  ：持倉已達標 (+1 / -1)，無須下單")
    print("=" * 70 + "\n")

    return {
        "status": "ok",
        "symbol": parsed_symbol,
        "signal": signal,
        "opinion_data": opinion_res,
        "order_data": order_res,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Barchart 7 Day Directional Indicator 監控與 IBKR Adaptive Patient 自動調倉 (trade/opinion.py)"
    )
    parser.add_argument(
        "--url",
        type=str,
        default="https://www.barchart.com/futures/quotes/TAV26/opinion",
        help="Barchart 技術觀點網址 (預設: https://www.barchart.com/futures/quotes/TAV26/opinion)"
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default=None,
        help="指定標的代碼 (若提供則覆蓋或自動構造 URL，例如 TAV26)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="模擬測試模式，僅查詢指標與部位，不實際向交易所送單"
    )

    args = parser.parse_args()
    run_opinion_strategy(url=args.url, symbol=args.symbol, dry_run=args.dry_run)
