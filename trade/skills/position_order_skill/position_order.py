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
    from trade.skills.ibkr_skill import create_fast_ib_connection, load_env_settings
except ImportError:
    try:
        from skills.ibkr_skill import create_fast_ib_connection, load_env_settings
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

try:
    from skills.walk_up_skill import walk_up_limit_price, execute_walk_up_order
except ImportError:
    try:
        from trade.skills.walk_up_skill import walk_up_limit_price, execute_walk_up_order
    except ImportError:
        walk_up_limit_price = None
        execute_walk_up_order = None

ENV_FILE = os.path.join(BASE_DIR, ".env")

# 期貨月份代碼表
FUTURES_MONTH_MAP = {
    "F": "01", "G": "02", "H": "03", "J": "04",
    "K": "05", "M": "06", "N": "07", "Q": "08",
    "U": "09", "V": "10", "X": "11", "Z": "12"
}

# Barchart 代碼至 IBKR 代碼/交易所映射表
BARCHART_TO_IBKR_FUTURES = {
    # 日圓期貨 (Barchart J6/6J -> IBKR symbols: MJY)
    "J6": ("MJY", "CME"),
    "6J": ("MJY", "CME"),
    "JPY": ("MJY", "CME"),
    "MJY": ("MJY", "CME"),
    "JP": ("MJY", "CME"),
    "JPU": ("MJY", "CME"),

    # 歐元期貨 (Barchart E6/6E -> IBKR symbols: M6E)
    "E6": ("M6E", "CME"),
    "6E": ("M6E", "CME"),
    "EUR": ("M6E", "CME"),
    "M6E": ("M6E", "CME"),
    "EUU": ("M6E", "CME"),

    # 加密貨幣
    "TA": ("MET", "CME"),     # Micro Ether
    "TAV": ("MET", "CME"),
    "MET": ("MET", "CME"),
    "ETH": ("ETH", "CME"),
    "BTC": ("BRR", "CME"),
    "MBT": ("MBT", "CME"),
    "BA": ("PBT", "CFE"),
    "PET": ("PET", "CFE"),
    "PBT": ("PBT", "CFE"),

    # 指數期貨
    "ES": ("MES", "CME"),
    "MES": ("MES", "CME"),
    "NQ": ("MNQ", "CME"),
    "MNQ": ("MNQ", "CME"),
    "RTY": ("M2K", "CME"),
    "M2K": ("M2K", "CME"),
    "YM": ("MYM", "CBOT"),
    "MYM": ("MYM", "CBOT"),

    # 能源與金屬
    "CL": ("MCL", "NYMEX"),
    "MCL": ("MCL", "NYMEX"),
    "GC": ("MGC", "COMEX"),
    "MGC": ("MGC", "COMEX"),
    "GS": ("1OZ", "COMEX"),
    "1OZ": ("1OZ", "COMEX"),
    "HG": ("MHG", "COMEX"),
    "MHG": ("MHG", "COMEX"),
    "NG": ("MNG", "NYMEX"),
    "MNG": ("MNG", "NYMEX"),

    # 債券與利率
    "VU": ("MTN", "CBOT"),
    "MTN": ("MTN", "CBOT"),
    "TN": ("TN", "CBOT"),

    # 農產品
    "YC": ("YC", "CBOT"),
    "XC": ("YC", "CBOT"),
    "ZC": ("ZC", "CBOT"),
    "ZW": ("ZW", "CBOT"),
    "ZS": ("ZS", "CBOT"),

    # 波動率
    "VXM": ("VXM", "CFE"),
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
        確保有可用的 IBKR 連線 (具備完整即時倉位同步能力)
        """
        if self.ib is None or not self.ib.isConnected():
            import random
            host = self.cfg.get("IB_HOST", "127.0.0.1")
            port = int(self.cfg.get("IB_PORT", 4001))
            client_id = random.randint(7100, 7900)
            print(f"[INFO] [PositionSkill] 正在建立具備即時倉位同步之 IBKR 連線 ({host}:{port}, ClientId: {client_id}) ...")
            ib = IB()
            ib.connect(host, port, clientId=client_id, timeout=12)
            self.ib = ib
            self.owns_ib = True
            print(f"[SUCCESS] ✅ 成功建立 IBKR 連線與部位同步！")
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

        # 0. 特殊處理 CFE 交易所之期貨合約 (如 PET, PBT)
        if sym in ("PET", "PBT") or sym.startswith("PET") or sym.startswith("PBT"):
            cfe_root = "PET" if sym.startswith("PET") else "PBT"
            print(f"[INFO] [PositionSkill] 識別為 CFE 加密指數期貨: {sym} (根合約: {cfe_root}, 交易所: CFE)")
            c_cfe = Contract(symbol=cfe_root, secType="FUT", exchange="CFE", currency="USD")
            qualified = ib.qualifyContracts(c_cfe)
            if qualified and c_cfe.conId:
                print(f"[SUCCESS] ✅ CFE 期貨合約驗證成功: {c_cfe.localSymbol} (conId: {c_cfe.conId})")
                return c_cfe

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

        # 1.5 嘗試直接作為期貨 Root 代號 (如 MGC, GC, MES 等) 尋找主力期貨合約
        if sym in BARCHART_TO_IBKR_FUTURES:
            try:
                from trade.q import get_target_future_contract as _gtfc
                c_fut_auto = _gtfc(ib, sym)
                if c_fut_auto and c_fut_auto.conId:
                    print(f"[SUCCESS] ✅ 期貨 Root 代碼自動鎖定主力期貨合約: {c_fut_auto.localSymbol} (conId: {c_fut_auto.conId})")
                    return c_fut_auto
            except Exception:
                pass

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

    def adjust_position(self, symbol: str, signal: str, dry_run: bool = False, send_line: bool = True) -> dict:
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

        # 5. 預查現價與最小跳動點 (minTick)
        min_tick = 0.01
        try:
            cds = ib.reqContractDetails(contract)
            if cds and cds[0].minTick and cds[0].minTick > 0:
                min_tick = cds[0].minTick
        except Exception as e:
            print(f"[WARN] 取得合約 minTick 失敗，使用預設值 0.01: {e}")

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

        # 6. 自適應步進修單 (Custom Walk-Up)
        step_val = max(0.05, min_tick) if min_tick else 0.05
        max_slip = max(0.15, step_val * 3)
        price_desc = f"Custom Walk-Up (起始限價: ${market_price:.2f})"
        order_desc = f"{order_action} {order_qty} 口 {contract.localSymbol} @ {price_desc}"

        # 7. 執行下單
        order_status = "DryRun"
        if not dry_run:
            if execute_walk_up_order:
                filled, trade, avg_price = execute_walk_up_order(
                    ib=ib,
                    contract=contract,
                    action=order_action,
                    quantity=order_qty,
                    current_mid=market_price or 0.05,
                    max_slippage=max_slip,
                    step=step_val,
                    step_time=3.0,
                    max_steps=3,
                    symbol=f"{symbol}-Opinion",
                    account=target_account,
                    tif="DAY",
                    outside_rth=False,
                    min_tick=min_tick
                )
                order_status = "Filled" if filled else (trade.orderStatus.status if trade else "Cancelled")
                if filled:
                    order_desc = f"{order_action} {order_qty} 口 {contract.localSymbol} @ Walk-Up 成交均價 ${avg_price:.2f}"
            else:
                order = LimitOrder(order_action, order_qty, market_price or 0.05)
                order.tif = "DAY"
                if target_account:
                    order.account = target_account
                trade = ib.placeOrder(contract, order)
                ib.sleep(1)
                order_status = trade.orderStatus.status if hasattr(trade, "orderStatus") else "Submitted"
            print(f"[SUCCESS] ✅ [已執行自適應步進修單] 委託狀態: {order_status} | {order_desc}")
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

        if send_push_message and send_line:
            print(f"[INFO] [PositionSkill] 正在發送 LINE 調倉通知...")
            try:
                ok = send_push_message(line_msg)
                if ok:
                    print("[SUCCESS] ✅ LINE 推播發送成功！")
            except Exception as le:
                print(f"[WARN] LINE 推播異常: {le}")
        elif not send_line:
            print(f"[INFO] [PositionSkill] --no-line 已啟用，略過 LINE 推播發送。")

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
