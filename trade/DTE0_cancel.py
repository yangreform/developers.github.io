#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DTE0 Emergency Cancel & Walk-Up Close Module (trade/DTE0_cancel.py)
================================================================================
專門針對 0DTE 指數期權與 MES 對沖部位進行即時緊急撤單與平倉：
  1. 連線至 IBKR TWS / Gateway。
  2. 掃描所有由 DTE0_SELL.py / DTE0.py 屬於 SPX / MES 之指數期權與期貨委託：
     - 未觸發成交的有效掛單 (Submitted / PreSubmitted) -> 立即 Cancel 撤單！
  3. 掃描目前持倉中所有屬於 0DTE 指數期權之已成交部位：
     - 立即啟動 Custom Walk-Up 自適應步進修單進行平倉！
       • 多頭部位 -> 步進限價賣出 (Walk-Up SELL)
       • 空頭部位 -> 步進限價買回 (Walk-Up BUY)
  4. 掃描目前持倉中所有 MES 已成交部位：
     - 立即啟動 Custom Walk-Up 自適應步進修單進行平倉！
       • 多頭部位 -> 步進限價賣出 (Walk-Up SELL)
       • 空頭部位 -> 步進限價買回 (Walk-Up BUY)
  5. 整合 LINE 即時推播，彙總回報撤單與平倉執行結果。

指令範例:
  python trade/DTE0_cancel.py              # 正式執行全套 (撤單 + 0DTE期權步進平倉 + MES步進平倉 + 發 LINE)
  python trade/DTE0_cancel.py --dry-run    # 模擬模式 (僅查詢掃描盤口與部位，不發送真實交易)
  python trade/DTE0_cancel.py --cancel-only# 僅撤單，不平倉
  python trade/DTE0_cancel.py --close-only # 僅平倉 (期權+MES)，不撤單
  python trade/DTE0_cancel.py --opt-only   # 僅平倉 0DTE 指數期權部位
  python trade/DTE0_cancel.py --mes-only   # 僅平倉 MES 期貨部位
  python trade/DTE0_cancel.py --no-line    # 略過 LINE 推播
  python trade/DTE0_cancel.py --market     # 緊急後備：改用市價單 (Market Order) 平倉
