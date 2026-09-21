#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Position Order Skill (trade/skills/position_order_skill/position_order.py)
================================================================================
職責：
  1. 接收標的代號 (例如 TAV26) 與方向訊號 ("BUY" 或 "SELL")
  2. 智能解析並驗證 IBKR 合約 (支援期貨別名如 TA -> MET、到期月份轉換及股票)
  3. 查詢目前持有此標的之未平倉部位淨口數 (current_pos)
  4. 判斷是否已達標：
     - 若為 BUY 且未平倉已為 +1：不動作 (已達標)
     - 若為 SELL 且未平倉已為 -1：不動作 (已達標)
     - 若未達標：計算差額，以 Adaptive Patient 演算法下單送出以達標
  5. 支援 Live 下單、Dry-run 模擬以及 LINE 推播通知
================================================================================
"""

import os
import sys
import re
import time
import math
import datetime

# 確保路徑正常匯入
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    from ib_insync import IB, Contract, Future, Stock, LimitOrder, MarketOrder, TagValue
except ImportError:
    IB = None
    Contract = None
    Future = None
    Stock = None
    LimitOrder = None
    MarketOrder = None
    TagValue = None

try:
    from barchart_placeOrder import create_fast_ib_connection, load_env_settings
except ImportError:
    create_fast_ib_connection = None

    def load_env_settings(env_path=None):
        return {
            "IB_HOST": "127.0.0.1",
            "IB_PORT": 4001,
            "IB_TARGET_ACCOUNT": "",
        }

try:
    from notifier import send_push_message
except ImportError:
    send_push_message = None

ENV_FILE = os.path.join(BASE_DIR, ".env")

# 期貨月份代碼表
FUTURES_MONTH_MAP = {
    "F": "01", "G": "02", "H": "03", "J": "04",
    "K": "05", "M": "06", "N": "07", "Q": "08",
    "U": "09", "V": "10", "X": "11", "Z": "12"
}

# Barchart 代碼至 IBKR 代碼/交易所映射表
BARCHART_TO_IBKR_FUTURES = {
    "TA": ("MET", "CME"),     # Micro Ether
    "MET": ("MET", "CME"),
    "ETH": ("ETH", "CME"),
    "BTC": ("BRR", "CME"),
    "MBT": ("MBT", "CME"),
    "ES": ("ES", "CME"),
    "MES": ("MES", "CME"),
    "NQ": ("NQ", "CME"),
    "MNQ": ("MNQ", "CME"),
    "CL": ("CL", "NYMEX"),
    "MCL": ("MCL", "NYMEX"),
    "GC": ("GC", "COMEX"),
    "MGC": ("MGC", "COMEX"),
    "HG": ("HG", "COMEX"),
    "MHG": ("MHG", "COMEX"),
    "NG": ("NG", "NYMEX"),
    "MNG": ("MNG", "NYMEX"),
    "YC": ("YC", "CBOT"),
    "XC": ("YC", "CBOT"),
    "ZC": ("ZC", "CBOT"),
    "ZW": ("ZW", "CBOT"),
    "ZS": ("ZS", "CBOT"),
    "6E": ("EUR", "CME"),
    "M6E": ("M6E", "CME"),
    "6J": ("JPY", "CME"),
    "MJY": ("MJY", "CME"),
    "VXM": ("VXM", "CFE"),
    "TAV": ("MET", "CME"),
}


class PositionOrderSkill:
    """
    IBKR 部位校驗與 Adaptive Patient 調倉技能
    """

    def __init__(self, ib=None, env_path=None):
        self.ib = ib
        self.owns_ib = False
        self.env_path = env_path or ENV_FILE
        self.cfg = load_env_settings(self.env_path)

    def ensure_ib(self):
        """
        確保有可用的 IBKR 連線
        """
        if self.ib is None or not self.ib.isConnected():
            if create_fast_ib_connection is None:
                raise RuntimeError("無法載入 create_fast_ib_connection，請確認 ib_insync 已安裝。")
            host = self.cfg.get("IB_HOST", "127.0.0.1")
            port = int(self.cfg.get("IB_PORT", 4001))
            self.ib = create_fast_ib_connection(host=host, port=port)
            self.owns_ib = True
        return self.ib

    def close(self):
        """
        若本實例自行建立了 IBKR 連線，則斷線關閉
        """
        if self.owns_ib and self.ib and self.ib.isConnected():
            try:
                self.ib.disconnect()
            except Exception:
                pass
            self.ib = None
            self.owns_ib = False

    def resolve_contract(self, raw_symbol: str) -> Contract:
        """
        將輸入之代號 (例如 TAV26、ESZ26、AAPL 等) 轉換為 IBKR 驗證後的合格合約。
        """
        ib = self.ensure_ib()
        sym = raw_symbol.strip().upper()

        # 1. 嘗試解析期貨格式：Root + MonthCode + Year (例如 TAV26 -> Root=TA, Month=V, Year=26)
        m = re.match(r'^([A-Z0-9]+?)([FGHJKMNQUVXZ])(\d{1,2})$', sym)
        if m:
            root = m.group(1)
            m_code = m.group(2)
            y_str = m.group(3)

            month_num = FUTURES_MONTH_MAP.get(m_code, "12")
            year_full = (2000 + int(y_str)) if len(y_str) == 2 else (2020 + int(y_str))
            expiry = f"{year_full}{month_num}"

            ib_root, exchange = BARCHART_TO_IBKR_FUTURES.get(root, (root, "CME"))
            print(f"[INFO] [PositionSkill] 解析期貨代碼: 原標的={sym} ➔ IBKR={ib_root} 到期月={expiry} 交易所={exchange}")

            c_fut = Future(ib_root, expiry, exchange, currency="USD")
            qualified = ib.qualifyContracts(c_fut)
            if qualified and c_fut.conId:
                print(f"[SUCCESS] ✅ 期貨合約驗證成功: {c_fut.localSymbol} (conId: {c_fut.conId})")
                return c_fut

        # 2. 嘗試直接以 localSymbol 驗證期貨
        try:
            c_local = Contract(secType="FUT", localSymbol=sym, exchange="SMART", currency="USD")
            qualified = ib.qualifyContracts(c_local)
            if qualified and c_local.conId:
                print(f"[SUCCESS] ✅ 本地代碼期貨驗證成功: {c_local.localSymbol} (conId: {c_local.conId})")
                return c_local
        except Exception:
            pass

        # 3. 嘗試以股票合約驗證
        try:
            c_stk = Stock(sym, "SMART", "USD")
            qualified = ib.qualifyContracts(c_stk)
            if qualified and c_stk.conId:
                print(f"[SUCCESS] ✅ 股票合約驗證成功: {c_stk.symbol} (conId: {c_stk.conId})")
                return c_stk
        except Exception:
            pass

        raise ValueError(f"無法在 IBKR 驗證合約代號: {sym}")

    def get_current_position(self, target_contract: Contract) -> float:
        """
        查詢當前帳戶中與目標合約相符之未平倉淨部位口數 (可正可負或0)
        """
        ib = self.ensure_ib()
        target_account = self.cfg.get("IB_TARGET_ACCOUNT", "")

        ib.reqPositions()
        ib.sleep(1)
        all_positions = ib.positions()

        matched_qty = 0.0
        for p in all_positions:
            if target_account and p.account != target_account:
                continue

            # 依據 conId 精準匹配，或備援比對 localSymbol
            is_match = False
            if target_contract.conId and p.contract.conId == target_contract.conId:
                is_match = True
            elif target_contract.localSymbol and p.contract.localSymbol:
                if target_contract.localSymbol.upper() == p.contract.localSymbol.upper():
                    is_match = True
            elif target_contract.symbol and p.contract.symbol:
                if target_contract.symbol.upper() == p.contract.symbol.upper() and p.contract.secType == target_contract.secType:
                    is_match = True

            if is_match:
                matched_qty += float(p.position)

        return matched_qty

    def adjust_position(self, symbol: str, signal: str, dry_run: bool = False) -> dict:
        """
        依據方向訊號 (BUY / SELL) 檢查與調倉至目標部位：
          • signal == "BUY"  ➔ 目標淨部位: +1
          • signal == "SELL" ➔ 目標淨部位: -1
        """
        sig_upper = signal.strip().upper()
        if "BUY" in sig_upper:
            target_pos = 1.0
            sig_name = "BUY"
        elif "SELL" in sig_upper:
            target_pos = -1.0
            sig_name = "SELL"
        else:
            return {
                "status": "ignored",
                "symbol": symbol,
                "signal": signal,
                "message": f"訊號為 {signal}，非 BUY/SELL，不執行調倉。"
            }

        print(f"\n" + "=" * 60)
        print(f"📊 [PositionSkill] 開始持倉檢查與 Adaptive Patient 委託調度")
        print(f"   • 目標標的: {symbol}")
        print(f"   • 方向訊號: 【{sig_name}】")
        print(f"   • 目標淨部位: {target_pos:+g} 口")
        print(f"   • 運行模式: {'模擬測試 (Dry-Run)' if dry_run else '正式送單 (Live)'}")
        print("=" * 60)

        ib = self.ensure_ib()
        target_account = self.cfg.get("IB_TARGET_ACCOUNT", "")

        # 1. 驗證合約
        contract = self.resolve_contract(symbol)

        # 2. 查詢當前未平倉部位
        current_pos = self.get_current_position(contract)
        print(f"[INFO] [PositionSkill] {contract.localSymbol} 當前未平倉部位: {current_pos:+g} 口")

        # 3. 判斷是否已達標
        if current_pos == target_pos:
            msg = f"🎉 標的 {contract.localSymbol} 當前部位已為 {current_pos:+g} 口，完全符合 {sig_name} 目標 ({target_pos:+g})，無須重複下單！"
            print(f"[SUCCESS] ✅ {msg}")
            return {
                "status": "already_target",
                "symbol": symbol,
                "contract": contract.localSymbol,
                "conId": contract.conId,
                "signal": sig_name,
                "current_pos": current_pos,
                "target_pos": target_pos,
                "order_placed": False,
                "message": msg,
            }

        # 4. 計算所需下單方向與口數
        # 例如：當前 0 口，目標 +1 ➔ BUY 1 口
        # 例如：當前 -1 口，目標 +1 ➔ BUY 2 口
        # 例如：當前 +1 口，目標 -1 ➔ SELL 2 口
        diff = target_pos - current_pos
        order_action = "BUY" if diff > 0 else "SELL"
        order_qty = abs(int(round(diff)))

        print(f"[INFO] [PositionSkill] 部位需調整: 當前 {current_pos:+g} ➔ 目標 {target_pos:+g} 口 (差額: {diff:+g})")
        print(f"       -> 預備送出委託: {order_action} {order_qty} 口 (Adaptive Patient)")

        # 5. 預查現價
        ticker = ib.reqMktData(contract, "", False, False)
        ib.sleep(2)

        market_price = None
        if order_action == "BUY":
            if ticker.ask and not math.isnan(ticker.ask) and ticker.ask > 0:
                market_price = ticker.ask
            elif ticker.marketPrice() and not math.isnan(ticker.marketPrice()) and ticker.marketPrice() > 0:
                market_price = ticker.marketPrice()
            elif ticker.close and not math.isnan(ticker.close) and ticker.close > 0:
                market_price = ticker.close
        else:
            if ticker.bid and not math.isnan(ticker.bid) and ticker.bid > 0:
                market_price = ticker.bid
            elif ticker.marketPrice() and not math.isnan(ticker.marketPrice()) and ticker.marketPrice() > 0:
                market_price = ticker.marketPrice()
            elif ticker.close and not math.isnan(ticker.close) and ticker.close > 0:
                market_price = ticker.close

        # 6. 組裝 Adaptive Patient 委託
        if market_price and market_price > 0:
            limit_p = round(market_price, 2)
            order = LimitOrder(order_action, order_qty, limit_p)
            price_desc = f"限價 ${limit_p:.2f}"
        else:
            order = MarketOrder(order_action, order_qty)
            price_desc = "市價"

        order.algoStrategy = "Adaptive"
        order.algoParams = [TagValue("adaptivePriority", "Patient")]
        order.tif = "DAY"
        if target_account:
            order.account = target_account

        order_desc = f"{order_action} {order_qty} 口 {contract.localSymbol} @ {price_desc} (Adaptive Patient)"

        # 7. 執行下單
        order_status = "DryRun"
        if not dry_run:
            trade = ib.placeOrder(contract, order)
            ib.sleep(1)
            order_status = trade.orderStatus.status if hasattr(trade, "orderStatus") else "Submitted"
            print(f"[SUCCESS] ✅ [已送單至 IBKR] 委託狀態: {order_status} | {order_desc}")
        else:
            print(f"[INFO] 🔍 [模擬模式 (Dry-Run)] 未實際送單至 IBKR | {order_desc}")

        # 8. 組裝推播通知
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line_msg = (
            f"🎯【Barchart Opinion 自動調倉回報】\n"
            f"🕒 時間：{now_str}\n"
            f"📌 標的：{symbol} ({contract.localSymbol})\n"
            f"🧭 方向訊號：{sig_name}\n"
            f"📊 部位校正：{current_pos:+g} 口 ➔ 目標 {target_pos:+g} 口\n"
            f"🚀 執行委託：{order_desc}\n"
            f"⚙️ 模式：{'模擬測試 (Dry-Run)' if dry_run else '正式送單 (Live)'}\n"
            f"📋 狀態：{order_status}"
        )

        if send_push_message:
            print(f"[INFO] [PositionSkill] 正在發送 LINE 調倉通知...")
            try:
                ok = send_push_message(line_msg)
                if ok:
                    print("[SUCCESS] ✅ LINE 推播發送成功！")
            except Exception as le:
                print(f"[WARN] LINE 推播異常: {le}")

        return {
            "status": "ok",
            "symbol": symbol,
            "contract": contract.localSymbol,
            "conId": contract.conId,
            "signal": sig_name,
            "current_pos": current_pos,
            "target_pos": target_pos,
            "order_placed": True,
            "action": order_action,
            "qty": order_qty,
            "order_desc": order_desc,
            "order_status": order_status,
            "message": f"成功執行調倉委託: {order_desc}",
        }
