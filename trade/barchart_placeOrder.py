#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Barchart AI Report Auto Place Order (trade/barchart_placeOrder.py)
================================================================================
讀取 trade/latest_ai_analysis.txt 中的三個投資建議：
  1. 個股突破交易 (Buy Call / Buy Put / Sell Put)
  2. ETF 趨勢/宏觀對沖 (Buy Call / Buy Put / Sell Put)
  3. Bull Put 垂直價差最佳商品組合 (Bull Put Spread)

執行邏輯：
  1. 預查即時市價 (Bid / Ask / MarketPrice)
  2. 建立 IBKR Adaptive Patient 演算法母單
  3. 自動掛出 Attached Order 附屬停利與停損訂單：
     - SELL PUT (及 Bull Put 賣方信用價差)：
       Profit Taker 價格 = 現價的一半 (0.5 * 現價，權利金收斂 50% 停利)
       STOP LOSS 價格    = 現價的二倍 (2.0 * 現價，虧損擴大 2 倍停損)
     - BUY PUT / CALL (買方策略)：
       Profit Taker 價格 = 現價的二倍 (2.0 * 現價，翻倍停利)
       STOP LOSS 價格    = 現價的一半 (0.5 * 現價，跌損 50% 停損)
  4. 下單成功後自動將三筆下單詳情與 Attached 價格推播至手機 LINE
