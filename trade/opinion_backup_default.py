#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Barchart Opinion Auto Hedge / Position Adjuster (trade/opinion.py)
================================================================================
本模組透過二個核心 SKILLS 實現多商品自動化方向性指標分析與倉位校準：

  每次執行時：
  1. 自動從 trade/.env 讀取 OPINION_SYMBOLS 設定 (支援 JSON 格式，每檔商品可配置專屬指標與專屬網址)
  2. 第一個 SKILL (trade/skills/opinion_skill):
     逐一尋找或造訪每一個 symbol 的 Barchart 網址，
     精準萃取該商品指定之技術指標（例如「7 Day Average Directional Indicator」或「20 Day Bollinger Bands」）
     欄位右側的 "BUY"、"SELL" 或 "HOLD" 方向訊號。
  3. 第二個 SKILL (trade/skills/position_order_skill):
     依序檢查即時該 symbol 在 IBKR 的未平倉庫存：
     - 若為 "BUY"：檢查未平倉淨部位是否為 +1。若不是，送出 Adaptive Patient 委託調至 +1。
     - 若為 "SELL"：檢查未平倉淨部位是否為 -1。若不是，送出 Adaptive Patient 委託調至 -1。
     - 若為 "HOLD" / 其他：不執行調倉，維持現有持倉。
     - 若已達標則維持持倉不重複下單。
  4. 自動推播調倉與執行明細至手機 LINE。

使用方式：
  python trade/opinion.py                                # 自動讀取 trade/.env 之 OPINION_SYMBOLS
  python trade/opinion.py --dry-run                      # 模擬測試模式 (不實際送單)
  python trade/opinion.py --symbols GSZ26,VUZ26          # 指定特定商品清單 (覆蓋 .env)
  python trade/opinion.py --symbol GSZ26 --indicator "20 Day Bollinger Bands"
  python trade/opinion.py --url https://...              # 指定單一網址
