#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Barchart Opinion Auto Hedge / Position Adjuster (trade/opinion.py)
================================================================================
本模組透過二個核心 SKILLS 實現多商品自動化方向性指標分析與倉位校準：

  每次執行時：
  1. 自動從 trade/.env 讀取 opinion_symbols 清單 (例如 ["TAV26", ...])
  2. 第一個 SKILL (trade/skills/opinion_skill):
     逐一尋找每一個 symbol 的 Barchart Opinion 網址，
     精準萃取「7 Day Average Directional Indicator」欄位右側的 "Buy" 或 "Sell" 方向訊號。
  3. 第二個 SKILL (trade/skills/position_order_skill):
     依序檢查即時該 symbol 在 IBKR 的未平倉庫存：
     - 若為 "Buy"：檢查未平倉淨部位是否為 +1。若不是，送出 Adaptive Patient 委託調至 +1。
     - 若為 "Sell"：檢查未平倉淨部位是否為 -1。若不是，送出 Adaptive Patient 委託調至 -1。
     - 若已達標則維持持倉不重複下單。
  4. 自動推播調倉與執行明細至手機 LINE。

使用方式：
  python trade/opinion.py                     # 自動讀取 trade/.env 之 opinion_symbols
  python trade/opinion.py --dry-run           # 模擬測試模式 (不實際送單)
  python trade/opinion.py --symbols TAV26     # 指定特定商品清單 (覆蓋 .env)
  python trade/opinion.py --symbol TAV26      # 指定單一標的
  python trade/opinion.py --url https://...   # 指定單一網址
