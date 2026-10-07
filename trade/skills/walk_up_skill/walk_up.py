#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Walk-Up Order Skill (trade/skills/walk_up_skill/walk_up.py)
================================================================================
職責：
  自適應步進修單 (Custom Walk-Up / Order Modify & Price Concession Engine)：
  1. 取代高滑點的 Market 單與無效率等待之固定 Limit 單。
  2. 依據買賣方向動態步進修單 (預設每 3 秒讓步 $0.05，最多讓步 max_slippage，預設 $0.15)。
     - 賣出單 (SELL): 價格由高往低逐級向下讓步 ($base_price -> -0.05 -> -0.10 -> -0.15)。
     - 買入單 (BUY) : 價格由低往高逐級向上加價 ($base_price -> +0.05 -> +0.10 -> +0.15)。
  3. 沿用同一 Order 物件與 orderId 重新送出，IBKR 原生識別為 Modify (Order Replace)，不佔用多餘額度與通道。
  4. 逾時 (如 3 次讓步共 9 秒) 仍未成交，徹底撤銷發呆訂單，防止死水期被動接刀。
  5. 支援個股、期貨、單腿期權與 BAG 垂直價差/蝶式組合單。
================================================================================
"""

import time
import math
from typing import Optional, Tuple, Any

try:
    from ib_insync import IB, Contract, Order, LimitOrder, Trade
except ImportError:
    IB = None
    Contract = None
    Order = None
    LimitOrder = None
    Trade = None


def is_valid_price(p: Any) -> bool:
    """檢查價格是否為合法正數且非 NaN / Inf"""
    if p is None:
        return False
    try:
        f = float(p)
        return not (math.isnan(f) or math.isinf(f) or f <= 0)
    except (TypeError, ValueError):
        return False


def extract_valid_price(*candidates) -> Optional[float]:
    """按優先順序提取第一個有效的非 NaN/Inf 且 >0 的價格"""
    for c in candidates:
        if is_valid_price(c):
            return float(c)
    return None


def round_to_tick(price: float, min_tick: Optional[float] = None) -> float:
    """根據最小跳動點修整價格，若無 min_tick 預設保留 2 位小數 (或 4 位)"""
    if not is_valid_price(price):
        return 0.0
    p = float(price)
    if min_tick and min_tick > 0:
        return round(round(p / min_tick) * min_tick, 6)
    return round(p, 2)


def fmt_price(price: Any, min_tick: Optional[float] = None) -> str:
    """根據 min_tick 動態格式化價格字串，避免小數過長或截斷"""
    if not is_valid_price(price):
        return "nan"
    p = float(price)
    if min_tick and min_tick > 0:
        if min_tick < 0.00005:
            return f"{p:.6f}"
        elif min_tick < 0.0005:
            return f"{p:.4f}"
        elif min_tick < 0.005:
            return f"{p:.3f}"
        elif min_tick < 0.05:
            return f"{p:.2f}"
    return f"{p:.2f}"


def determine_min_tick_and_step(contract: Any, price: Optional[float] = None) -> Tuple[float, float, float]:
    """
    計算合約的最小跳動點 (min_tick)、讓步步階 (step_val) 與最大容許滑價 (max_slip)
    - 國外期貨 (FUT / CONTFUT):
        • 天然氣 (MNG, MHNG, NG, LN): min_tick = 0.001, step = 0.001, max_slip = 0.015
        • 原油 (MCL, CL, LO): min_tick = 0.01, step = 0.01, max_slip = 0.15
        • 銅 (MHG, HG, HXE): min_tick = 0.0005, step = 0.0005, max_slip = 0.010
        • 黃金 (MGC, GC, OG): min_tick = 0.10, step = 0.10, max_slip = 1.50
        • 白銀 (MSI, SI, SIL): min_tick = 0.005, step = 0.005, max_slip = 0.05
        • 玉米 (XC, YC, ZC, OZC): min_tick = 0.125 (XC/YC) 或 0.25 (ZC), step = min_tick, max_slip = 1.0
        • 微型歐元 (M6E, 6E, EUR, EUU): min_tick = 0.0001, step = 0.0001, max_slip = 0.0010
        • 微型日圓 (MJY, 6J, JPY, JPU): min_tick = 0.000001, step = 0.000001, max_slip = 0.000010
        • 羅素 (M2K, RTY): min_tick = 0.10, step = 0.10, max_slip = 1.0
        • 道瓊 (MYM, YM): min_tick = 1.0, step = 1.0, max_slip = 5.0
        • VIX (VXM): min_tick = 0.05, step = 0.05, max_slip = 0.30
        • 股指期貨 (MES, ES, MNQ, NQ): min_tick = 0.25, step = 0.25, max_slip = 2.0
        • 台指 (TMF, TX, MTX, MXF): min_tick = 1.0, step = 1.0, max_slip = 5.0
    - 期貨期權 (FOP):
        • 標的為天然氣/原油/銅/黃金等，依大宗商品跳動點精確適配
        • 股指期權 (EW, ES, NQ): 價格 >= 5.0 時 0.25; < 5.0 時 0.05
    - 指數期權 (SPX, SPXW, NDX, RUT):
        • 價格 >= 3.0 時 CBOE 規定 min_tick = 0.10, step = 0.10, max_slip = 0.30
        • 價格 < 3.0 時 min_tick = 0.05, step = 0.05, max_slip = 0.15
    - 一般個股期權與個股 (STK, OPT): min_tick = 0.05, step = 0.05, max_slip = 0.15 (或 0.01)
    :return: (min_tick, step_val, max_slippage)
    """
    sec_type = getattr(contract, 'secType', '')
    symbol = (getattr(contract, 'symbol', '') or '').upper()
    loc_sym = (getattr(contract, 'localSymbol', '') or '').upper()
    try:
        price_val = float(price) if (price is not None and not math.isnan(float(price))) else 0.0
    except (TypeError, ValueError):
        price_val = 0.0

    if sec_type in ('FUT', 'CONTFUT'):
        if symbol in ('MNG', 'MHNG', 'NG', 'LN') or any(loc_sym.startswith(x) for x in ('MNG', 'NG', 'LN')):
            return 0.001, 0.001, 0.015
        elif symbol in ('MCL', 'CL', 'LO') or any(loc_sym.startswith(x) for x in ('MCL', 'CL')):
            return 0.01, 0.01, 0.15
        elif symbol in ('MHG', 'HG', 'HXE') or any(loc_sym.startswith(x) for x in ('MHG', 'HG')):
            return 0.0005, 0.0005, 0.010
        elif symbol in ('MGC', 'GC', 'OG') or any(loc_sym.startswith(x) for x in ('MGC', 'GC')):
            return 0.10, 0.10, 1.50
        elif symbol in ('MSI', 'SI', 'SIL') or any(loc_sym.startswith(x) for x in ('MSI', 'SI')):
            return 0.005, 0.005, 0.05
        elif symbol in ('XC', 'YC') or any(loc_sym.startswith(x) for x in ('XC', 'YC')):
            return 0.125, 0.125, 1.0
        elif symbol in ('ZC', 'OZC') or any(loc_sym.startswith(x) for x in ('ZC', 'OZC')):
            return 0.25, 0.25, 1.5
        elif symbol in ('M6E', '6E', 'EUR', 'EUU') or any(loc_sym.startswith(x) for x in ('M6E', '6E')):
            return 0.0001, 0.0001, 0.0010
        elif symbol in ('MJY', '6J', 'JPY', 'JPU') or any(loc_sym.startswith(x) for x in ('MJY', '6J')):
            return 0.000001, 0.000001, 0.000010
        elif symbol in ('M2K', 'RTY') or any(loc_sym.startswith(x) for x in ('M2K', 'RTY')):
            return 0.10, 0.10, 1.0
        elif symbol in ('MYM', 'YM') or any(loc_sym.startswith(x) for x in ('MYM', 'YM')):
            return 1.0, 1.0, 5.0
        elif symbol in ('VXM',):
            return 0.05, 0.05, 0.30
        elif symbol in ('TMF', 'TX', 'MTX', 'MXF') or any(loc_sym.startswith(x) for x in ('TMF', 'TX', 'MTX', 'MXF')):
            return 1.0, 1.0, 5.0
        elif symbol in ('MES', 'ES', 'MNQ', 'NQ') or any(loc_sym.startswith(x) for x in ('MES', 'ES', 'MNQ', 'NQ')):
            return 0.25, 0.25, 2.0
        return 0.25, 0.25, 1.5

    elif sec_type == 'FOP':
        if symbol in ('MNG', 'MHNG', 'NG', 'LN') or any(loc_sym.startswith(x) for x in ('MNG', 'NG', 'LN')):
            return 0.001, 0.001, 0.015
        elif symbol in ('MCL', 'CL', 'LO') or any(loc_sym.startswith(x) for x in ('MCL', 'CL')):
            return 0.01, 0.01, 0.15
        elif symbol in ('MHG', 'HG', 'HXE') or any(loc_sym.startswith(x) for x in ('MHG', 'HG')):
            return 0.0005, 0.0005, 0.010
        elif symbol in ('MGC', 'GC', 'OG') or any(loc_sym.startswith(x) for x in ('MGC', 'GC')):
            return 0.10, 0.10, 1.50
        elif symbol in ('XC', 'YC') or any(loc_sym.startswith(x) for x in ('XC', 'YC')):
            return 0.125, 0.125, 1.0
        elif symbol in ('ZC', 'OZC') or any(loc_sym.startswith(x) for x in ('ZC', 'OZC')):
            return 0.25, 0.25, 1.5
        if price_val >= 5.0:
            return 0.25, 0.25, 1.5
        else:
            return 0.05, 0.05, 0.15

    elif sec_type in ('OPT', 'WAR'):
        if symbol in ('SPX', 'SPXW', 'NDX', 'RUT'):
            tick = 0.10 if price_val >= 3.0 else 0.05
            return tick, tick, 0.30
        return 0.05, 0.05, 0.15

    return 0.01, 0.01, 0.10


def walk_up_limit_price(
    ib: Any,
    contract: Any,
    order: Any,
    current_mid: float,
    max_slippage: float = 0.15,
    step: float = 0.05,
    step_time: float = 3.0,
    max_steps: int = 3,
    symbol: Optional[str] = None,
    trade: Optional[Any] = None,
    min_tick: Optional[float] = None
) -> bool:
    """
    步進式動態修改限價 (每 step_time 秒讓步 step，最多讓步 max_slippage)
    :param ib: IB 連線實例
    :param contract: Contract 物件 (含 BAG 組合單、期貨、期權、股票)
    :param order: LimitOrder 物件 (維持同一 orderId)
    :param current_mid: 起始基準價格 (Mid / Bid / Limit)
    :param max_slippage: 最大容許滑價 (預設 0.15)
    :param step: 每次讓步價差 (預設 0.05)
    :param step_time: 每次讓步等待秒數 (預設 3.0 秒)
    :param max_steps: 最大讓步次數 (預設 3 次，共 9 秒)
    :param symbol: 標的代號 (供日誌顯示)
    :param trade: 選填，已存在的 Trade 物件
    :param min_tick: 最小跳動點 (若有)
    :return: 成交回傳 True，逾時撤單回傳 False
    """
    sym_name = symbol or getattr(contract, 'symbol', '') or getattr(contract, 'localSymbol', 'COMBO')

    # 檢查起始價格有效性 (嚴格防禦 NaN / Inf / <=0)
    base_price = None
    if is_valid_price(current_mid):
        base_price = float(current_mid)
    elif hasattr(order, 'lmtPrice') and is_valid_price(order.lmtPrice):
        base_price = float(order.lmtPrice)

    if base_price is None or not is_valid_price(base_price):
        print(f"[{sym_name}] ❌ 自適應步進修單中止：無有效基準價格 (current_mid={current_mid}, order.lmtPrice={getattr(order, 'lmtPrice', None)})，拒絕下單！")
        return False

    base_price = round(base_price, 4)
    order_action = getattr(order, 'action', 'SELL').upper()
    is_sell = (order_action == 'SELL')

    # 若合約有 minTick 且大於 step，調整 step 為 min_tick 整數倍
    if min_tick and min_tick > step:
        step = min_tick

    current_trade = trade
    if current_trade is None and ib is not None:
        target_oid = getattr(order, 'orderId', None)
        if target_oid:
            for t in ib.trades():
                if getattr(getattr(t, 'order', None), 'orderId', None) == target_oid:
                    current_trade = t
                    break

    for i in range(max_steps):
        # 計算讓步後的目標限價
        if is_sell:
            # 賣單：逐級向下減價以促成成交
            target_price = round_to_tick(base_price - (i * step), min_tick)
            slippage = round(base_price - target_price, 4)
        else:
            # 買單：逐級向上加價以促成成交
            target_price = round_to_tick(base_price + (i * step), min_tick)
            slippage = round(target_price - base_price, 4)

        # 若讓步超過最大容忍滑價，則停止追價
        if slippage > max_slippage + 1e-5:
            print(f"[{sym_name}] ⚠️ 第 {i+1} 次讓步已達最大滑價上限 (${fmt_price(slippage, min_tick)} > ${fmt_price(max_slippage, min_tick)})，停止追價。")
            break

        # 確保價格大於零
        target_price = max(min_tick or 0.01, target_price)

        # 更新訂單限價
        order.lmtPrice = target_price
        order.orderType = 'LMT'

        # 關鍵：沿用同一物件與 orderId 重新送出，IBKR 自動識別為 Modify (Order Replace)
        if i == 0 and current_trade is not None:
            # 首輪若已由外部送出且限價相符，直接進入等待
            if getattr(current_trade.order, 'lmtPrice', None) == target_price:
                print(f"[{sym_name}] [自適應步進首單] 限價鎖定為 ${fmt_price(target_price, min_tick)}，等待撮合...")
            else:
                print(f"[{sym_name}] [訂單調整] 第 1 次下單/修單: 限價更新為 ${fmt_price(target_price, min_tick)}")
                new_trade = ib.placeOrder(contract, order)
                if new_trade is not None:
                    current_trade = new_trade
        else:
            # 檢查當前訂單狀態，嚴格防禦 Duplicate order id
            c_status = getattr(getattr(current_trade, 'orderStatus', None), 'status', '')
            if c_status in ('Filled',):
                return True
            if c_status in ('Cancelled', 'Inactive', 'ApiCancelled'):
                print(f"[{sym_name}] ⚠️ 訂單已處於 {c_status} 狀態，終止步進修單以防 Duplicate order id。")
                return False
            if c_status in ('PendingSubmit', ''):
                ib.sleep(0.8)
                c_status = getattr(getattr(current_trade, 'orderStatus', None), 'status', '')
                if c_status in ('PendingSubmit', ''):
                    print(f"[{sym_name}] ⏳ 訂單仍為 PendingSubmit，暫緩修改以防 Duplicate order id。")
                    continue

            print(f"[{sym_name}] [訂單調整] 第 {i+1} 次修單: 將限價更新為 ${fmt_price(target_price, min_tick)}")
            new_trade = ib.placeOrder(contract, order)
            if new_trade is not None:
                current_trade = new_trade

        # 等待 step_time 秒檢查是否成交 (高頻輪詢狀態)
        start_wait = time.time()
        while time.time() - start_wait < step_time:
            ib.sleep(0.5)

            # 確保取得最新 Trade 物件 (ib_insync 中訂單狀態由 Trade 維護，Order 物件無 orderStatus)
            if (current_trade is None or not hasattr(current_trade, 'orderStatus')) and ib is not None:
                target_oid = getattr(order, 'orderId', None)
                if target_oid:
                    for t in ib.trades():
                        if getattr(getattr(t, 'order', None), 'orderId', None) == target_oid:
                            current_trade = t
                            break

            status = ''
            avg_p = 0.0
            if current_trade and hasattr(current_trade, 'orderStatus') and current_trade.orderStatus:
                status = str(getattr(current_trade.orderStatus, 'status', '') or '')
                avg_p = float(getattr(current_trade.orderStatus, 'avgFillPrice', 0.0) or 0.0)

            if status == 'Filled':
                avg_p = avg_p or target_price
                print(f"✅ [{sym_name}] 成功撮合成交！成交價: ${fmt_price(avg_p, min_tick)}")
                return True
            elif status in ('Cancelled', 'Inactive', 'ApiCancelled'):
                print(f"⚠️ [{sym_name}] 訂單已處於 {status} 狀態，終止步進修單。")
                return False

    # 若 max_steps 次讓步 (共 max_steps * step_time 秒) 仍未成交，此時徹底撤單
    print(f"⚠️ [{sym_name}] 逾時未成交 ({max_steps}次讓步共 {max_steps*step_time:.0f}秒)，徹底撤銷訂單以防死水期被動接刀。")
    try:
        ib.cancelOrder(order)
        ib.sleep(1.0)
    except Exception as e:
        print(f"[{sym_name}] 撤單請求發送異常: {e}")

    return False


def execute_walk_up_order(
    ib: Any,
    contract: Any,
    action: str,
    quantity: float,
    current_mid: float,
    max_slippage: float = 0.15,
    step: float = 0.05,
    step_time: float = 3.0,
    max_steps: int = 3,
    symbol: Optional[str] = None,
    account: Optional[str] = None,
    tif: str = 'DAY',
    outside_rth: bool = False,
    min_tick: Optional[float] = None
) -> Tuple[bool, Optional[Any], float]:
    """
    建立並執行全新的自適應步進限價訂單 (整合建立、送出與修單撤單全套流水線)
    :return: (is_filled, trade_object, avg_fill_price)
    """
    sym_name = symbol or getattr(contract, 'symbol', '') or getattr(contract, 'localSymbol', 'CONTRACT')
    action_upper = action.strip().upper()
    qty_val = float(quantity)

    # 驗證價格有效性 (嚴格防禦 NaN / Inf / <=0)
    if not is_valid_price(current_mid):
        print(f"\n❌ [{sym_name}] 自適應步進下單中止：無有效起始限價 (current_mid={current_mid})，拒絕向 IBKR 送出無效委託！")
        return False, None, 0.0

    # 初始價格
    base_price = round_to_tick(current_mid, min_tick)
    if not is_valid_price(base_price):
        print(f"\n❌ [{sym_name}] 自適應步進下單中止：修整後基準價格無效 (${fmt_price(base_price, min_tick)})，拒絕向 IBKR 送單！")
        return False, None, 0.0

    order = LimitOrder(action_upper, qty_val, base_price)
    order.tif = tif
    order.outsideRth = outside_rth
    if account:
        order.account = account

    print(f"\n⚡ [{sym_name}] 啟動自適應步進修單 (Custom Walk-Up):")
    print(f"   • 動作: {action_upper} {qty_val:g}口 @ 起始限價 ${fmt_price(base_price, min_tick)}")
    print(f"   • 步進參數: 每 {step_time} 秒讓步 ${fmt_price(step, min_tick)} (最大讓步 ${fmt_price(max_slippage, min_tick)}, 最多 {max_steps} 次)")

    trade = ib.placeOrder(contract, order)
    ib.sleep(0.5)

    filled = walk_up_limit_price(
        ib=ib,
        contract=contract,
        order=order,
        current_mid=base_price,
        max_slippage=max_slippage,
        step=step,
        step_time=step_time,
        max_steps=max_steps,
        symbol=sym_name,
        trade=trade,
        min_tick=min_tick
    )

    # 確保取得最新的 Trade 物件 (包含所有後續修改與錯誤日誌)
    target_oid = getattr(order, 'orderId', None)
    if target_oid and ib is not None:
        for t in ib.trades():
            if getattr(getattr(t, 'order', None), 'orderId', None) == target_oid:
                trade = t
                break

    avg_price = 0.0
    if filled:
        avg_price = getattr(getattr(trade, 'orderStatus', None), 'avgFillPrice', 0.0) or order.lmtPrice or base_price
    return filled, trade, avg_price


class WalkUpOrderSkill:
    """
    自適應步進修單技能類別 (封裝 Walk-Up 機制供模組化調度)
    """
    def __init__(self, ib_instance=None):
        self.ib = ib_instance

    def walk_up(self, contract, order, current_mid, max_slippage=0.15, step=0.05, step_time=3.0, max_steps=3, symbol=None, trade=None, min_tick=None):
        return walk_up_limit_price(
            ib=self.ib,
            contract=contract,
            order=order,
            current_mid=current_mid,
            max_slippage=max_slippage,
            step=step,
            step_time=step_time,
            max_steps=max_steps,
            symbol=symbol,
            trade=trade,
            min_tick=min_tick
        )

    def execute(self, contract, action, quantity, current_mid, max_slippage=0.15, step=0.05, step_time=3.0, max_steps=3, symbol=None, account=None, tif='DAY', outside_rth=False, min_tick=None):
        return execute_walk_up_order(
            ib=self.ib,
            contract=contract,
            action=action,
            quantity=quantity,
            current_mid=current_mid,
            max_slippage=max_slippage,
            step=step,
            step_time=step_time,
            max_steps=max_steps,
            symbol=symbol,
            account=account,
            tif=tif,
            outside_rth=outside_rth,
            min_tick=min_tick
        )