================================================================================
"""

import os
import sys
import ast
import json
import argparse
import datetime
import time

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


DEFAULT_INDICATOR = "7 Day Average Directional Indicator"

# 預設各網址標的對應之真正下單 SYMBOL (例如 CFE 交易所之 PET、PBT)
DEFAULT_ORDER_SYMBOLS = {
    "J6Z26": "J6Z26",
    "TAV26": "PET",
    "GSZ26": "GSZ26",
    "VUZ26": "VUZ26",
    "BAV26": "PBT",
}

# 特定標的專屬預設網址 (若配置中未指定則以此為準)
DEFAULT_SYMBOL_URLS = {
    "GSZ26": "https://www.barchart.com/futures/quotes/GSZ26/overview",
    "VUZ26": "https://www.barchart.com/futures/quotes/VUZ26/opinion",
    "BAV26": "https://www.barchart.com/futures/quotes/BAV26/opinion",
}


def load_opinion_configs(env_path: str = ENV_FILE) -> list:
    """
    從 trade/.env 動態讀取 OPINION_SYMBOLS / opinion_symbols 設定。
    支援以下格式：
      1. JSON 字典 (鍵為商品代號，值為指標名稱或詳細物件)：
         OPINION_SYMBOLS='{"J6Z26": "7 Day Average Directional Indicator", "TAV26": {"indicator": "...", "order_symbol": "PET"}}'
      2. JSON 陣列 (字串列表或物件列表)：
         OPINION_SYMBOLS='["J6Z26", "TAV26"]'
         OPINION_SYMBOLS='[{"symbol": "TAV26", "order_symbol": "PET", "indicator": "..."}]'
      3. 逗號分隔字串
    
    回傳標準格式清單：
    [
        {"symbol": "TAV26", "order_symbol": "PET", "indicator": "7 Day Average Directional Indicator", "url": "https://..."},
        ...
    ]
    """
    raw_val = None
    if os.path.exists(env_path):
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
                                raw_val = v
                                break
        except Exception as e:
            print(f"[WARN] 讀取 .env 的 OPINION_SYMBOLS 失敗: {e}")

    targets = []
    if raw_val:
        parsed_data = None
        # 嘗試 JSON 解析
        try:
            parsed_data = json.loads(raw_val)
        except Exception:
            try:
                parsed_data = ast.literal_eval(raw_val)
            except Exception:
                pass

        if isinstance(parsed_data, dict):
            for sym, conf in parsed_data.items():
                sym_clean = str(sym).strip().upper()
                if not sym_clean:
                    continue
                if isinstance(conf, dict):
                    ind = conf.get("indicator") or DEFAULT_INDICATOR
                    url = conf.get("url") or DEFAULT_SYMBOL_URLS.get(sym_clean)
                    order_sym = conf.get("order_symbol") or conf.get("order_sym") or conf.get("trade_symbol") or DEFAULT_ORDER_SYMBOLS.get(sym_clean, sym_clean)
                elif isinstance(conf, str):
                    ind = conf.strip() or DEFAULT_INDICATOR
                    url = DEFAULT_SYMBOL_URLS.get(sym_clean)
                    order_sym = DEFAULT_ORDER_SYMBOLS.get(sym_clean, sym_clean)
                else:
                    ind = DEFAULT_INDICATOR
                    url = DEFAULT_SYMBOL_URLS.get(sym_clean)
                    order_sym = DEFAULT_ORDER_SYMBOLS.get(sym_clean, sym_clean)
                targets.append({
                    "symbol": sym_clean,
                    "order_symbol": order_sym,
                    "indicator": ind,
                    "url": url
                })
        elif isinstance(parsed_data, list):
            for item in parsed_data:
                if isinstance(item, dict):
                    sym_clean = str(item.get("symbol", "")).strip().upper()
                    if not sym_clean:
                        continue
                    ind = item.get("indicator") or DEFAULT_INDICATOR
                    url = item.get("url") or DEFAULT_SYMBOL_URLS.get(sym_clean)
                    order_sym = item.get("order_symbol") or item.get("order_sym") or item.get("trade_symbol") or DEFAULT_ORDER_SYMBOLS.get(sym_clean, sym_clean)
                    targets.append({
                        "symbol": sym_clean,
                        "order_symbol": order_sym,
                        "indicator": ind,
                        "url": url
                    })
                elif isinstance(item, str):
                    sym_clean = item.strip().upper()
                    if sym_clean:
                        targets.append({
                            "symbol": sym_clean,
                            "order_symbol": DEFAULT_ORDER_SYMBOLS.get(sym_clean, sym_clean),
                            "indicator": DEFAULT_INDICATOR,
                            "url": DEFAULT_SYMBOL_URLS.get(sym_clean)
                        })
        elif isinstance(raw_val, str):
            for s in raw_val.split(","):
                sym_clean = s.strip().strip("'").strip('"').upper()
                if sym_clean:
                    targets.append({
                        "symbol": sym_clean,
                        "order_symbol": DEFAULT_ORDER_SYMBOLS.get(sym_clean, sym_clean),
                        "indicator": DEFAULT_INDICATOR,
                        "url": DEFAULT_SYMBOL_URLS.get(sym_clean)
                    })

    if not targets:
        # 回退預設
        targets = [
            {"symbol": "J6Z26", "order_symbol": "J6Z26", "indicator": "7 Day Average Directional Indicator", "url": None},
            {"symbol": "TAV26", "order_symbol": "PET", "indicator": "7 Day Average Directional Indicator", "url": None},
            {"symbol": "GSZ26", "order_symbol": "GSZ26", "indicator": "20 Day Bollinger Bands", "url": "https://www.barchart.com/futures/quotes/GSZ26/overview"},
            {"symbol": "VUZ26", "order_symbol": "VUZ26", "indicator": "20 Day Bollinger Bands", "url": "https://www.barchart.com/futures/quotes/VUZ26/opinion"},
            {"symbol": "BAV26", "order_symbol": "PBT", "indicator": "7 Day Average Directional Indicator", "url": "https://www.barchart.com/futures/quotes/BAV26/opinion"},
        ]

    return targets


def load_opinion_symbols(env_path: str = ENV_FILE) -> list:
    """
    相容原函式簽名，回傳所有目標標的代號清單。
    """
    configs = load_opinion_configs(env_path)
    return [c["symbol"] for c in configs]


def run_opinion_strategy(
    symbols: list = None,
    url: str = None,
    indicator: str = None,
    dry_run: bool = False,
    send_line: bool = True
) -> dict:
    """
    執行 Barchart Opinion 多標的方向指標萃取與 IBKR 自動調倉流程
    """
    scraper_helper = BarchartOpinionSkill()

    # 決定要處理的目標商品與指標清單
    if url:
        parsed_sym = scraper_helper.extract_symbol_from_url(url)
        targets_to_process = [{
            "symbol": parsed_sym,
            "order_symbol": DEFAULT_ORDER_SYMBOLS.get(parsed_sym, parsed_sym),
            "indicator": indicator or DEFAULT_INDICATOR,
            "url": url
        }]
    elif symbols:
        env_configs = {c["symbol"].upper(): c for c in load_opinion_configs(ENV_FILE)}
        targets_to_process = []
        for s in symbols:
            s_clean = str(s).strip().upper()
            if not s_clean:
                continue
            if s_clean in env_configs:
                cfg = dict(env_configs[s_clean])
                if indicator:
                    cfg["indicator"] = indicator
                targets_to_process.append(cfg)
            else:
                targets_to_process.append({
                    "symbol": s_clean,
                    "order_symbol": DEFAULT_ORDER_SYMBOLS.get(s_clean, s_clean),
                    "indicator": indicator or DEFAULT_INDICATOR,
                    "url": DEFAULT_SYMBOL_URLS.get(s_clean)
                })
    else:
        # 預設自 trade/.env 讀取完整配置 (含 symbol、order_symbol、indicator 與專屬 url)
        targets_to_process = load_opinion_configs(ENV_FILE)

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("\n" + "=" * 75)
    print("🚀 【Barchart Opinion 多商品方向指標與 IBKR Adaptive Patient 調倉系統】")
    print(f"🕒 啟動時間：{now_str}")
    print(f"📋 監控標的清單 (共 {len(targets_to_process)} 檔)：")
    for t in targets_to_process:
        url_info = f" -> {t['url']}" if t.get("url") else ""
        print(f"   • 網址標的: {t['symbol']} ➔ 下單標的: {t['order_symbol']} | 指標: [{t['indicator']}]{url_info}")
    print(f"⚙️ 執行模式：{'模擬測試 (Dry-Run)' if dry_run else '正式交易 (Live)'}")
    print("=" * 75)

    scraper = BarchartOpinionSkill()
    order_skill = PositionOrderSkill()
    results = []

    try:
        # 共用驅動與連線，避免頻繁建立與關閉
        scraper.ensure_driver()
        order_skill.ensure_ib()

        for idx, target_cfg in enumerate(targets_to_process, 1):
            item_sym = target_cfg["symbol"]
            item_indicator = target_cfg.get("indicator") or DEFAULT_INDICATOR
            item_url = target_cfg.get("url") or item_sym
            item_order_sym = target_cfg.get("order_symbol") or DEFAULT_ORDER_SYMBOLS.get(item_sym, item_sym)

            print(f"\n" + "-" * 75)
            print(f"👉 【標的 {idx}/{len(targets_to_process)}】正在處理: {item_sym} (指標: {item_indicator})")
            if target_cfg.get("url"):
                print(f"   指定網址: {target_cfg['url']}")
            print("-" * 75)

            try:
                # ------------------------------------------------------------------
                # 第一個 SKILL：找尋該 symbol 的 Barchart 網址，取得 BUY / SELL / HOLD 方向訊號
                # ------------------------------------------------------------------
                print(f"[步驟 1/2] 啟動 BarchartOpinionSkill 查詢技術觀點指標 [{item_indicator}]...")
                opinion_res = scraper.fetch_opinion(
                    url_or_symbol=item_url,
                    target_indicator=item_indicator
                )

                if not opinion_res or opinion_res.get("status") != "ok":
                    err_msg = opinion_res.get("message") if opinion_res else "未能取得技術指標"
                    print(f"❌ [ERROR] 標的 {item_sym} 指標擷取失敗: {err_msg}")
                    results.append({
                        "symbol": item_sym,
                        "order_symbol": item_order_sym,
                        "indicator": item_indicator,
                        "status": "error",
                        "signal": "ERROR",
                        "message": err_msg,
                        "opinion_res": opinion_res,
                        "order_res": None
                    })
                    continue

                parsed_symbol = opinion_res.get("symbol") or item_sym
                signal = opinion_res.get("signal", "UNKNOWN").upper()
                raw_signal = opinion_res.get("raw_signal", "")

                print(f"✅ [步驟 1 完成] 成功取得方向訊號：")
                print(f"   • 標的代號：{parsed_symbol}")
                print(f"   • {item_indicator}：【{signal}】 (原始文字: {raw_signal})")
                if opinion_res.get("overall_opinion"):
                    print(f"   • Overall Opinion：{opinion_res.get('overall_opinion')}")

                # ------------------------------------------------------------------
                # 第二個 SKILL：依序檢查即時該 symbol 的未平倉庫存，執行調倉送單
                # ------------------------------------------------------------------
                item_order_sym = target_cfg.get("order_symbol") or DEFAULT_ORDER_SYMBOLS.get(item_sym, parsed_symbol)
                print(f"[步驟 2/2] 啟動 PositionOrderSkill 檢查即時庫存並調度委託 (網址標的: {item_sym} ➔ 下單標的: {item_order_sym})...")
                order_res = order_skill.adjust_position(
                    symbol=item_order_sym,
                    signal=signal,
                    dry_run=dry_run,
                    send_line=send_line
                )

                results.append({
                    "symbol": item_sym,
                    "order_symbol": item_order_sym,
                    "indicator": item_indicator,
                    "status": "ok",
                    "signal": signal,
                    "opinion_res": opinion_res,
                    "order_res": order_res
                })
            except Exception as item_err:
                item_order_sym = target_cfg.get("order_symbol") or item_sym
                print(f"❌ [ERROR] 處理標的 {item_sym} (下單: {item_order_sym}) 時發生異常: {item_err}")
                results.append({
                    "symbol": item_sym,
                    "order_symbol": item_order_sym,
                    "indicator": item_indicator,
                    "status": "error",
                    "signal": "ERROR",
                    "message": str(item_err),
                    "opinion_res": None,
                    "order_res": None
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
    print(f"📋 【全部標的執行結果總結】(共處理 {len(results)}/{len(targets_to_process)} 檔)")
    print("-" * 75)
    for r in results:
        sym = r.get("symbol")
        order_sym = r.get("order_symbol", sym)
        sig = r.get("signal", "UNKNOWN")
        ind = r.get("indicator", DEFAULT_INDICATOR)
        ores = r.get("order_res")
        sym_desc = f"{sym:<6} (下單:{order_sym})"
        if ores:
            cpos = ores.get("current_pos")
            tpos = ores.get("target_pos")
            placed = ores.get("order_placed")
            c_name = ores.get("contract", order_sym)
            if ores.get("status") == "ignored":
                desc = f"⏸️ {ores.get('message')}"
                print(f"  • {sym_desc:<18} ({c_name}) | 指標[{ind}]: 【{sig:<4}】 | {desc}")
            else:
                if placed:
                    desc = f"🚀 已送出委託: {ores.get('order_desc')}"
                else:
                    desc = f"✅ 持倉已達標 ({tpos:+g} 口)，無須重複送單"
                pos_str = f"部位: {cpos:+g} ➔ {tpos:+g} 口 | " if (cpos is not None and tpos is not None) else ""
                print(f"  • {sym_desc:<18} ({c_name}) | 指標[{ind}]: 【{sig:<4}】 | {pos_str}{desc}")
        else:
            print(f"  • {sym_desc:<18} | 指標[{ind}]: 【{sig:<4}】 | ❌ 失敗: {r.get('message')}")
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
            order_sym = r.get("order_symbol", sym)
            sig = r.get("signal", "UNKNOWN")
            ind = r.get("indicator", DEFAULT_INDICATOR)
            ores = r.get("order_res")
            if ores:
                cpos = ores.get("current_pos")
                tpos = ores.get("target_pos")
                c_name = ores.get("contract", order_sym)
                if ores.get("status") == "ignored":
                    summary_lines.append(f"• {sym}➔{order_sym} ({c_name}): {ind} 【{sig}】 | 維持觀望 (不調倉)")
                else:
                    action_info = "已送單" if ores.get("order_placed") else "維持持倉"
                    pos_info = f" | 部位 {cpos:+g} ➔ {tpos:+g}" if (cpos is not None and tpos is not None) else ""
                    summary_lines.append(f"• {sym}➔{order_sym} ({c_name}): {ind} 【{sig}】{pos_info} | {action_info}")
            else:
                summary_lines.append(f"• {sym}➔{order_sym}: {ind} 【{sig}】 | 處理失敗")

        try:
            send_push_message("\n".join(summary_lines))
        except Exception:
            pass

    return {
        "status": "ok",
        "total": len(targets_to_process),
        "results": results
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Barchart 多商品多技術指標方向監控與 IBKR Adaptive Patient 自動調倉 (trade/opinion.py)"
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default=None,
        help="指定商品清單，逗號分隔 (例如 GSZ26,VUZ26)，若未提供則自 trade/.env 之 OPINION_SYMBOLS 讀取"
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default=None,
        help="指定單一標的代號 (例如 GSZ26)"
    )
    parser.add_argument(
        "--indicator",
        type=str,
        default=None,
        help="指定目標技術指標名稱 (例如 '20 Day Bollinger Bands'，若未指定則讀取配置或預設)"
    )
    parser.add_argument(
        "--url",
        type=str,
        default=None,
        help="指定單一 Barchart 網址 (例如 https://www.barchart.com/futures/quotes/GSZ26/overview)"
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
        indicator=args.indicator,
        dry_run=args.dry_run,
        send_line=(not args.no_line)
    )

    time.sleep(60 * 60 * 10)