================================================================================
"""

import os
import sys
import ast
import json
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

ENV_FILE = os.path.join(BASE_DIR, ".env")

from skills.opinion_skill import BarchartOpinionSkill
from skills.position_order_skill import PositionOrderSkill

try:
    from notifier import send_push_message
except ImportError:
    send_push_message = None


def load_opinion_symbols(env_path: str = ENV_FILE) -> list:
    """
    從 trade/.env 動態讀取 opinion_symbols / OPINION_SYMBOLS 清單。
    支援格式：
      OPINION_SYMBOLS='["TAV26", "ESZ26"]'
      opinion_symbols='["TAV26"]'
      OPINION_SYMBOLS=TAV26, ESZ26
      OPINION_SYMBOLS=TAV26
    若未設定或解析為空，回退預設值 ["TAV26"]。
    """
    symbols = []
    if not os.path.exists(env_path):
        print(f"[WARN] 找不到 .env 檔案: {env_path}，使用預設標的 ['TAV26']")
        return ["TAV26"]

    try:
        with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                for delim in ["=", ":"]:
                    if delim in line:
                        k, v = line.split(delim, 1)
                        k = k.strip().upper()
                        v = v.strip().strip("'").strip('"')

                        if k in ("OPINION_SYMBOLS", "OPINION_SYMBOL"):
                            parsed = False
                            # 嘗試 JSON 解析
                            if v.startswith("[") and v.endswith("]"):
                                try:
                                    symbols = json.loads(v)
                                    parsed = True
                                except Exception:
                                    try:
                                        symbols = ast.literal_eval(v)
                                        parsed = True
                                    except Exception:
                                        pass
                            # 若非列表格式，嘗試逗號分隔或單一字串
                            if not parsed:
                                if "," in v:
                                    symbols = [s.strip().strip("'").strip('"') for s in v.split(",") if s.strip()]
                                elif v:
                                    symbols = [v]
                            break
    except Exception as e:
        print(f"[WARN] 讀取 .env 的 OPINION_SYMBOLS 失敗: {e}")

    # 清理非空字串並去除空白
    clean_syms = [str(s).strip() for s in symbols if s and str(s).strip()]
    if not clean_syms:
        clean_syms = ["TAV26"]

    return clean_syms


def run_opinion_strategy(symbols: list = None, url: str = None, dry_run: bool = False, send_line: bool = True) -> dict:
    """
    執行 Barchart Opinion 多標的方向指標萃取與 IBKR 自動調倉流程
    """
    # 決定要處理的商品清單
    if url:
        # 若指定了具體 URL，直接作為單一標的處理
        symbols_to_process = [url]
    elif symbols:
        symbols_to_process = list(symbols)
    else:
        # 預設自 trade/.env 讀取
        symbols_to_process = load_opinion_symbols(ENV_FILE)

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("\n" + "=" * 75)
    print("🚀 【Barchart Opinion 多商品方向指標與 IBKR Adaptive Patient 調倉系統】")
    print(f"🕒 啟動時間：{now_str}")
    print(f"📋 監控標的清單 (共 {len(symbols_to_process)} 檔)：{symbols_to_process}")
    print(f"⚙️ 執行模式：{'模擬測試 (Dry-Run)' if dry_run else '正式交易 (Live)'}")
    print("=" * 75)

    scraper = BarchartOpinionSkill()
    order_skill = PositionOrderSkill()
    results = []

    try:
        # 共用驅動與連線，避免頻繁建立與關閉
        scraper.ensure_driver()
        order_skill.ensure_ib()

        for idx, item in enumerate(symbols_to_process, 1):
            print(f"\n" + "-" * 75)
            print(f"👉 【標的 {idx}/{len(symbols_to_process)}】正在處理: {item}")
            print("-" * 75)

            # ------------------------------------------------------------------
            # 第一個 SKILL：找尋該 symbol 的 Barchart 網址，取得 BUY or SELL 方向訊號
            # ------------------------------------------------------------------
            print(f"[步驟 1/2] 啟動 BarchartOpinionSkill 查詢技術觀點指標...")
            opinion_res = scraper.fetch_opinion(
                url_or_symbol=item,
                target_indicator="7 Day Average Directional Indicator"
            )

            if not opinion_res or opinion_res.get("status") != "ok":
                err_msg = opinion_res.get("message") if opinion_res else "未能取得技術指標"
                print(f"❌ [ERROR] 標的 {item} 指標擷取失敗: {err_msg}")
                results.append({
                    "symbol": item,
                    "status": "error",
                    "signal": "ERROR",
                    "message": err_msg,
                    "opinion_res": opinion_res,
                    "order_res": None
                })
                continue

            parsed_symbol = opinion_res.get("symbol") or item
            signal = opinion_res.get("signal", "UNKNOWN").upper()
            raw_signal = opinion_res.get("raw_signal", "")

            print(f"✅ [步驟 1 完成] 成功取得方向訊號：")
            print(f"   • 標的代號：{parsed_symbol}")
            print(f"   • 7 Day Average Directional Indicator：【{signal}】 (原始文字: {raw_signal})")
            if opinion_res.get("overall_opinion"):
                print(f"   • Overall Opinion：{opinion_res.get('overall_opinion')}")

            # ------------------------------------------------------------------
            # 第二個 SKILL：依序檢查即時該 symbol 的未平倉庫存，執行調倉送單
            # ------------------------------------------------------------------
            print(f"[步驟 2/2] 啟動 PositionOrderSkill 檢查即時庫存並調度委託...")
            order_res = order_skill.adjust_position(
                symbol=parsed_symbol,
                signal=signal,
                dry_run=dry_run,
                send_line=send_line
            )

            results.append({
                "symbol": parsed_symbol,
                "status": "ok",
                "signal": signal,
                "opinion_res": opinion_res,
                "order_res": order_res
            })

    except Exception as e:
        print(f"\n❌ [CRITICAL ERROR] 執行過程發生未預期異常: {e}")
    finally:
        # 安全釋放資源
        scraper.close()
        order_skill.close()

    # --------------------------------------------------------------------------
    # 綜合結果總結輸出
    # --------------------------------------------------------------------------
    print("\n" + "=" * 75)
    print(f"📋 【全部標的執行結果總結】(共處理 {len(results)}/{len(symbols_to_process)} 檔)")
    print("-" * 75)
    for r in results:
        sym = r.get("symbol")
        sig = r.get("signal", "UNKNOWN")
        ores = r.get("order_res")
        if ores:
            cpos = ores.get("current_pos")
            tpos = ores.get("target_pos")
            placed = ores.get("order_placed")
            c_name = ores.get("contract", sym)
            if placed:
                desc = f"🚀 已送出委託: {ores.get('order_desc')}"
            else:
                desc = f"✅ 持倉已達標 ({tpos:+g} 口)，無須重複送單"
            print(f"  • {sym:<8} ({c_name}) | 指標: 【{sig:<4}】 | 部位: {cpos:+g} ➔ {tpos:+g} 口 | {desc}")
        else:
            print(f"  • {sym:<8} | 指標: 【{sig:<4}】 | ❌ 失敗: {r.get('message')}")
    print("=" * 75 + "\n")

    # 若有多檔標的且開啟推播，額外發送彙總推播通知
    if send_line and send_push_message and len(results) > 1:
        summary_lines = [
            f"🎯【Barchart Opinion 批次調倉總結】",
            f"🕒 時間：{now_str}",
            f"⚙️ 模式：{'模擬測試 (Dry-Run)' if dry_run else '正式交易 (Live)'}",
            f"📋 處理標的：共 {len(results)} 檔",
            ""
        ]
        for r in results:
            sym = r.get("symbol")
            sig = r.get("signal", "UNKNOWN")
            ores = r.get("order_res")
            if ores:
                cpos = ores.get("current_pos")
                tpos = ores.get("target_pos")
                c_name = ores.get("contract", sym)
                action_info = "已送單" if ores.get("order_placed") else "維持持倉"
                summary_lines.append(f"• {sym} ({c_name}): {sig} | 部位 {cpos:+g} ➔ {tpos:+g} | {action_info}")
            else:
                summary_lines.append(f"• {sym}: {sig} | 處理失敗")

        try:
            send_push_message("\n".join(summary_lines))
        except Exception:
            pass

    return {
        "status": "ok",
        "total": len(symbols_to_process),
        "results": results
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Barchart 7 Day Directional Indicator 監控與 IBKR Adaptive Patient 自動調倉 (trade/opinion.py)"
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default=None,
        help="指定商品清單，逗號分隔 (例如 TAV26,ESZ26)，若未提供則自 trade/.env 之 OPINION_SYMBOLS 讀取"
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default=None,
        help="指定單一標的代號 (例如 TAV26)"
    )
    parser.add_argument(
        "--url",
        type=str,
        default=None,
        help="指定單一 Barchart 技術觀點網址 (例如 https://www.barchart.com/futures/quotes/TAV26/opinion)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="模擬測試模式，僅查詢指標與部位，不實際向交易所送單"
    )
    parser.add_argument(
        "--no-line",
        action="store_true",
        help="不發送 LINE 推播通知"
    )

    args = parser.parse_args()

    # 解析 symbols 參數
    selected_symbols = None
    if args.symbols:
        selected_symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    elif args.symbol:
        selected_symbols = [args.symbol.strip()]

    run_opinion_strategy(
        symbols=selected_symbols,
        url=args.url,
        dry_run=args.dry_run,
        send_line=(not args.no_line)
    )