================================================================================
"""

import os
import sys
import re
import time
import math
import random
import asyncio
import datetime
import argparse

# 確保 Windows 主控台正確輸出 UTF-8
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")
BARCHART_DIR = os.path.join(BASE_DIR, "Barchart")
REPORTS_DIR = os.path.join(BARCHART_DIR, "reports")

if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    from ib_insync import IB, Option, Contract, ComboLeg, LimitOrder, StopOrder, TagValue
except ImportError:
    print("[ERROR] 請先安裝 ib_insync (pip install ib_insync)")
    sys.exit(1)

try:
    from notifier import send_push_message
except ImportError:
    send_push_message = None


# ==============================================================================
# 1. 讀取 .env 設定 (IBKR 連線與帳號)
# ==============================================================================
def load_env_settings(env_path=ENV_FILE):
    cfg = {
        "IB_HOST": "127.0.0.1",
        "IB_PORT": 4001,
        "IB_TARGET_ACCOUNT": "",
        "OP_SEND_WEBHOOK": "true",
    }
    if not os.path.exists(env_path):
        return cfg

    with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip().upper()
            v = v.strip().strip('"').strip("'")
            if k == "IB_HOST":
                cfg["IB_HOST"] = v
            elif k == "IB_PORT":
                try:
                    cfg["IB_PORT"] = int(v)
                except ValueError:
                    pass
            elif k == "IB_TARGET_ACCOUNT":
                cfg["IB_TARGET_ACCOUNT"] = v
            elif k in ("OP_SEND_WEBHOOK", "SEND_WEBHOOK"):
                cfg["OP_SEND_WEBHOOK"] = v.lower()
    return cfg


# ==============================================================================
# 2. 解析 latest_ai_analysis.txt 報告中的三大建議
# ==============================================================================
def find_latest_report_file():
    candidates = [
        os.path.join(BASE_DIR, "latest_ai_analysis.txt"),
        os.path.join(BARCHART_DIR, "latest_ai_analysis.txt"),
        os.path.join(REPORTS_DIR, "latest_ai_analysis.txt"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p

    # Fallback to newest in reports/
    reports = glob.glob(os.path.join(REPORTS_DIR, "ai_analysis_*.txt"))
    if reports:
        reports.sort(key=os.path.getmtime, reverse=True)
        return reports[0]
    return None


def parse_report_suggestions(report_text):
    """
    自 AI 分析報告內容中結構化萃取三項建議：
      1. 個股突破交易 (單腿期權)
      2. ETF 趨勢/宏觀對沖 (單腿期權)
      3. Bull Put 垂直價差最佳組合 (雙腿價差單)
    """
    suggestions = []
    sections = re.split(r'####\s*📌?\s*【(?:投資建議|投资建议)[一二三123]', report_text)
    if len(sections) < 2:
        # Fallback split
        sections = re.split(r'【(?:投資建議|投资建议)[一二三123]', report_text)

    for i, s in enumerate(sections[1:], 1):
        # 標的代號
        sym_match = re.search(r'推薦標的代號[^\w]*([A-Za-z0-9]+)', s)
        symbol = sym_match.group(1).strip().upper() if sym_match else None

        # 建議策略
        strat_match = re.search(r'建議策略[^\w]*([A-Za-z\s]+)', s)
        raw_strat = strat_match.group(1).strip() if strat_match else ""

        is_call = "CALL" in raw_strat.upper()
        is_put = "PUT" in raw_strat.upper()

        # 到期日
        exp_match = re.search(r'到期日[^\d]*(\d{4}[-/]\d{2}[-/]\d{2})', s)
        exp_date = exp_match.group(1).replace("-", "").replace("/", "") if exp_match else None

        is_bull_put = ("BULL PUT" in s.upper()) or ("垂直價差" in s) or ("SPREAD" in s.upper())
        if is_bull_put and ("賣出" in s or "SHORT STRIKE" in s.upper() or "SPREAD" in s.upper()):
            # Bull Put Spread (垂直價差組合單)
            short_put = None
            long_put = None
            ref_credit = None

            for line in s.split("\n"):
                line_clean = line.strip()
                if any(k in line_clean for k in ["賣出下翼", "Leg 1 Short", "Short Strike", "Short Put"]):
                    after = line_clean.split("：")[-1] if "：" in line_clean else line_clean.split(":")[-1]
                    m = re.search(r'(?:Strike\s*)?\$?(\d+\.?\d*)', after.replace("*", "").strip(), re.IGNORECASE)
                    if m and float(m.group(1)) > 0:
                        short_put = float(m.group(1))
                    else:
                        m2 = re.search(r'Strike\s*[:\*\$]*\s*(\d+\.?\d*)', line_clean, re.IGNORECASE)
                        if m2:
                            short_put = float(m2.group(1))

                elif any(k in line_clean for k in ["買入保護", "Leg 2 Long", "Long Strike", "Long Put"]):
                    after = line_clean.split("：")[-1] if "：" in line_clean else line_clean.split(":")[-1]
                    m = re.search(r'(?:Strike\s*)?\$?(\d+\.?\d*)', after.replace("*", "").strip(), re.IGNORECASE)
                    if m and float(m.group(1)) > 0:
                        long_put = float(m.group(1))
                    else:
                        m2 = re.search(r'Strike\s*[:\*\$]*\s*(\d+\.?\d*)', line_clean, re.IGNORECASE)
                        if m2:
                            long_put = float(m2.group(1))

                elif any(k in line_clean for k in ["淨權利金收入", "Net Credit", "淨權利金", "淨收入權利金", "淨信用"]) and ref_credit is None:
                    after = line_clean.split("：")[-1] if "：" in line_clean else line_clean.split(":")[-1]
                    m = re.search(r'\$?(\d+\.?\d*)', after.replace("*", "").strip())
                    if m and float(m.group(1)) > 0:
                        ref_credit = float(m.group(1))

            # 備援 1: 單行描述 (例如: "賣出 727.00 Put / 買入 710.00 Put")
            if not short_put or not long_put:
                bp_inline = re.search(r'賣出[^\d]*(\d+\.?\d*)\s*(?:塊|點)?\s*(?:Strike\s*)?Put.*?買入[^\d]*(\d+\.?\d*)\s*(?:塊|點)?\s*(?:Strike\s*)?Put', s, re.IGNORECASE)
                if bp_inline:
                    short_put = float(bp_inline.group(1))
                    long_put = float(bp_inline.group(2))

            # 備援 2: 自報告總覽摘要行提取
            if not short_put or not long_put:
                bp_summary = re.search(r'Bull\s*Put.*?賣出\s*\*?\$?(\d+\.?\d*)\s*Put.*?買入\s*\*?\$?(\d+\.?\d*)\s*Put', report_text, re.IGNORECASE)
                if bp_summary:
                    short_put = float(bp_summary.group(1))
                    long_put = float(bp_summary.group(2))

            # 備援 3: 自該段文字所有 Strike 數值中排序取前兩大
            if not short_put or not long_put:
                strikes_found = [float(x) for x in re.findall(r'Strike\s*[:\*\$]*\s*(\d+\.?\d*)', s, re.IGNORECASE) if float(x) > 0]
                if len(strikes_found) >= 2:
                    s1, s2 = strikes_found[0], strikes_found[1]
                    short_put = max(s1, s2)
                    long_put = min(s1, s2)

            suggestions.append({
                "id": i,
                "type": "bull_put",
                "symbol": symbol,
                "strategy": "Bull Put Spread",
                "action": "SELL",
                "exp_date": exp_date,
                "short_put_strike": short_put,
                "long_put_strike": long_put,
                "ref_price": ref_credit,
                "raw_strategy": raw_strat or "Bull Put Spread",
            })
        else:
            # 單腿選擇權 (Buy Call / Buy Put / Sell Put)
            strike_match = re.search(r'(?:履約價|Strike)[^\n:]*[：:]\s*[\*\$]*\s*(\d+\.?\d*)', s, re.IGNORECASE)
            strike = float(strike_match.group(1)) if strike_match else None
            if not strike:
                m_st = re.search(r'Strike\s*[:\*\$]*\s*(\d+\.?\d*)', s, re.IGNORECASE)
                if m_st:
                    strike = float(m_st.group(1))

            # 抓取參考現價或權利金 (支援 "Ask 僅 $0.51", "Ask: $0.51", "最新成交價 / Ask: $0.52 / $0.55")
            ref_match = re.search(r'Ask[^\$\d\n]*\$?(\d+\.?\d*)', s, re.IGNORECASE)
            if not ref_match:
                ref_match = re.search(r'(?:最新成交價|權利金)[^\$\d\n]*\$?(\d+\.?\d*)', s, re.IGNORECASE)
            ref_p = float(ref_match.group(1)) if ref_match else None

            action = "SELL" if "SELL" in raw_strat.upper() else "BUY"
            right = "P" if is_put else "C"

            suggestions.append({
                "id": i,
                "type": "single_option",
                "symbol": symbol,
                "strategy": f"{action} {'Call' if right == 'C' else 'Put'}",
                "action": action,
                "right": right,
                "exp_date": exp_date,
                "strike": strike,
                "ref_price": ref_p,
                "raw_strategy": raw_strat,
            })

    # 若從詳細區塊中未能解析出完整 3 項建議，啟動速覽摘要備援解析
    if len(suggestions) < 3:
        suggestions = parse_from_summary_fallback(report_text, suggestions)

    return suggestions


def parse_from_summary_fallback(report_text, existing_suggestions=None):
    """
    備援機制：若無法從詳細分析段落解析出完整 3 項建議，
    嘗試自【手機速覽摘要】或條列行中補足缺失的建議項目。
    """
    existing_ids = {item["id"] for item in (existing_suggestions or [])}
    found = list(existing_suggestions or [])

    lines = report_text.split("\n")
    for line in lines:
        line_clean = line.strip()
        if not line_clean:
            continue

        # 建議 1 / 建議 2：單腿期權
        if any(k in line_clean for k in ["個股突破", "ETF 趨勢", "個股", "ETF"]) and any(s in line_clean for s in ["Buy", "Call", "Put", "看多", "看空"]):
            cand_id = 2 if any(k in line_clean for k in ["ETF", "趨勢", "2."]) else 1
            if cand_id in existing_ids:
                continue

            m_sym = re.search(r'[\*`]*([A-Z]{1,5})[\*`]*\s*(?:【|\(|\||:|\s*Sell|\s*Buy)', line_clean)
            if not m_sym:
                m_sym = re.search(r'：\s*[\*`]*([A-Z]{1,5})[\*`]*', line_clean)
            sym = m_sym.group(1).strip() if m_sym else None

            strat_str = line_clean.upper()
            is_put = "PUT" in strat_str or "看空" in line_clean
            right = "P" if is_put else "C"
            action = "SELL" if "SELL" in strat_str else "BUY"

            m_strike = re.search(r'(?:履約價|Strike)[^\d]*\$?(\d+\.?\d*)', line_clean, re.IGNORECASE)
            strike = float(m_strike.group(1)) if m_strike else None

            m_exp = re.search(r'(\d{4}[-/]\d{2}[-/]\d{2})', line_clean)
            exp_date = m_exp.group(1).replace("-", "").replace("/", "") if m_exp else None

            if sym and strike and exp_date:
                found.append({
                    "id": cand_id,
                    "type": "single_option",
                    "symbol": sym,
                    "strategy": f"{action} {'Call' if right == 'C' else 'Put'}",
                    "action": action,
                    "right": right,
                    "exp_date": exp_date,
                    "strike": strike,
                    "ref_price": None,
                    "raw_strategy": f"{action} {'Call' if right == 'C' else 'Put'}",
                })
                existing_ids.add(cand_id)

        # 建議 3：Bull Put 垂直價差
        elif any(k in line_clean for k in ["Bull Put", "垂直價差", "雙賣"]):
            cand_id = 3
            if cand_id in existing_ids:
                continue

            m_sym = re.search(r'[\*`]*([A-Z]{1,5})[\*`]*\s*(?:【|\(|\||:|\s*Sell|\s*Buy)', line_clean)
            if not m_sym:
                m_sym = re.search(r'：\s*[\*`]*([A-Z]{1,5})[\*`]*', line_clean)
            sym = m_sym.group(1).strip() if m_sym else None

            m_strikes = re.findall(r'(?:賣出|Sell|買入|Buy|Strike)[^\d]*(\d+\.?\d*)', line_clean, re.IGNORECASE)
            s_nums = [float(x) for x in m_strikes if float(x) > 0]
            if len(s_nums) >= 2:
                short_p = max(s_nums[0], s_nums[1])
                long_p = min(s_nums[0], s_nums[1])
            else:
                short_p, long_p = None, None

            m_exp = re.search(r'(\d{4}[-/]\d{2}[-/]\d{2})', line_clean)
            exp_date = m_exp.group(1).replace("-", "").replace("/", "") if m_exp else None

            if sym and short_p and long_p and exp_date:
                found.append({
                    "id": 3,
                    "type": "bull_put",
                    "symbol": sym,
                    "strategy": "Bull Put Spread",
                    "action": "SELL",
                    "exp_date": exp_date,
                    "short_put_strike": short_p,
                    "long_put_strike": long_p,
                    "ref_price": None,
                    "raw_strategy": "Bull Put Spread",
                })
                existing_ids.add(cand_id)

    found.sort(key=lambda x: x["id"])
    return found


# ==============================================================================
# 3. 連線至 IBKR (繞過重型同步，以毫秒級快速連線)
# ==============================================================================
def create_fast_ib_connection(host="127.0.0.1", port=4001, client_id=None):
    if client_id is None:
        client_id = random.randint(7100, 7900)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    ib = IB()
    # 攔截異步訂閱以防 TWS 連線逾時
    ib.reqPositionsAsync = lambda: asyncio.sleep(0)
    ib.reqAccountUpdatesAsync = lambda acct: asyncio.sleep(0)
    ib.reqAccountUpdatesMultiAsync = lambda acct: asyncio.sleep(0)
    ib.reqOpenOrdersAsync = lambda: asyncio.sleep(0)
    ib.reqCompletedOrdersAsync = lambda apiOnly: asyncio.sleep(0)
    ib.reqExecutionsAsync = lambda: asyncio.sleep(0)

    print(f"[INFO] 正在連線至 IBKR TWS/Gateway ({host}:{port}, ClientId: {client_id}) ...")
    ib.connect(host, port, clientId=client_id, timeout=12)
    print(f"[SUCCESS] ✅ 成功建立 IBKR 連線！")
    return ib


# ==============================================================================
# 4. 預查現價並組裝 Adaptive Patient + Attached Bracket 訂單
# ==============================================================================
def process_and_place_suggestion(ib, item, target_account=None, dry_run=False):
    """
    針對單項建議進行：
      1. 合約建立與驗證
      2. 預查即時現價 (Market Price / Bid / Ask)
      3. 計算 Profit Taker 與 Stop Loss 價格
      4. 建立 Adaptive Patient + Attached Orders
      5. 送單至 IBKR (若 dry_run=False)
    """
    s_id = item["id"]
    stype = item["type"]
    symbol = item["symbol"]
    exp_date = item["exp_date"]
    action = item["action"]
    ref_price = item.get("ref_price") or 1.0

    print(f"\n" + "-" * 60)
    print(f"👉 【處理建議 {s_id}】{symbol} - {item['strategy']} (到期日: {exp_date})")
    print("-" * 60)

    order_records = []

    if stype == "single_option":
        strike = item.get("strike")
        right = item.get("right")

        if not strike or not exp_date or not right:
            err = f"❌ [合約參數缺失] 無法下單 {symbol}: Strike={strike}, ExpDate={exp_date}, Right={right}"
            print(err)
            return {"status": "error", "message": err, "item": item}

        contract = Option(symbol, exp_date, strike, right, "SMART")
        qualified = ib.qualifyContracts(contract)
        if not qualified or not contract.conId:
            err = f"❌ [合約無效] 無法在 IBKR 驗證合約: {symbol} {exp_date} {right}{strike}"
            print(err)
            return {"status": "error", "message": err, "item": item}

        print(f"  -> 合約驗證成功: {contract.localSymbol} (conId: {contract.conId})")

        # 預查現價
        ticker = ib.reqMktData(contract, "", False, False)
        ib.sleep(2)

        # 判定現價
        current_price = 0.0
        if action == "BUY":
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
        else:  # SELL
            if ticker.bid and not math.isnan(ticker.bid) and ticker.bid > 0:
                current_price = ticker.bid
            elif ticker.marketPrice() and not math.isnan(ticker.marketPrice()) and ticker.marketPrice() > 0:
                current_price = ticker.marketPrice()
            elif ticker.ask and not math.isnan(ticker.ask) and ticker.ask > 0:
                current_price = ticker.ask
            elif ticker.close and not math.isnan(ticker.close) and ticker.close > 0:
                current_price = ticker.close
            else:
                current_price = ref_price

        if current_price is None or math.isnan(current_price) or current_price <= 0:
            current_price = ref_price or 1.00

        current_price = max(0.05, round(current_price, 2))
        print(f"  -> 預查現價成功: ${current_price:.2f} (即時Bid: {ticker.bid}, Ask: {ticker.ask}, 參考價: {ref_price})")

        # 計算 Attached Order 價格
        # 規則：
        # SELL PUT: Profit Taker 現價一半，Stop Loss 現價二倍
        # BUY PUT/CALL: Profit Taker 現價二倍，Stop Loss 現價一半
        if action == "SELL":
            take_profit_price = max(0.01, round(current_price * 0.5, 2))
            stop_loss_price = round(current_price * 2.0, 2)
        else:  # BUY
            take_profit_price = round(current_price * 2.0, 2)
            stop_loss_price = max(0.01, round(current_price * 0.5, 2))

        print(f"  -> 附屬單價格規劃:")
        print(f"     • 🎯 Profit Taker (停利單): ${take_profit_price:.2f}")
        print(f"     • 🛑 Stop Loss    (停損單): ${stop_loss_price:.2f}")

        # 建立 Bracket Order
        bracket = ib.bracketOrder(
            action=action,
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
            f"{item['strategy']} {contract.localSymbol}\n"
            f"     委託: {action} 1口 @ 限價 ${current_price:.2f} (Adaptive Patient)\n"
            f"     停利: ${take_profit_price:.2f} | 停損: ${stop_loss_price:.2f}"
        )

        if not dry_run:
            for o in order_list:
                ib.placeOrder(contract, o)
            ib.sleep(1)
            status = bracket.parent.orderStatus.status if hasattr(bracket.parent, 'orderStatus') else "Submitted"
            print(f"  -> ✅ [已送出委託至 IBKR] 母單狀態: {status}")
        else:
            print(f"  -> 🔍 [模擬模式 (Dry-run)] 不實際送單至交易所")
            status = "DryRun"

        return {
            "status": "ok",
            "symbol": symbol,
            "desc": desc,
            "strategy": item["strategy"],
            "entry_price": current_price,
            "take_profit": take_profit_price,
            "stop_loss": stop_loss_price,
            "order_status": status,
        }

    elif stype == "bull_put":
        # Bull Put 垂直價差 (賣出短腳 Put + 買入長腳 Put)
        short_put = item.get("short_put_strike")
        long_put = item.get("long_put_strike")

        if not short_put or not long_put or not exp_date:
            err = f"❌ [合約參數缺失] 無法下單 Bull Put: Strike={short_put}/{long_put}, ExpDate={exp_date}"
            print(err)
            return {"status": "error", "message": err, "item": item}

        c_short = Option(symbol, exp_date, short_put, "P", "SMART")
        c_long = Option(symbol, exp_date, long_put, "P", "SMART")
        qualified = ib.qualifyContracts(c_short, c_long)
        if len(qualified) < 2:
            err = f"❌ [合約無效] 無法在 IBKR 驗證 Bull Put 雙腿: {symbol} {exp_date} P{short_put} / P{long_put}"
            print(err)
            return {"status": "error", "message": err, "item": item}

        print(f"  -> 雙腿合約驗證成功: 賣出 P{short_put} (conId: {c_short.conId}) / 買入 P{long_put} (conId: {c_long.conId})")

        # 組合單合約
        combo = Contract(secType="BAG", symbol=symbol, currency="USD", exchange="SMART")
        combo.comboLegs = [
            ComboLeg(conId=c_short.conId, ratio=1, action="BUY", exchange="SMART"),
            ComboLeg(conId=c_long.conId, ratio=1, action="SELL", exchange="SMART"),
        ]

        # 預查現價 (合成淨權利金)
        t_short = ib.reqMktData(c_short, "", False, False)
        t_long = ib.reqMktData(c_long, "", False, False)
        t_combo = ib.reqMktData(combo, "", False, False)
        ib.sleep(2)

        credit = 0.0
        if t_combo.bid and not math.isnan(t_combo.bid) and t_combo.bid > 0:
            credit = t_combo.bid
        else:
            s_bid = t_short.bid if (t_short.bid and not math.isnan(t_short.bid) and t_short.bid > 0) else (t_short.close or 0.0)
            l_ask = t_long.ask if (t_long.ask and not math.isnan(t_long.ask) and t_long.ask > 0) else (t_long.close or 0.0)
            if s_bid > l_ask > 0:
                credit = s_bid - l_ask

        if credit <= 0:
            credit = ref_price or 1.50

        credit = max(0.10, round(credit, 2))
        print(f"  -> 預查淨權利金 (現價): ${credit:.2f} (短腿Bid: {t_short.bid}, 長腿Ask: {t_long.ask}, 參考: {ref_price})")

        # Attached 停利與停損 (賣方收租價差：Profit Taker 價格為現價一半，Stop Loss 價格為現價二倍)
        take_profit_price = max(0.01, round(credit * 0.5, 2))
        stop_loss_price = round(credit * 2.0, 2)

        print(f"  -> 附屬單價格規劃:")
        print(f"     • 🎯 Profit Taker (停利單): 買回平倉價 ${take_profit_price:.2f} (收斂 50%)")
        print(f"     • 🛑 Stop Loss    (停損單): 買回止損價 ${stop_loss_price:.2f} (擴大 200%)")

        # 建立組合單母單 + 附屬訂單
        bracket = ib.bracketOrder(
            action="SELL",
            quantity=1,
            limitPrice=credit,
            takeProfitPrice=take_profit_price,
            stopLossPrice=stop_loss_price,
        )
        bracket.parent.algoStrategy = "Adaptive"
        bracket.parent.algoParams = [TagValue("adaptivePriority", "Patient")]
        bracket.parent.tif = "DAY"
        bracket.takeProfit.tif = "GTC"
        bracket.stopLoss.tif = "GTC"

        if target_account:
            bracket.parent.account = target_account
            bracket.takeProfit.account = target_account
            bracket.stopLoss.account = target_account

        desc = (
            f"Bull Put Spread {symbol} P{short_put} / P{long_put} ({exp_date})\n"
            f"     委託: SELL 1手組合單 @ 淨收權利金 ${credit:.2f} (Adaptive Patient)\n"
            f"     停利: 買回價 ${take_profit_price:.2f} | 停損: 買回價 ${stop_loss_price:.2f}"
        )

        if not dry_run:
            # 依序送出母單與附屬單
            for o in [bracket.parent, bracket.takeProfit, bracket.stopLoss]:
                try:
                    ib.placeOrder(combo, o)
                except Exception as oe:
                    print(f"  -> [提示] 附屬單 {o.orderType} 委託反饋: {oe}")
            ib.sleep(1)
            status = bracket.parent.orderStatus.status if hasattr(bracket.parent, 'orderStatus') else "Submitted"
            print(f"  -> ✅ [已送出委託至 IBKR] 母單狀態: {status}")
        else:
            print(f"  -> 🔍 [模擬模式 (Dry-run)] 不實際送單至交易所")
            status = "DryRun"

        return {
            "status": "ok",
            "symbol": symbol,
            "desc": desc,
            "strategy": "Bull Put Spread",
            "entry_price": credit,
            "take_profit": take_profit_price,
            "stop_loss": stop_loss_price,
            "order_status": status,
        }


# ==============================================================================
# 5. 主執行函式：解析報告並逐筆下單
# ==============================================================================
def place_barchart_orders(report_path=None, dry_run=False):
    print("\n" + "=" * 65)
    print("🚀 Barchart AI 三大建議自動下單模組 (Adaptive Patient + Attached)")
    print("=" * 65)

    if not report_path:
        report_path = find_latest_report_file()

    if not report_path or not os.path.exists(report_path):
        err = f"[ERROR] 找不到最新 AI 分析報告檔案 (搜尋路徑: {BASE_DIR}, {BARCHART_DIR}, {REPORTS_DIR})"
        print(err)
        return False

    print(f"[INFO] 讀取分析報告: {report_path}")
    with open(report_path, "r", encoding="utf-8", errors="ignore") as f:
        report_text = f.read()

    suggestions = parse_report_suggestions(report_text)
    if not suggestions:
        print("[ERROR] 無法從報告中解析出任何有效投資建議。")
        return False

    print(f"[INFO] 成功自報告解析出 {len(suggestions)} 筆核心投資建議：")
    for s in suggestions:
        if s["type"] == "bull_put":
            print(f"  • 建議 {s['id']}: {s['symbol']} {s['strategy']} (賣出 P{s['short_put_strike']} / 買入 P{s['long_put_strike']}, 到期: {s['exp_date']})")
        else:
            print(f"  • 建議 {s['id']}: {s['symbol']} {s['strategy']} (履約價: ${s['strike']}, 到期: {s['exp_date']})")

    # 讀取連線參數
    cfg = load_env_settings(ENV_FILE)
    host = cfg["IB_HOST"]
    port = cfg["IB_PORT"]
    target_account = cfg["IB_TARGET_ACCOUNT"]

    ib = None
    results = []
    try:
        ib = create_fast_ib_connection(host=host, port=port)
        for s in suggestions:
            res = process_and_place_suggestion(ib, s, target_account=target_account, dry_run=dry_run)
            results.append(res)

    except Exception as e:
        print(f"[ERROR] 執行下單過程發生異常: {e}")
    finally:
        if ib and ib.isConnected():
            ib.disconnect()
            print("\n[INFO] 已安全斷開 IBKR 連線。")

    # 組裝推播訊息至手機 LINE
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    success_items = [r for r in results if r.get("status") == "ok"]

    line_lines = [
        f"🚀【IBKR Barchart AI 自動下單回報】",
        f"🕒 時間：{now_str}",
        f"⚙️ 模式：{'模擬測試 (Dry-Run)' if dry_run else '正式送單 (Live)'}",
        f"📋 下單筆數：{len(success_items)}/{len(suggestions)} 筆成功",
        "",
    ]

    for i, r in enumerate(success_items, 1):
        line_lines.append(f"📌 建議 {i}：{r['desc']}")

    line_msg = "\n".join(line_lines)

    if send_push_message:
        print(f"\n[INFO] 正在推播下單摘要到手機 LINE...")
        ok = send_push_message(line_msg.strip())
        if ok:
            print("[SUCCESS] ✅ LINE 下單摘要推播成功！")
        else:
            print("[WARN] ⚠️ LINE 推播異常。")

    print("\n" + "=" * 65)
    print("下單執行結果總結：")
    for r in results:
        print(f"  • {r.get('symbol')}: {r.get('status')} - {r.get('desc', r.get('message'))}")
    print("=" * 65 + "\n")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="讀取 Barchart AI 報告並下單 IBKR (Adaptive Patient + Attached Orders)")
    parser.add_argument("--report", type=str, default=None, help="自訂報告檔案路徑")
    parser.add_argument("--dry-run", action="store_true", help="模擬預查市價與價格計算，不實際向 IBKR 送單")
    args = parser.parse_args()

    place_barchart_orders(report_path=args.report, dry_run=args.dry_run)