================================================================================
"""

import os
import sys
import json
import time
import math
import datetime
import argparse

try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo

from ib_insync import IB, Contract, Option, Future, Index, MarketOrder, LimitOrder, Trade

# 確保 Windows 主控台與子行程正確輸出 UTF-8 字符，避免 UnicodeEncodeError
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
        sys.stderr.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
    except Exception:
        pass

# 嘗試載入 LINE 推播模組
try:
    from notifier import send_push_message, send_trade_notification
except ImportError:
    try:
        from trade.notifier import send_push_message, send_trade_notification
    except ImportError:
        send_push_message = lambda *a, **kw: False
        send_trade_notification = lambda *a, **kw: False

# 載入自適應步進修單模組 (Walk-Up Skill)
try:
    from skills.walk_up_skill import (
        walk_up_limit_price,
        execute_walk_up_order,
        round_to_tick,
        is_valid_price,
        extract_valid_price,
        determine_min_tick_and_step,
        fmt_price,
    )
except ImportError:
    try:
        from trade.skills.walk_up_skill import (
            walk_up_limit_price,
            execute_walk_up_order,
            round_to_tick,
            is_valid_price,
            extract_valid_price,
            determine_min_tick_and_step,
            fmt_price,
        )
    except ImportError:
        walk_up_limit_price = None
        execute_walk_up_order = None
        round_to_tick = lambda p, t=None: round(p, 2)
        is_valid_price = lambda p: p is not None and not math.isnan(float(p)) and float(p) > 0
        extract_valid_price = lambda *args: next(
            (float(x) for x in args if x is not None and not math.isnan(float(x)) and float(x) > 0), None
        )
        determine_min_tick_and_step = lambda c, p=None: (
            (0.25, 0.25, 2.0) if getattr(c, 'secType', '') in ('FUT', 'CONTFUT') else (0.05, 0.05, 0.15)
        )
        fmt_price = lambda p, t=None: f"{float(p):.2f}"

try:
    from dte0_config import clear_dte0_wings, load_dte0_config
except ImportError:
    try:
        from trade.dte0_config import clear_dte0_wings, load_dte0_config
    except ImportError:
        clear_dte0_wings = None
        load_dte0_config = None

# ==============================================================================
# 0. 讀取 .env 設定
# ==============================================================================
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')


def load_env_config():
    """即時自 trade/.env 讀取最新環境變數設定。"""
    cfg = {}
    if os.path.exists(ENV_PATH):
        try:
            with open(ENV_PATH, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '=' in line:
                        k, v = line.split('=', 1)
                        cfg[k.strip()] = v.strip().strip("'").strip('"')
        except Exception as e:
            print(f"[警告] 讀取 .env 失敗: {e}")
    return cfg


def get_market_today():
    """
    獲取美東時間 (US Eastern Date)，作為美股/CBOE 指數期權 0DTE 的計算基準。
    """
    try:
        us_eastern = zoneinfo.ZoneInfo("America/New_York")
        return datetime.datetime.now(us_eastern).date()
    except Exception:
        return (datetime.datetime.utcnow() - datetime.timedelta(hours=4)).date()


# ==============================================================================
# 1. 交易參數與標的配置
# ==============================================================================
env_config = load_env_config()
IB_HOST = env_config.get('IB_HOST', '127.0.0.1')
IB_PORT = int(env_config.get('IB_PORT', 4001))
BASE_CLIENT_ID = int(env_config.get('IB_CLIENT_ID', 1001))
DTE0_CLIENT_ID = BASE_CLIENT_ID + 7
DTE0_SELL_CLIENT_ID = int(env_config.get('IB_CLIENT_ID', 100)) + 9
TARGET_ACCOUNT = env_config.get('IB_TARGET_ACCOUNT', '').strip()

# 自 DTE0_CONFIG_JSON 讀取 SPX 設定
dte0_cfg = load_dte0_config() if load_dte0_config else {}
spx_dte0 = dte0_cfg.get("SPX") or dte0_cfg.get("標普指數(SPX)") or {}
spx_hedge_sym = str(spx_dte0.get("hedge_sym", "MES")).upper()

INDEX_SYMBOLS = {'SPX', 'NDX', 'RUT', 'DJX'}
INDEX_SYMBOL_MAP = {
    'SPX': 'SPX', 'SPXW': 'SPX',
    'NDX': 'NDX', 'NDXP': 'NDX',
    'RUT': 'RUT', 'RUTW': 'RUT',
    'DJX': 'DJX', 'DJXW': 'DJX',
}
PREFERRED_TRADING_CLASS_MAP = {
    'SPX': 'SPXW',
    'NDX': 'NDXP',
    'RUT': 'RUTW',
    'DJX': 'DJXW',
}
ALL_INDEX_CLASSES = {'SPX', 'SPXW', 'NDX', 'NDXP', 'RUT', 'RUTW', 'DJX', 'DJXW'}
MES_SYMBOLS = {spx_hedge_sym, 'MES', 'ES'}

ib = IB()
RECENT_IB_ERRORS = []


def on_ib_error(reqId, errorCode, errorString, contract):
    RECENT_IB_ERRORS.append((reqId, errorCode, errorString, contract))
    if errorCode in (110, 201, 103, 321, 200, 399, 10349):
        print(f"[IB 警示/錯誤] ReqId: {reqId} | 代碼: {errorCode} | 訊息: {errorString}")


ib.errorEvent += on_ib_error


def connect_ib():
    """建立與 IBKR 的連線，避免與正在運行的 DTE0_SELL / q.py 衝突。"""
    if not ib.isConnected():
        connected = False
        # 優先嘗試 Master ClientId 0 (具備取消全帳號所有 Client 委託之最高權限)
        # 若 0 被佔用，則依序嘗試備援 ID
        cids_to_try = [
            0,
            BASE_CLIENT_ID + 77,
            BASE_CLIENT_ID + 88,
            BASE_CLIENT_ID + 7,
            DTE0_SELL_CLIENT_ID,
            BASE_CLIENT_ID + 99,
        ] + [BASE_CLIENT_ID + 80 + i for i in range(5)]

        for cid in cids_to_try:
            try:
                ib.connect(IB_HOST, IB_PORT, clientId=cid, timeout=5)
                if ib.isConnected():
                    connected = True
                    break
            except Exception:
                try:
                    ib.disconnect()
                except Exception:
                    pass
                time.sleep(0.3)

        if not connected:
            raise ConnectionError(f"無法連接至 IBKR ({IB_HOST}:{IB_PORT})，已嘗試 ClientId: {cids_to_try[:5]}...")

        try:
            ib.reqMarketDataType(3)  # 即時延遲/凍結行情切換，確保休市或非訂閱亦能讀取盤口
        except Exception:
            pass

        actual_id = ib.client.clientId if ib.client else 0
        print(f"=== [DTE0 Cancel] 成功連接至 IBKR ({IB_HOST}:{IB_PORT}) | ClientId={actual_id} ===")


# ==============================================================================
# 2. 判斷合約與委託所屬 (SPX / MES / DTE0_SELL.py)
# ==============================================================================
def is_target_trade_or_order(trade, market_date=None, allow_any_expiry=False):
    """
    精確檢查委託是否屬於：
      1. 由 DTE0_SELL.py 或 DTE0.py 送出之委託
      2. 標的屬於 SPX / SPXW (或 NDX/RUT/DJX) 之指數期權 (OPT/BAG)
      3. 標的屬於 MES / ES 之微型標普期貨 / 期權 (FUT/FOP)
    """
    if not trade:
        return False
    c = trade.contract
    o = trade.order
    if not c or not o:
        return False

    sec_type = (getattr(c, 'secType', '') or '').upper()
    symbol = (getattr(c, 'symbol', '') or '').upper()
    trading_class = (getattr(c, 'tradingClass', '') or '').upper()
    local_symbol = (getattr(c, 'localSymbol', '') or '').upper()
    order_client_id = getattr(o, 'clientId', None)

    # 1. 檢查是否為 DTE0_SELL.py 或 DTE0.py 下單 ClientId
    if order_client_id is not None:
        if DTE0_CLIENT_ID <= order_client_id <= (DTE0_CLIENT_ID + 15):
            return True
        if DTE0_SELL_CLIENT_ID <= order_client_id <= (DTE0_SELL_CLIENT_ID + 10):
            return True

    # 2. 檢查是否為 SPX / 指數期權 (BAG 組合單或 OPT 期權)
    if sec_type in ('BAG', 'OPT'):
        if symbol in INDEX_SYMBOLS or symbol in INDEX_SYMBOL_MAP:
            return True
        if trading_class in ALL_INDEX_CLASSES:
            return True
        if any(local_symbol.startswith(cls) for cls in ALL_INDEX_CLASSES):
            return True

    # 3. 檢查是否為 MES / ES 期貨或期權 (FUT, CONTFUT, FOP)
    if sec_type in ('FUT', 'CONTFUT', 'FOP'):
        if symbol in ('MES', 'ES'):
            return True
        if trading_class in ('MES', 'EW', 'ES'):
            return True
        if local_symbol.startswith('MES') or local_symbol.startswith('EW'):
            return True

    return False


def is_0dte_option_contract(contract, market_date=None, allow_any_expiry=False):
    """
    精確檢查合約是否屬於 0DTE 指數期權：
      - secType 為 OPT (或 BAG)
      - symbol / tradingClass 為 SPX / NDX / RUT / DJX
      - 若未指定 allow_any_expiry，檢查到期日是否為 0DTE (到期日 <= market_date 或相距 <= 1 天)
    """
    if not contract:
        return False

    sec_type = (getattr(contract, 'secType', '') or '').upper()
    symbol = (getattr(contract, 'symbol', '') or '').upper()
    trading_class = (getattr(contract, 'tradingClass', '') or '').upper()
    local_symbol = (getattr(contract, 'localSymbol', '') or '').upper()

    if sec_type not in ('OPT', 'BAG'):
        return False

    is_index = False
    if symbol in INDEX_SYMBOLS or symbol in INDEX_SYMBOL_MAP:
        is_index = True
    elif trading_class in ALL_INDEX_CLASSES:
        is_index = True
    elif any(local_symbol.startswith(cls) for cls in ALL_INDEX_CLASSES):
        is_index = True

    if not is_index:
        return False

    if allow_any_expiry:
        return True

    if market_date is None:
        market_date = get_market_today()

    expiry_str = getattr(contract, 'lastTradeDateOrContractMonth', '') or ''
    if expiry_str:
        try:
            exp_date = datetime.datetime.strptime(expiry_str[:8], '%Y%m%d').date()
            dte = (exp_date - market_date).days
            if dte <= 1:
                return True
            else:
                return False
        except Exception:
            pass

    return True


def is_mes_contract(contract):
    """檢查合約是否屬於 MES 微型標普 500 期貨部位。"""
    if not contract:
        return False
    sec_type = (getattr(contract, 'secType', '') or '').upper()
    symbol = (getattr(contract, 'symbol', '') or '').upper()
    local_symbol = (getattr(contract, 'localSymbol', '') or '').upper()

    if sec_type in ('FUT', 'CONTFUT'):
        if symbol == 'MES' or local_symbol.startswith('MES'):
            return True
    return False


# ==============================================================================
# 3. 撤銷所有未觸發成交的掛單 (Cancel Working Orders: SPX & MES)
# ==============================================================================
def cancel_all_dte0_and_mes_orders(dry_run=False, allow_any_expiry=False):
    """
    掃描 IBKR 所有有效掛單 (open orders)，找出所有 DTE0_SELL.py 屬於 SPX / MES 之掛單並立即取消。
    回傳撤單結果列表。
    """
    print("\n" + "=" * 70)
    print("🚀 【步驟 1】掃描並撤銷 DTE0_SELL.py (SPX / MES) 未觸發成交之有效掛單 (Cancel Orders)")
    print("=" * 70)

    try:
        all_trades = ib.reqAllOpenOrders()
    except Exception as e:
        print(f"⚠️ [查詢掛單異常] reqAllOpenOrders 失敗: {e}，改用 openTrades()")
        all_trades = ib.openTrades()

    market_date = get_market_today()
    target_trades = []

    for t in all_trades:
        status = t.orderStatus.status
        if status in ('Filled', 'Cancelled', 'Inactive', 'ApiCancelled'):
            continue

        if is_target_trade_or_order(t, market_date=market_date, allow_any_expiry=allow_any_expiry):
            target_trades.append(t)

    print(f"-> 檢測到全帳戶有效掛單: {len(all_trades)} 筆 | 符合 SPX / MES 策略之掛單: {len(target_trades)} 筆")

    cancel_results = []
    if not target_trades:
        print("-> ✅ 目前市場無任何 SPX / MES 未成交掛單，無需撤單。")
        return cancel_results

    for idx, t in enumerate(target_trades, 1):
        c = t.contract
        o = t.order
        s = t.orderStatus
        disp_sym = c.localSymbol if c.localSymbol else (f"{c.symbol} ({c.secType})")
        price_disp = f"${o.lmtPrice:.2f}" if getattr(o, 'lmtPrice', 0) and o.lmtPrice > 0 else "MKT"

        print(f"  [{idx}/{len(target_trades)}] 鎖定掛單: OrderId #{o.orderId} | {disp_sym} | {o.action} {o.totalQuantity}口 @ {price_disp} | 目前狀態: {s.status}")

        item_res = {
            'order_id': o.orderId,
            'symbol': c.symbol,
            'local_symbol': disp_sym,
            'sec_type': c.secType,
            'action': o.action,
            'quantity': o.totalQuantity,
            'limit_price': getattr(o, 'lmtPrice', 0.0),
            'orig_status': s.status,
            'new_status': 'DryRun' if dry_run else 'Cancelled',
            'success': True
        }

        if dry_run:
            print(f"     💡 [模擬模式] 略過發送撤單請求。")
        else:
            try:
                ib.cancelOrder(o)
                try:
                    ib.client.cancelOrder(o.orderId)
                except Exception:
                    pass
                print(f"     ⚡ [已發送撤單] 成功送出取消 OrderId #{o.orderId} 請求！")
            except Exception as ex:
                print(f"     ❌ [撤單失敗] 取消 OrderId #{o.orderId} 失敗: {ex}")
                item_res['success'] = False
                item_res['error'] = str(ex)

        cancel_results.append(item_res)

    if not dry_run and cancel_results:
        print("-> 正在等待 1.5 秒以同步 IBKR 撤單回報...")
        ib.sleep(1.5)
        for r in cancel_results:
            for t in ib.trades():
                if t.order.orderId == r['order_id']:
                    r['new_status'] = t.orderStatus.status
                    break

    return cancel_results


# ==============================================================================
# 通用單部位 Walk-Up 步進修單平倉函數
# ==============================================================================
def execute_position_close_walk_up(p, category_name="期權部位", dry_run=False, use_market=False, outside_rth=True):
    """
    通用平倉邏輯：
      1. 判斷多空 (pos > 0 -> SELL, pos < 0 -> BUY)
      2. 查詢該合約即時盤口 (Bid / Ask / Mid / Last / ModelPrice)
      3. 計算合約跳動點與步進參數 (determine_min_tick_and_step)
      4. 啟動 Custom Walk-Up 自適應步進修單平倉
    """
    c = p.contract
    pos_qty = float(p.position)
    abs_qty = abs(pos_qty)

    close_action = 'SELL' if pos_qty > 0 else 'BUY'
    pos_desc = f"多頭 +{pos_qty:g}口" if pos_qty > 0 else f"空頭 {pos_qty:g}口"
    disp_sym = c.localSymbol if c.localSymbol else (f"{c.symbol} {getattr(c, 'strike', '')}{getattr(c, 'right', '')}" if c.secType == 'OPT' else c.symbol)

    print(f"\n  🎯 鎖定{category_name}: {disp_sym} (conId: {c.conId}) | 現有持倉: {pos_desc}")
    print(f"     -> 平倉方向: {close_action} {abs_qty:g} 口")

    item_res = {
        'symbol': c.symbol,
        'local_symbol': disp_sym,
        'sec_type': c.secType,
        'con_id': c.conId,
        'strike': getattr(c, 'strike', 0.0),
        'right': getattr(c, 'right', ''),
        'expiry': getattr(c, 'lastTradeDateOrContractMonth', ''),
        'position': pos_qty,
        'close_action': close_action,
        'close_qty': abs_qty,
        'avg_cost': getattr(p, 'avgCost', 0.0),
        'order_id': None,
        'status': 'DryRun' if dry_run else 'Pending',
        'filled_price': 0.0,
        'success': True,
        'error': None,
    }

    # 1. 資格確認合約
    trade_contract = c
    try:
        qualified = ib.qualifyContracts(c)
        if qualified:
            trade_contract = qualified[0]
    except Exception as qe:
        print(f"     ⚠️ [資格確認異常] {disp_sym}: {qe}")

    # 2. 獲取即時行情 (Bid, Ask, Mid, Last)
    start_price = None
    ticker = None
    try:
        ticker = ib.reqMktData(trade_contract, '', False, False)
        ib.sleep(1.0)
        bid = ticker.bid if is_valid_price(ticker.bid) else None
        ask = ticker.ask if is_valid_price(ticker.ask) else None
        last = ticker.last if is_valid_price(ticker.last) else None
        close = ticker.close if is_valid_price(ticker.close) else None
        mg = getattr(ticker, 'modelGreeks', None)
        opt_price = getattr(mg, 'optPrice', None) if mg and is_valid_price(getattr(mg, 'optPrice', None)) else None

        # 優先計算盤口中價
        if is_valid_price(bid) and is_valid_price(ask):
            start_price = (bid + ask) / 2.0
        elif close_action == 'BUY' and is_valid_price(ask):
            start_price = ask
        elif close_action == 'BUY' and is_valid_price(bid):
            start_price = bid
        elif close_action == 'SELL' and is_valid_price(bid):
            start_price = bid
        elif close_action == 'SELL' and is_valid_price(ask):
            start_price = ask
        elif is_valid_price(last):
            start_price = last
        elif is_valid_price(opt_price):
            start_price = opt_price
        elif is_valid_price(close):
            start_price = close
    except Exception as me:
        print(f"     ⚠️ [行情獲取異常] {disp_sym}: {me}")
    finally:
        if ticker and trade_contract:
            try:
                ib.cancelMktData(trade_contract)
            except Exception:
                pass

    # 備援價格檢查
    if not is_valid_price(start_price):
        start_price = extract_valid_price(
            getattr(p, 'marketPrice', None),
            getattr(p, 'avgCost', None),
        )

    # 期貨若仍無價格，抓取 1 分鐘 K 線收盤
    if not is_valid_price(start_price) and trade_contract.secType in ('FUT', 'CONTFUT'):
        try:
            bars = ib.reqHistoricalData(trade_contract, '', '1 D', '1 min', 'TRADES', False, 1, False)
            if bars and len(bars) > 0 and is_valid_price(bars[-1].close):
                start_price = float(bars[-1].close)
        except Exception:
            pass

    # 期權若深度價外無報價，給予最小申報價格
    if not is_valid_price(start_price) and trade_contract.secType == 'OPT':
        start_price = 0.05

    min_tick, step_val, max_slip = determine_min_tick_and_step(trade_contract, start_price)
    start_price = round_to_tick(start_price, min_tick)
    if start_price <= 0:
        start_price = min_tick or 0.05

    item_res['filled_price'] = start_price

    if dry_run:
        print(f"     💡 [模擬模式] 試算步進修單規劃: 起始限價 ${fmt_price(start_price, min_tick)} | 每 3.0 秒讓步 ${fmt_price(step_val, min_tick)} | 最大讓步 ${fmt_price(max_slip, min_tick)} (最多 3 次) | 模擬略過送單。")
        return item_res

    # 3. 若使用者指定使用市價單兜底
    if use_market or not execute_walk_up_order:
        try:
            order = MarketOrder(close_action, abs_qty)
            order.tif = 'DAY'
            if TARGET_ACCOUNT:
                order.account = TARGET_ACCOUNT
            trade = ib.placeOrder(trade_contract, order)
            item_res['order_id'] = trade.order.orderId
            print(f"     ⚡ [市價平倉委託已送出] OrderId #{trade.order.orderId} | 狀態: {trade.orderStatus.status}")
            for _ in range(6):
                ib.sleep(1)
                st = trade.orderStatus.status
                if st in ('Filled', 'Cancelled', 'Inactive', 'Submitted', 'PreSubmitted'):
                    break
            final_status = trade.orderStatus.status
            item_res['status'] = final_status
            item_res['filled_price'] = trade.orderStatus.avgFillPrice or 0.0
            if final_status == 'Filled':
                print(f"     ✅ [平倉完全成交] 均價: ${trade.orderStatus.avgFillPrice:.2f}")
            else:
                print(f"     ℹ️ [平倉委託狀態] 目前狀態: {final_status}")
        except Exception as ex:
            print(f"     ❌ [市價單失敗] {ex}")
            item_res['success'] = False
            item_res['error'] = str(ex)
        return item_res

    # 4. 執行 Custom Walk-Up 步進修單
    min_tick, step_val, max_slip = determine_min_tick_and_step(trade_contract, start_price)
    start_price = round_to_tick(start_price, min_tick)
    if start_price <= 0:
        start_price = min_tick or 0.05

    print(f"     🚀 [啟動 Custom Walk-Up 步進平倉] 起始限價: ${fmt_price(start_price, min_tick)} (跳動點: {min_tick}, 步進: {step_val}, 最大讓步: {max_slip})")

    try:
        filled, trade, avg_price = execute_walk_up_order(
            ib=ib,
            contract=trade_contract,
            action=close_action,
            quantity=abs_qty,
            current_mid=start_price,
            max_slippage=max_slip,
            step=step_val,
            step_time=3.0,
            max_steps=3,
            symbol=f"{disp_sym}-Close",
            account=TARGET_ACCOUNT,
            tif='DAY',
            outside_rth=outside_rth,
            min_tick=min_tick
        )

        item_res['order_id'] = getattr(getattr(trade, 'order', None), 'orderId', None)
        item_res['filled_price'] = avg_price

        if filled:
            item_res['status'] = 'Filled'
            print(f"     ✅ [Walk-Up 平倉完全成交] 均價: ${fmt_price(avg_price, min_tick)}")
        else:
            final_st = getattr(getattr(trade, 'orderStatus', None), 'status', 'Cancelled')
            item_res['status'] = final_st
            item_res['success'] = False
            item_res['error'] = '步進修單逾時撤銷 (市場未撮合)'
            print(f"     ⚠️ [Walk-Up 平倉未成交] 狀態: {final_st} (已撤銷防止死水接刀)")

    except Exception as we:
        print(f"     ❌ [Walk-Up 執行異常] {we}")
        item_res['success'] = False
        item_res['error'] = str(we)

    return item_res


# ==============================================================================
# 4. 掃描持倉中所有 0DTE 指數期權部位並啟動 Custom Walk-Up 平倉
# ==============================================================================
def close_all_0dte_option_positions(dry_run=False, allow_any_expiry=False, use_market=False):
    """
    掃描 IBKR 帳戶持倉 (positions)，找出所有 0DTE 指數期權部位 (SPX/NDX/RUT/DJX) 並立即啟動 Custom Walk-Up 平倉。
    回傳平倉結果列表。
    """
    print("\n" + "=" * 70)
    print("🚀 【步驟 2】掃描目前持倉中 0DTE 指數期權已成交部位 -> 啟動 Custom Walk-Up 步進平倉")
    print("=" * 70)

    try:
        ib.reqPositions()
        ib.sleep(0.8)
    except Exception:
        pass

    positions = ib.positions()
    portfolio_items = ib.portfolio()
    market_date = get_market_today()

    target_positions = []
    seen_con_ids = set()

    for p in positions:
        if getattr(p, 'position', 0) == 0:
            continue
        c = p.contract
        if c.conId in seen_con_ids:
            continue
        if is_0dte_option_contract(c, market_date=market_date, allow_any_expiry=allow_any_expiry):
            target_positions.append(p)
            seen_con_ids.add(c.conId)

    for pf in portfolio_items:
        if getattr(pf, 'position', 0) == 0:
            continue
        c = pf.contract
        if c.conId in seen_con_ids:
            continue
        if is_0dte_option_contract(c, market_date=market_date, allow_any_expiry=allow_any_expiry):
            target_positions.append(pf)
            seen_con_ids.add(c.conId)

    print(f"-> 檢測到全帳戶持倉: {len(positions)} 筆 | 符合 0DTE 指數期權部位: {len(target_positions)} 筆")

    opt_close_results = []
    if not target_positions:
        print("-> ✅ 目前帳戶無任何 0DTE 指數期權未平倉部位，無需平倉。")
        return opt_close_results

    for idx, p in enumerate(target_positions, 1):
        print(f"\n--- [0DTE 期權平倉組別 {idx}/{len(target_positions)}] ---")
        res = execute_position_close_walk_up(
            p=p,
            category_name="0DTE 指數期權部位",
            dry_run=dry_run,
            use_market=use_market,
            outside_rth=True  # SPXW 期權支援 ETH / Curb 盤外撮合時段
        )
        opt_close_results.append(res)

    return opt_close_results


# ==============================================================================
# 5. 掃描持倉中所有 MES 已成交部位並啟動 Custom Walk-Up 平倉
# ==============================================================================
def close_all_mes_positions(dry_run=False, use_market=False):
    """
    掃描 IBKR 帳戶持倉 (positions)，找出所有 MES 微型標普期貨部位並立即啟動 Custom Walk-Up 平倉。
    回傳平倉結果列表。
    """
    print("\n" + "=" * 70)
    print("🚀 【步驟 3】掃描目前持倉中 MES 已成交部位 -> 啟動 Custom Walk-Up 步進平倉")
    print("=" * 70)

    try:
        ib.reqPositions()
        ib.sleep(0.8)
    except Exception:
        pass

    positions = ib.positions()
    portfolio_items = ib.portfolio()

    target_mes_positions = []
    seen_con_ids = set()

    for p in positions:
        if getattr(p, 'position', 0) == 0:
            continue
        c = p.contract
        if c.conId in seen_con_ids:
            continue
        if is_mes_contract(c):
            target_mes_positions.append(p)
            seen_con_ids.add(c.conId)

    for pf in portfolio_items:
        if getattr(pf, 'position', 0) == 0:
            continue
        c = pf.contract
        if c.conId in seen_con_ids:
            continue
        if is_mes_contract(c):
            target_mes_positions.append(pf)
            seen_con_ids.add(c.conId)

    print(f"-> 檢測到全帳戶持倉: {len(positions)} 筆 | 符合 MES 期貨部位: {len(target_mes_positions)} 筆")

    mes_close_results = []
    if not target_mes_positions:
        print("-> ✅ 目前帳戶無任何 MES 期貨未平倉部位，無需平倉。")
        return mes_close_results

    for idx, p in enumerate(target_mes_positions, 1):
        print(f"\n--- [MES 期貨平倉組別 {idx}/{len(target_mes_positions)}] ---")
        res = execute_position_close_walk_up(
            p=p,
            category_name="MES 期貨對沖部位",
            dry_run=dry_run,
            use_market=use_market,
            outside_rth=True  # CME Globex 24 小時電子交易
        )
        mes_close_results.append(res)

    return mes_close_results


# ==============================================================================
# 6. 發送 LINE 總結推播
# ==============================================================================
def send_summary_notification(cancel_results, opt_close_results, mes_close_results, dry_run=False, no_line=False):
    """將撤單與 0DTE 期權 / MES 步進平倉結果整理成報表推播至手機 LINE。"""
    if no_line:
        print("\n-> ℹ️ 依指令參數 --no-line，略過 LINE 推播通知。")
        return

    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    market_date = get_market_today()
    mode_text = '【模擬測試 Dry-Run】' if dry_run else '【正式執行 Live】'

    lines = [
        f"🚨{mode_text} DTE0 緊急撤單與 Walk-Up 平倉回報",
        f"🕒 執行時間：{now_str}",
        f"📅 美東基準日：{market_date}",
        f"----------------------------------------",
    ]

    # 1. 撤單成果
    lines.append(f"🛑【未成交掛單撤銷 (SPX/MES)】(共 {len(cancel_results)} 筆)")
    if cancel_results:
        for idx, r in enumerate(cancel_results, 1):
            status_desc = "模擬撤單" if dry_run else r.get('new_status', '已取消')
            lines.append(
                f"  {idx}. #{r.get('order_id')} {r.get('local_symbol')}: "
                f"{r.get('action')} {r.get('quantity')}口 -> {status_desc}"
            )
    else:
        lines.append("  • 無任何未成交掛單")

    lines.append("")

    # 2. 0DTE 期權平倉成果
    lines.append(f"⚡【0DTE 指數期權部位平倉 (Walk-Up)】(共 {len(opt_close_results)} 筆)")
    if opt_close_results:
        for idx, r in enumerate(opt_close_results, 1):
            if dry_run:
                status_desc = "模擬平倉"
            elif r.get('status') == 'Filled':
                status_desc = f"已成交 (${r.get('filled_price', 0):.2f})"
            elif r.get('status') in ('Submitted', 'PreSubmitted'):
                status_desc = f"已排單 ({r.get('status')})"
            else:
                status_desc = r.get('status', '處理中')

            lines.append(
                f"  {idx}. {r.get('local_symbol')}: "
                f"{r.get('close_action')} {r.get('close_qty')}口 -> {status_desc}"
            )
    else:
        lines.append("  • 無任何 0DTE 指數期權部位需平倉")

    lines.append("")

    # 3. MES 期貨平倉成果
    lines.append(f"🎯【MES 期貨部位平倉 (Walk-Up)】(共 {len(mes_close_results)} 筆)")
    if mes_close_results:
        for idx, r in enumerate(mes_close_results, 1):
            if dry_run:
                status_desc = "模擬平倉"
            elif r.get('status') == 'Filled':
                status_desc = f"已成交 (${r.get('filled_price', 0):.2f})"
            elif r.get('status') in ('Submitted', 'PreSubmitted'):
                status_desc = f"已排單 ({r.get('status')})"
            else:
                status_desc = r.get('status', '處理中')

            lines.append(
                f"  {idx}. {r.get('local_symbol')}: "
                f"{r.get('close_action')} {r.get('close_qty')}口 -> {status_desc}"
            )
    else:
        lines.append("  • 無任何 MES 期貨部位需平倉")

    error_items = [r for r in (cancel_results + opt_close_results + mes_close_results) if r.get('error')]
    if error_items:
        lines.append("\n⚠️ 異常回報：")
        for err_r in error_items:
            lines.append(f"  • {err_r.get('local_symbol', '項目')}: {err_r.get('error')}")

    message_text = "\n".join(lines)

    payload = {
        "action": "DTE0_WALK_UP_CANCEL_AND_CLOSE",
        "mode": "dry_run" if dry_run else "live",
        "market_date": str(market_date),
        "total_cancelled": len(cancel_results),
        "total_opt_closed": len(opt_close_results),
        "total_mes_closed": len(mes_close_results),
        "cancel_orders": cancel_results,
        "opt_close_orders": opt_close_results,
        "mes_close_orders": mes_close_results,
    }

    try:
        ok = send_trade_notification('DTE0_CANCEL', message_text, payload)
        if not ok:
            ok = send_push_message(message_text)
        if ok:
            print("\n-> 📲 [LINE 推播成功] 已發送 DTE0 / MES 撤單與平倉彙總通知至手機！")
        else:
            print("\n-> ⚠️ [LINE 推播失敗] 無法送出通知。")
    except Exception as e:
        print(f"\n-> ❌ [LINE 推播異常] {e}")


# ==============================================================================
# 7. 主程式入口
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="即時連線 IBKR，撤銷 SPX / MES 未成交掛單，並以 Custom Walk-Up 步進修單平倉 0DTE 指數期權與 MES 部位"
    )
    parser.add_argument("--dry-run", action="store_true", help="模擬模式（僅查詢列出，不實際發送撤單與平倉請求）")
    parser.add_argument("--cancel-only", action="store_true", help="僅執行撤銷未成交掛單，不進行平倉")
    parser.add_argument("--close-only", action="store_true", help="僅執行平倉（期權與 MES），不執行撤銷掛單")
    parser.add_argument("--opt-only", action="store_true", help="僅執行 0DTE 指數期權部位平倉")
    parser.add_argument("--mes-only", action="store_true", help="僅執行 MES 期貨部位平倉")
    parser.add_argument("--no-line", action="store_true", help="不發送手機 LINE 推播通知")
    parser.add_argument("--all-index", action="store_true", help="不限 0DTE 到期日，平倉所有 SPX/NDX/RUT/DJX 指數期權")
    parser.add_argument("--market", action="store_true", help="緊急兜底：改用市價單 (Market Order) 平倉，不使用 Walk-Up 步進")

    args = parser.parse_args()

    print("=" * 70)
    print("🛡️ 【DTE0 & MES 緊急撤單與 Custom Walk-Up 步進平倉系統】")
    print(f"⏰ 啟動時間: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if args.dry_run:
        print("💡 [模式設定] DRY-RUN 模擬模式已啟動（安全檢視，不向市場送出交易指令）")
    if args.market:
        print("⚠️ [模式設定] 已啟用 --market 緊急市價單兜底模式！")
    print("=" * 70)

    cancel_results = []
    opt_close_results = []
    mes_close_results = []

    try:
        connect_ib()

        # 1. 撤銷有效掛單 (除非指定 --close-only 或 --opt-only 或 --mes-only)
        if not (args.close_only or args.opt_only or args.mes_only):
            cancel_results = cancel_all_dte0_and_mes_orders(
                dry_run=args.dry_run,
                allow_any_expiry=args.all_index
            )

        # 2. 0DTE 指數期權部位 Custom Walk-Up 平倉 (除非指定 --cancel-only 或 --mes-only)
        if not (args.cancel_only or args.mes_only):
            opt_close_results = close_all_0dte_option_positions(
                dry_run=args.dry_run,
                allow_any_expiry=args.all_index,
                use_market=args.market
            )

        # 3. MES 期貨部位 Custom Walk-Up 平倉 (除非指定 --cancel-only 或 --opt-only)
        if not (args.cancel_only or args.opt_only):
            mes_close_results = close_all_mes_positions(
                dry_run=args.dry_run,
                use_market=args.market
            )

        # 4. 同步將 trade/.env 中 DTE0_CONFIG_JSON 的 wing_call 與 wing_put 設為 NIL
        if clear_dte0_wings:
            if not args.dry_run:
                clear_dte0_wings("SPX")
            else:
                print("\n💡 [模擬模式] 檢視 DTE0 翼點位狀態 (正式執行時將重置為 NIL):")
                if load_dte0_config:
                    cur_cfg = load_dte0_config().get("SPX", {})
                    print(f"   • 目前 SPX: wing_call={cur_cfg.get('wing_call')}, wing_put={cur_cfg.get('wing_put')}")

        # 5. 發送手機 LINE 推播通知
        send_summary_notification(
            cancel_results=cancel_results,
            opt_close_results=opt_close_results,
            mes_close_results=mes_close_results,
            dry_run=args.dry_run,
            no_line=args.no_line
        )

        print("\n" + "=" * 70)
        print(f"🎉 【執行完畢】已撤單: {len(cancel_results)} 筆 | 已平倉 0DTE 期權: {len(opt_close_results)} 筆 | 已平倉 MES: {len(mes_close_results)} 筆")
        print("=" * 70)

    except Exception as e:
        print(f"\n❌ [系統錯誤] 執行異常: {e}")
        import traceback
        traceback.print_exc()

    finally:
        if ib.isConnected():
            try:
                ib.disconnect()
            except Exception:
                pass


if __name__ == '__main__':
    main()
    time.sleep(60 * 60 * 20)
    print("完成時間:", datetime.datetime.now().strftime('%H:%M:%S'))
