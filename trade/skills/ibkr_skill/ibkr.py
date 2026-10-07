#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IBKR Intelligent Order Skill (trade/skills/ibkr_skill/ibkr.py)
================================================================================
職責：
  1. 建立非同步快速連線與載入 .env 交易帳戶參數
  2. 預查美股期權合約合法性與即時市場行情 (Bid / Ask / MarketPrice)
  3. 送出單腿期權 Adaptive Patient 市價母單，掛出 2 倍停利與一半停損 Attached Orders
  4. 送出 Bull Put 垂直價差組合單 (BAG Combo)，整合 ScaleInOrderSkill 防衝突機制 (Error 201)
  5. 委託狀態即時推播至手機 LINE
================================================================================
"""

import os
import sys
import time
import math
import random
import asyncio
import datetime

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ENV_FILE = os.path.join(BASE_DIR, ".env")

try:
    from ib_insync import IB, Option, Contract, ComboLeg, LimitOrder, MarketOrder, StopOrder, TagValue
except ImportError:
    IB = None
    Option = None
    Contract = None
    ComboLeg = None
    LimitOrder = None
    MarketOrder = None
    StopOrder = None
    TagValue = None

try:
    from notifier import send_push_message
except ImportError:
    send_push_message = None

try:
    from skills.walk_up_skill import walk_up_limit_price, execute_walk_up_order
except ImportError:
    try:
        from trade.skills.walk_up_skill import walk_up_limit_price, execute_walk_up_order
    except ImportError:
        walk_up_limit_price = None
        execute_walk_up_order = None

RECENT_IB_ERRORS = []


def on_ib_error(reqId, errorCode, errorString, contract):
    RECENT_IB_ERRORS.append((reqId, errorCode, errorString, contract))
    if errorCode in (110, 201, 103, 321, 200):
        print(f"[IBKR 委託警示] ReqId: {reqId} | 代碼: {errorCode} | 訊息: {errorString}")


def load_env_settings(env_path=ENV_FILE):
    """讀取 .env 中的 IBKR 連線主機、通訊埠與目標帳號"""
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
            elif k == "OP_SEND_WEBHOOK":
                cfg["OP_SEND_WEBHOOK"] = v
    return cfg


def create_fast_ib_connection(host="127.0.0.1", port=4001, client_id=None):
    """快速建立輕量化 IBKR TWS/Gateway 連線"""
    if IB is None:
        raise RuntimeError("未安裝 ib_insync 套件。")

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

    print(f"[INFO] [IBKRSkill] 正在連線至 IBKR TWS/Gateway ({host}:{port}, ClientId: {client_id}) ...")
    ib.connect(host, port, clientId=client_id, timeout=12)
    ib.errorEvent += on_ib_error
    print(f"[SUCCESS] [IBKRSkill] ✅ 成功建立 IBKR 連線！")
    return ib


def check_ibkr_contract_validity(symbol, exp_date, strike, right="C", host="127.0.0.1", port=4001):
    """
    向 IBKR 驗證期權合約合法性與可交易性。
    若 IBKR 連線成功且合約合法，回傳 (True, contract)。
    若查無定義或已到期，回傳 (False, None)。
    若 IBKR 離線，回傳 (True, None) 允許離線流程通行。
    """
    ib = None
    try:
        ib = create_fast_ib_connection(host=host, port=port, client_id=random.randint(9780, 9799))
        if not ib or not ib.isConnected():
            return True, None
    except Exception:
        return True, None

    try:
        contract = Option(symbol, exp_date, strike, right, "SMART")
        qualified = ib.qualifyContracts(contract)
        is_valid = bool(qualified and contract.conId and contract.conId > 0)
        return is_valid, contract if is_valid else None
    except Exception as e:
        print(f"[WARN] [IBKRSkill] 合約驗證過程異常: {e}")
        return False, None
    finally:
        if ib and ib.isConnected():
            ib.disconnect()


def execute_adaptive_option_bracket(contract_info, dry_run=False, env_path=ENV_FILE):
    """
    執行單腿期權 (Buy Call 或 Buy Put) 委託：
      - 預查即時市價
      - 發送 Adaptive Patient 市價母單
      - 掛出 2 倍停利與一半停損 Attached Orders
      - 即時結果推播至手機 LINE
    """
    symbol = contract_info.get("symbol", "").strip().upper()
    strike = float(contract_info.get("strike", 0))
    exp_date = str(contract_info.get("exp_date", "")).strip()
    right = str(contract_info.get("right", "C")).strip().upper()
    action = str(contract_info.get("action", "BUY")).strip().upper()
    ref_price = float(contract_info.get("ref_price", contract_info.get("ask", 1.0)) or 1.0)
    strategy_name = contract_info.get("strategy", f"{action} {('CALL' if right == 'C' else 'PUT')}")

    print("\n" + "=" * 65)
    print(f"🚀 【IBKR 智能下單模組】執行 {symbol} {strategy_name} 委託")
    print("=" * 65)

    if not strike or not exp_date:
        err = f"❌ [合約參數缺失] 無法下單：Symbol={symbol}, Strike={strike}, ExpDate={exp_date}"
        print(err)
        return {"status": "error", "message": err}

    cfg = load_env_settings(env_path)
    host = cfg.get("IB_HOST", "127.0.0.1")
    port = cfg.get("IB_PORT", 4001)
    target_account = cfg.get("IB_TARGET_ACCOUNT", "")

    ib = None
    try:
        ib = create_fast_ib_connection(host=host, port=port, client_id=random.randint(9700, 9799))

        contract = Option(symbol, exp_date, strike, right, "SMART")
        qualified = ib.qualifyContracts(contract)
        if not qualified or not contract.conId:
            err = f"❌ [合約無效] 無法在 IBKR 驗證合約: {symbol} {exp_date} {right}{strike}"
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
        print(f"  -> 預查即時參考價成功: ${current_price:.2f} (即時Bid: {ticker.bid}, Ask: {ticker.ask}, 參考價: {ref_price})")

        # 計算 Attached Orders 價格 (買方策略：2倍停利，0.5倍停損)
        take_profit_price = round(current_price * 2.0, 2)
        stop_loss_price = max(0.01, round(current_price * 0.5, 2))

        print(f"  -> 附屬單規劃:")
        print(f"     • 🎯 Profit Taker (2倍停利單): ${take_profit_price:.2f}")
        print(f"     • 🛑 Stop Loss    (一半停損單): ${stop_loss_price:.2f}")

        # 組裝 Bracket Order
        bracket = ib.bracketOrder(
            action=action,
            quantity=1,
            limitPrice=current_price,
            takeProfitPrice=take_profit_price,
            stopLossPrice=stop_loss_price,
        )

        bracket.parent.orderType = "LMT"
        bracket.parent.lmtPrice = current_price
        bracket.parent.tif = "DAY"
        bracket.takeProfit.tif = "GTC"
        bracket.stopLoss.tif = "GTC"

        if target_account:
            bracket.parent.account = target_account
            bracket.takeProfit.account = target_account
            bracket.stopLoss.account = target_account

        order_list = [bracket.parent, bracket.takeProfit, bracket.stopLoss]
        desc = (
            f"{action} {contract.localSymbol}\n"
            f"     委託: {action} 1口 @ Custom Walk-Up (起始限價 ${current_price:.2f})\n"
            f"     🎯 停利 (2倍): ${take_profit_price:.2f} | 🛑 停損 (一半): ${stop_loss_price:.2f}"
        )

        if not dry_run:
            parent_trade = None
            for o in order_list:
                tr = ib.placeOrder(contract, o)
                if o == bracket.parent:
                    parent_trade = tr
            ib.sleep(0.5)

            if walk_up_limit_price:
                print(f"⏳ [自適應步進修單] 啟動 Custom Walk-Up 步進調整 {contract.localSymbol} 括號母單...")
                is_filled = walk_up_limit_price(
                    ib=ib,
                    contract=contract,
                    order=bracket.parent,
                    current_mid=current_price,
                    max_slippage=0.15,
                    step=0.05,
                    step_time=3.0,
                    max_steps=3,
                    symbol=f"{contract.localSymbol}-Bracket",
                    trade=parent_trade
                )
                status = "Filled" if is_filled else (parent_trade.orderStatus.status if parent_trade else "Cancelled")
            else:
                ib.sleep(1)
                status = parent_trade.orderStatus.status if (parent_trade and hasattr(parent_trade, "orderStatus")) else "Submitted"
            print(f"  -> ✅ [已送出自適應步進委託至 IBKR] 母單狀態: {status}")
        else:
            print(f"  -> 🔍 [模擬模式 (Dry-Run)] 不實際送單至交易所")
            status = "DryRun"

        # 推播下單詳情報告至 LINE
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line_msg = f"""🚀【IBKR 期權自動下單回報】
🕒 時間：{now_str}
⚙️ 模式：{'模擬測試 (Dry-Run)' if dry_run else '正式送單 (Live)'}
📋 合約：{contract.localSymbol} (conId: {contract.conId})
💵 參考現價：${current_price:.2f}
🎯 停利單 (2倍)：${take_profit_price:.2f}
🛑 停損單 (一半)：${stop_loss_price:.2f}
⚡ 委託方式：自適應步進修單 Custom Walk-Up
📊 委託狀態：{status}"""

        if send_push_message:
            print(f"[INFO] [IBKRSkill] 正在推播下單詳情至手機 LINE...")
            ok = send_push_message(line_msg.strip())
            if ok:
                print(f"[SUCCESS] [IBKRSkill] ✅ 下單回報 LINE 推播成功！")
            else:
                print(f"[WARN] [IBKRSkill] ⚠️ LINE 推播異常。")

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
        print(f"[ERROR] [IBKRSkill] 執行下單異常: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        if ib and ib.isConnected():
            ib.disconnect()
            print("[INFO] [IBKRSkill] 已安全斷開 IBKR 連線。")
