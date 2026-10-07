#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DTE0 Real-Time Wing Breakout Hedge Module (trade/DTE0_HEDGE.py)
================================================================================
專門監控 SPX 0DTE 鐵蝶式上下翼幅，並依據 SPX 現價動態平衡 MES 即時庫存：
  1. 連線至 IBKR TWS / Gateway。
  2. 讀取 trade/.env 中的 DTE0_CONFIG_JSON 中的 SPX：
     - hedge_sym: 對沖期貨標的 (例如 "MES")
     - hedge_qty: 目標對沖單位口數 qty (預設 1)
     - wing_call: 上翼履約價點位 (例如 "7840")
     - wing_put : 下翼履約價點位 (例如 "7800")
     - 若 wing_call 或 wing_put 為 "NIL"，代表無生效之 0DTE 部位或已撤單平倉，安全略過。
  3. 即時查詢 SPX 底層指數現價 (spx_price)。
  4. 即時查詢目前帳戶中 hedge_sym ("MES") 的即時庫存 (current_pos)。
  5. 翼幅與即時庫存目標比對：
     - 若 spx_price > wing_call:
          hedge_sym ("MES") 的即時庫存應該為 +qty (多頭保護)
     - 若 spx_price < wing_put:
          hedge_sym ("MES") 的即時庫存應該為 -qty (空頭保護)
     - 其他情況 (wing_put <= spx_price <= wing_call):
          hedge_sym ("MES") 的即時庫存應該為 0 (回歸安全區間歸零)
  6. 若「即時庫存」不等於「目標庫存」，計算差異 diff = target_pos - current_pos：
     - diff > 0 -> 買進 (BUY) abs(diff) 口
     - diff < 0 -> 賣出 (SELL) abs(diff) 口
     - diff == 0 -> 庫存已達標，無需下單！
  7. 採用 Custom Walk-Up 步進修單下單，防止滑價與死水掛單。
  8. 整合手機 LINE 即時推播回報對沖結果。

指令範例:
  python trade/DTE0_HEDGE.py              # 常駐模式 (預設每 5 分鐘循環檢查一次)
  python trade/DTE0_HEDGE.py --once       # 單次執行檢查後立即結束
  python trade/DTE0_HEDGE.py --dry-run    # 模擬檢視模式 (不向市場送出委託)
  python trade/DTE0_HEDGE.py --no-line    # 略過 LINE 推播
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

from ib_insync import IB, Contract, Index, Option, Future, LimitOrder, MarketOrder, Trade

# 確保 Windows 主控台與子行程正確輸出 UTF-8 字符，避免 UnicodeEncodeError
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
        sys.stderr.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
    except Exception:
        pass

# 載入 LINE 推播模組
try:
    from notifier import send_push_message, send_trade_notification
except ImportError:
    try:
        from trade.notifier import send_push_message, send_trade_notification
    except ImportError:
        send_push_message = lambda *a, **kw: False
        send_trade_notification = lambda *a, **kw: False

# 載入 DTE0 設定模組
try:
    from dte0_config import load_dte0_config, update_dte0_wings, clear_dte0_wings
except ImportError:
    try:
        from trade.dte0_config import load_dte0_config, update_dte0_wings, clear_dte0_wings
    except ImportError:
        load_dte0_config = lambda: {"SPX": {"symbols": ["SPX"], "hedge_sym": "MES", "hedge_qty": 1, "wing_call": "NIL", "wing_put": "NIL"}}
        update_dte0_wings = None
        clear_dte0_wings = None

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
        extract_valid_price = lambda *args: next((float(x) for x in args if x is not None and not math.isnan(float(x)) and float(x) > 0), None)
        determine_min_tick_and_step = lambda c, p=None: (0.25, 0.25, 2.0) if getattr(c, 'secType', '') in ('FUT', 'CONTFUT') else (0.05, 0.05, 0.15)
        fmt_price = lambda p, t=None: f"{float(p):.2f}"

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




env_config = load_env_config()
IB_HOST = env_config.get('IB_HOST', '127.0.0.1')
IB_PORT = int(env_config.get('IB_PORT', 4001))
BASE_CLIENT_ID = int(env_config.get('IB_CLIENT_ID', 1001))
DTE0_HEDGE_CLIENT_ID = BASE_CLIENT_ID + 11
TARGET_ACCOUNT = env_config.get('IB_TARGET_ACCOUNT', '').strip()

ib = IB()
RECENT_IB_ERRORS = []


def on_ib_error(reqId, errorCode, errorString, contract):
    RECENT_IB_ERRORS.append((reqId, errorCode, errorString, contract))
    if errorCode in (110, 201, 103, 321, 200, 399, 10349):
        print(f"[IB 警示/錯誤] ReqId: {reqId} | 代碼: {errorCode} | 訊息: {errorString}")


ib.errorEvent += on_ib_error


def connect_ib():
    """建立與 IBKR 的連線。"""
    if not ib.isConnected():
        connected = False
        cids_to_try = [
            DTE0_HEDGE_CLIENT_ID,
            BASE_CLIENT_ID + 12,
            BASE_CLIENT_ID + 81,
            BASE_CLIENT_ID + 92,
            0
        ]
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
            raise ConnectionError(f"無法連接至 IBKR ({IB_HOST}:{IB_PORT})，已嘗試 ClientId: {cids_to_try}...")

        try:
            ib.reqMarketDataType(3)  # 即時延遲/凍結行情
        except Exception:
            pass

        actual_id = ib.client.clientId if ib.client else DTE0_HEDGE_CLIENT_ID
        print(f"=== [DTE0 Hedge] 成功連接至 IBKR ({IB_HOST}:{IB_PORT}) | ClientId={actual_id} ===")


# ==============================================================================
# 1. 取得 SPX 即時價格
# ==============================================================================
def get_spx_live_price() -> float | None:
    """取得 SPX 底層指數最新即時市價。"""
    try:
        ib.reqMarketDataType(3)
    except Exception:
        pass

    spx_contract = Index('SPX', 'CBOE', currency='USD')
    try:
        ib.qualifyContracts(spx_contract)
    except Exception:
        spx_contract = Index('SPX', 'SMART', currency='USD')
        try:
            ib.qualifyContracts(spx_contract)
        except Exception:
            pass

    ticker = ib.reqMktData(spx_contract, '', False, False)
    price = None
    for _ in range(3):
        ib.sleep(1.0)
        try:
            mp = ticker.marketPrice()
            if is_valid_price(mp):
                price = float(mp)
                break
        except Exception:
            pass

        for cand in [ticker.last, getattr(ticker, 'markPrice', None), ticker.close]:
            if is_valid_price(cand):
                price = float(cand)
                break
        if price is not None:
            break

        if is_valid_price(ticker.bid) and is_valid_price(ticker.ask):
            price = (float(ticker.bid) + float(ticker.ask)) / 2.0
            break

    try:
        ib.cancelMktData(spx_contract)
    except Exception:
        pass

    # 備援 1: 從帳戶既有的 SPXW 期權部位中提取標的市價 (underlyingPrice)
    if not is_valid_price(price):
        for p in ib.portfolio():
            if getattr(p.contract, 'symbol', '') in ('SPX', 'SPXW') and p.contract.secType == 'OPT':
                try:
                    t_opt = ib.reqMktData(p.contract, '', False, False)
                    ib.sleep(0.8)
                    mg = getattr(t_opt, 'modelGreeks', None)
                    if mg and is_valid_price(getattr(mg, 'undPrice', None)):
                        price = float(mg.undPrice)
                    ib.cancelMktData(p.contract)
                    if price:
                        break
                except Exception:
                    pass

    # 備援 2: 查詢 SPX 1 分鐘或日 K 線收盤
    if not is_valid_price(price):
        try:
            bars = ib.reqHistoricalData(
                spx_contract,
                endDateTime='',
                durationStr='1 D',
                barSizeSetting='1 min',
                whatToShow='TRADES',
                useRTH=False,
                formatDate=1
            )
            if bars and len(bars) > 0 and is_valid_price(bars[-1].close):
                price = float(bars[-1].close)
        except Exception:
            pass

    return price if is_valid_price(price) else None


## ==============================================================================
# 2. 即時查詢 hedge_sym (例如 MES) 的即時庫存 (未平倉口數)
# ==============================================================================
def get_current_hedge_position(ib_instance: IB, hedge_sym: str = "MES") -> tuple[float, list]:
    """
    即時查詢目前帳戶中 hedge_sym (例如 MES) 的未平倉口數 (即時庫存)。
    回傳: (總口數, 相關合約持倉列表)
    """
    try:
        ib_instance.reqPositions()
        ib_instance.sleep(0.6)
    except Exception:
        pass

    positions = ib_instance.positions()
    total_pos = 0.0
    matched_items = []
    seen_con_ids = set()
    target_sym = hedge_sym.strip().upper()

    for p in positions:
        c = p.contract
        if not c or c.conId in seen_con_ids:
            continue
        sec_type = (getattr(c, 'secType', '') or '').upper()
        symbol = (getattr(c, 'symbol', '') or '').upper()
        local_symbol = (getattr(c, 'localSymbol', '') or '').upper()

        if sec_type in ('FUT', 'CONTFUT'):
            if symbol == target_sym or local_symbol.startswith(target_sym):
                pos_val = float(p.position)
                total_pos += pos_val
                matched_items.append(p)
                seen_con_ids.add(c.conId)

    return total_pos, matched_items


def get_current_position(symbol: str = "MES") -> float:
    """取得特定標的 (例如 MES) 之即時未平倉持倉口數。"""
    pos, _ = get_current_hedge_position(ib, symbol)
    return pos


# ==============================================================================
# 3. 取得 MES 目標期貨合約
# ==============================================================================
def get_mes_future_contract(hedge_sym: str = "MES", matched_positions: list = None) -> Contract | None:
    """取得最近月份可交易之 MES 微型標普期貨合約。"""
    if matched_positions:
        c = matched_positions[0].contract
        if not getattr(c, 'exchange', ''):
            c.exchange = 'CME'
        return c

    target_sym = hedge_sym.strip().upper()
    # 1. 優先檢查持倉中是否已持有該期貨合約
    for p in ib.positions():
        c = p.contract
        if getattr(c, 'secType', '') in ('FUT', 'CONTFUT') and (getattr(c, 'symbol', '').upper() == target_sym or getattr(c, 'localSymbol', '').upper().startswith(target_sym)):
            if not getattr(c, 'exchange', ''):
                c.exchange = 'CME'
            return c

    # 2. 查詢 CME 交易所之所有未到期合約
    try:
        details = ib.reqContractDetails(Future(symbol=target_sym, exchange='CME', currency='USD'))
        today_str = datetime.date.today().strftime('%Y%m%d')
        unexpired = [d for d in details if getattr(d.contract, 'lastTradeDateOrContractMonth', '') >= today_str]
        unexpired.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)
        if unexpired:
            return unexpired[0].contract
    except Exception as e:
        print(f"⚠️ [期貨搜尋] 查詢 CME {target_sym} 合約詳細資訊失敗: {e}")

    # 3. 備援建立並資格確認合約
    c_fallback = Future(symbol=target_sym, exchange='CME', currency='USD')
    try:
        ib.qualifyContracts(c_fallback)
    except Exception:
        pass
    return c_fallback


# ==============================================================================
# 4. 執行 DTE0 翼幅與即時庫存平衡檢查核心流程
# ==============================================================================
def check_and_hedge(dry_run=False, no_line=False, force_action=None) -> dict:
    """
    即時查詢 SPX 現價、wing_call/wing_put，以及 DTE0_CONFIG_JSON 中 MES 的即時持倉：
      - hedge_step: 階梯級距 (例如 5 點)
      - buffer: 遲滯緩衝區 (例如 2.5 點)
      - max_qty: 強制硬上限 (例如 2 口)
      - 多階階梯遲滯判斷 target_qty
    計算庫存差異並透過 Custom Walk-Up 自動調倉至目標庫存。
    """
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    result = {
        'timestamp': now_str,
        'spx_price': None,
        'wing_call': None,
        'wing_put': None,
        'hedge_sym': 'MES',
        'hedge_step': 5.0,
        'buffer': 2.5,
        'max_qty': 2,
        'current_pos': 0.0,
        'target_pos': 0.0,
        'diff': 0.0,
        'triggered': False,
        'action': None,
        'trade_qty': 0,
        'reason': '',
        'order_status': None,
        'fill_price': 0.0,
        'success': False,
    }

    # 1. 讀取 DTE0_CONFIG_JSON 中的 SPX 設定 (不使用 HEDGE_CONFIG_JSON)
    dte0_cfg = load_dte0_config()
    spx_cfg = dte0_cfg.get("SPX") or dte0_cfg.get("標普指數(SPX)") or {}

    hedge_sym = str(spx_cfg.get("hedge_sym", "MES")).upper()

    try:
        hedge_step = float(spx_cfg.get("hedge_step", 5.0))
    except (ValueError, TypeError):
        hedge_step = 5.0

    try:
        buffer = float(spx_cfg.get("buffer", 2.5))
    except (ValueError, TypeError):
        buffer = 2.5

    try:
        max_qty = int(spx_cfg.get("max_qty", 2))
    except (ValueError, TypeError):
        max_qty = 2

    w_call_raw = str(spx_cfg.get("wing_call", "NIL")).strip()
    w_put_raw = str(spx_cfg.get("wing_put", "NIL")).strip()

    result['wing_call'] = w_call_raw
    result['wing_put'] = w_put_raw
    result['hedge_sym'] = hedge_sym
    result['hedge_step'] = hedge_step
    result['buffer'] = buffer
    result['max_qty'] = max_qty

    print(f"\n[{now_str}] 🔍 [DTE0_HEDGE] 讀取參數: DTE0_CONFIG_JSON(SPX) -> hedge_sym={hedge_sym}, hedge_step={hedge_step}, buffer={buffer}, max_qty={max_qty} | wing_call={w_call_raw}, wing_put={w_put_raw}")

    # 2. 檢查 wing_call / wing_put 是否為有效數字 (若為 NIL 則略過)
    if w_call_raw.upper() in ('NIL', 'NONE', '') or w_put_raw.upper() in ('NIL', 'NONE', ''):
        msg = f"wing_call ({w_call_raw}) 或 wing_put ({w_put_raw}) 為 NIL，目前無生效之 0DTE 履約價外翼或已撤單平倉，略過對沖。"
        print(f"ℹ️ [DTE0_HEDGE] {msg}")
        result['reason'] = msg
        return result

    try:
        wing_call = float(w_call_raw)
        wing_put = float(w_put_raw)
    except ValueError:
        msg = f"無法將 wing_call ({w_call_raw}) 或 wing_put ({w_put_raw}) 轉換為數值，略過對沖。"
        print(f"⚠️ [DTE0_HEDGE] {msg}")
        result['reason'] = msg
        return result

    # 3. 取得 SPX 即時現價
    spx_price = get_spx_live_price()
    result['spx_price'] = spx_price

    if not is_valid_price(spx_price):
        msg = "無法取得 SPX 即時有效指數價格，跳過本輪對沖檢查。"
        print(f"❌ [DTE0_HEDGE] {msg}")
        result['reason'] = msg
        return result

    # 4. 即時查詢 hedge_sym ("MES") 目前實際持倉
    current_mes_pos = get_current_position(hedge_sym)
    current_pos, matched_positions = get_current_hedge_position(ib, hedge_sym)
    result['current_pos'] = current_mes_pos

    # 5. 階梯遲滯對沖狀態機 (多階加減倉與死區保護)
    condition_desc = ""

    if force_action == 'BUY':
        target_qty = min(max_qty, max(1, int(current_mes_pos + 1)))
        condition_desc = f"手動強制指定 BUY，目標持倉: +{target_qty}口"
    elif force_action == 'SELL':
        target_qty = max(-max_qty, min(-1, int(current_mes_pos - 1)))
        condition_desc = f"手動強制指定 SELL，目標持倉: {target_qty}口"

    # ================= 向上突破 (Call 側) =================
    elif spx_price >= wing_call - hedge_step:
        # 判斷是否滿足第 2 階
        if spx_price >= wing_call + hedge_step:
            target_qty = min(2, max_qty)
            condition_desc = f"SPX ({spx_price:.2f}) >= 2階上翼 ({wing_call + hedge_step:.2f})，目標持倉: +{target_qty}口"
        else:
            # 在第 1 階與第 2 階之間
            # 若原本持倉為 2 口，需跌破 (2*step - buffer) 才降回 1 口 (遲滯保護)
            if current_mes_pos >= 2 and spx_price < (wing_call + hedge_step - buffer):
                target_qty = 1
                condition_desc = f"SPX ({spx_price:.2f}) 跌破2階遲滯線 ({wing_call + hedge_step - buffer:.2f})，目標降回: +1口"
            else:
                target_qty = max(1, current_mes_pos)
                condition_desc = f"SPX ({spx_price:.2f}) 處於1階~2階區間，目標持倉維持: +{target_qty}口"

    # ================= 向下跌破 (Put 側) =================
    elif spx_price <= wing_put + hedge_step:
        if spx_price <= wing_put - hedge_step:
            target_qty = -min(2, max_qty)
            condition_desc = f"SPX ({spx_price:.2f}) <= 2階下翼 ({wing_put - hedge_step:.2f})，目標持倉: {target_qty}口"
        else:
            if current_mes_pos <= -2 and spx_price > (wing_put - hedge_step + buffer):
                target_qty = -1
                condition_desc = f"SPX ({spx_price:.2f}) 彈回2階遲滯線 ({wing_put - hedge_step + buffer:.2f})，目標升回: -1口"
            else:
                target_qty = min(-1, current_mes_pos)
                condition_desc = f"SPX ({spx_price:.2f}) 處於1階~2階下跌區間，目標持倉維持: {target_qty}口"

    # ================= 中央死區 (回歸中性) =================
    else:
        # 只有當行情深跌回 (wing_call - hedge_step - buffer) 以下，才允許將多單清零
        if current_mes_pos > 0 and spx_price <= (wing_call - hedge_step - buffer):
            target_qty = 0
            condition_desc = f"SPX ({spx_price:.2f}) 回跌至多單清零線 ({wing_call - hedge_step - buffer:.2f}) 以下，目標多單清零: 0口"
        # 只有當行情強彈回 (wing_put + hedge_step + buffer) 以上，才允許將空單清零
        elif current_mes_pos < 0 and spx_price >= (wing_put + hedge_step + buffer):
            target_qty = 0
            condition_desc = f"SPX ({spx_price:.2f}) 強彈至空單清零線 ({wing_put + hedge_step + buffer:.2f}) 以上，目標空單清零: 0口"
        else:
            target_qty = current_mes_pos  # 維持原狀，不動作！
            condition_desc = f"SPX ({spx_price:.2f}) 位於中央死區/遲滯緩衝區內，持倉維持原狀: {current_mes_pos:g}口"

    # 確保不超越限制上限
    target_qty = max(-max_qty, min(max_qty, int(target_qty)))
    target_pos = target_qty
    result['target_pos'] = target_pos

    # 6. 計算庫存差距 (diff = target_qty - current_mes_pos)
    diff = target_qty - current_mes_pos
    result['diff'] = diff

    print(f"📊 [DTE0_HEDGE 階梯遲滯庫存檢視]")
    print(f"   • SPX 即時現價: {spx_price:.2f} (上翼: {wing_call:.2f}, 下翼: {wing_put:.2f}, 級距: {hedge_step}, 緩衝: {buffer})")
    print(f"   • {hedge_sym} 即時持倉: {current_mes_pos:g} 口")
    print(f"   • {hedge_sym} 目標持倉: {target_qty:g} 口")
    print(f"   • 持倉差異 (diff): {diff:+g} 口")
    print(f"   • 判定依據: {condition_desc}")

    # 若持倉已完全符合目標持倉，無需進行任何調倉下單
    if abs(diff) < 1e-4:
        msg = f"目前 {hedge_sym} 即時持倉 ({current_mes_pos:g}口) 已完全符合目標持倉 ({target_qty:g}口)，無需下單。"
        print(f"✅ [DTE0_HEDGE] {msg}")
        result['reason'] = msg
        return result

    # 7. 決定下單方向與數量
    if diff > 0:
        action = 'BUY'
        trade_qty = int(round(abs(diff)))
    else:
        action = 'SELL'
        trade_qty = int(round(abs(diff)))

    reason = f"{condition_desc} (現有持倉 {current_mes_pos:g}口 -> 目標持倉 {target_qty:g}口，需 {action} {trade_qty}口)"
    result['triggered'] = True
    result['action'] = action
    result['trade_qty'] = trade_qty
    result['reason'] = reason

    print(f"\n🚨 [DTE0_HEDGE 觸發調倉下單] {reason}")
    print(f"   -> 執行動作: {action} {trade_qty}口 {hedge_sym}")

    # 8. 鎖定 MES 期貨合約
    mes_contract = get_mes_future_contract(hedge_sym=hedge_sym, matched_positions=matched_positions)
    if not mes_contract:
        msg = f"找不到可交易的 {hedge_sym} 期貨合約，中止下單！"
        print(f"❌ [DTE0_HEDGE] {msg}")
        result['reason'] += f" | {msg}"
        return result

    disp_sym = getattr(mes_contract, 'localSymbol', mes_contract.symbol)
    print(f"🎯 [DTE0_HEDGE] 鎖定對沖期貨: {disp_sym} (conId: {mes_contract.conId})")

    # 9. 取得 MES 即時報價並計算起始限價
    future_mkt_price = None
    try:
        ib.reqMarketDataType(3)
        t_ticker = ib.reqMktData(mes_contract, '', False, False)
        ib.sleep(1.2)
        if action == 'BUY':
            future_mkt_price = extract_valid_price(t_ticker.bid, ((t_ticker.bid + t_ticker.ask) / 2.0) if (is_valid_price(t_ticker.bid) and is_valid_price(t_ticker.ask)) else None, t_ticker.last, t_ticker.close)
        else:
            future_mkt_price = extract_valid_price(t_ticker.ask, ((t_ticker.bid + t_ticker.ask) / 2.0) if (is_valid_price(t_ticker.bid) and is_valid_price(t_ticker.ask)) else None, t_ticker.last, t_ticker.close)
        ib.cancelMktData(mes_contract)
    except Exception as pe:
        print(f"⚠️ [DTE0_HEDGE] 查詢期貨即時價異常: {pe}")

    if not is_valid_price(future_mkt_price):
        try:
            bars = ib.reqHistoricalData(mes_contract, '', '1 D', '1 min', 'TRADES', False, 1, False)
            if bars and len(bars) > 0 and is_valid_price(bars[-1].close):
                future_mkt_price = float(bars[-1].close)
        except Exception:
            pass

    if not is_valid_price(future_mkt_price):
        msg = f"無法取得 {disp_sym} 有效市場價格，中止對沖下單！"
        print(f"❌ [DTE0_HEDGE] {msg}")
        result['reason'] += f" | {msg}"
        return result

    min_tick, step_val, max_slip = determine_min_tick_and_step(mes_contract, future_mkt_price)
    start_price = round_to_tick(future_mkt_price, min_tick)

    print(f"⚡ [DTE0_HEDGE] 準備啟動 Custom Walk-Up 步進修單:")
    print(f"   • 標的: {disp_sym} | 調倉: {action} {trade_qty}口 (目前庫存 {current_pos:g} -> 目標 {target_pos:g})")
    print(f"   • 起始限價: ${fmt_price(start_price, min_tick)} (跳動點: {min_tick}, 步階: {step_val}, 最大滑價: {max_slip})")

    # 10. 模擬模式處理
    if dry_run:
        print(f"💡 [模擬模式] DRY-RUN 啟用，略過真實送單。")
        result['order_status'] = 'DryRun'
        result['fill_price'] = start_price
        result['success'] = True
        return result

    # 11. 正式送單 (Custom Walk-Up)
    try:
        filled, trade, avg_price = execute_walk_up_order(
            ib=ib,
            contract=mes_contract,
            action=action,
            quantity=trade_qty,
            current_mid=start_price,
            max_slippage=max_slip,
            step=step_val,
            step_time=3.0,
            max_steps=3,
            symbol=f"DTE0-Hedge-{disp_sym}",
            account=TARGET_ACCOUNT,
            tif='DAY',
            outside_rth=True,
            min_tick=min_tick
        )

        result['fill_price'] = avg_price
        if filled:
            result['order_status'] = 'Filled'
            result['success'] = True
            print(f"✅ [DTE0_HEDGE 對沖完全成交] 均價: ${fmt_price(avg_price, min_tick)} | 新庫存應為: {target_pos:g}口")
        else:
            final_st = getattr(getattr(trade, 'orderStatus', None), 'status', 'Cancelled')
            result['order_status'] = final_st
            result['success'] = False
            print(f"⚠️ [DTE0_HEDGE 對沖未成交] 狀態: {final_st} (步進逾時已徹底撤單)")

    except Exception as we:
        print(f"❌ [DTE0_HEDGE 下單異常] {we}")
        result['order_status'] = 'Error'
        result['reason'] += f" | 錯誤: {we}"

    # 12. LINE 推播通知
    if not no_line:
        mode_str = "【模擬測試】" if dry_run else "【正式下單】"
        status_disp = f"已成交 (${result['fill_price']:.2f})" if result['success'] else (result['order_status'] or "未成交")
        line_msg = (
            f"🚨{mode_str} DTE0 翼幅庫存調倉通知\n"
            f"🕒 時間：{now_str}\n"
            f"📈 SPX 現價：{spx_price:.2f}\n"
            f"🛡️ 翼幅界限：下翼 {wing_put:.2f} ~ 上翼 {wing_call:.2f}\n"
            f"----------------------------------------\n"
            f"📦 庫存調整：{current_pos:g}口 -> 目標 {target_pos:g}口\n"
            f"⚡ 調倉指令：{action} {trade_qty}口 {disp_sym}\n"
            f"🎯 原因說明：{condition_desc}\n"
            f"📊 執行結果：{status_disp}"
        )
        payload = {
            "strategy": "DTE0_WING_INVENTORY_HEDGE",
            "spx_price": spx_price,
            "wing_call": wing_call,
            "wing_put": wing_put,
            "current_pos": current_pos,
            "target_pos": target_pos,
            "diff": diff,
            "action": action,
            "trade_qty": trade_qty,
            "hedge_sym": hedge_sym,
            "fill_price": result['fill_price'],
            "status": result['order_status'],
            "mode": "dry_run" if dry_run else "live"
        }
        try:
            ok = send_trade_notification('DTE0_HEDGE', line_msg, payload)
            if not ok:
                send_push_message(line_msg)
            print("📲 [LINE 推播] 對沖回報通知發送成功。")
        except Exception as le:
            print(f"⚠️ [LINE 推播失敗] {le}")

    return result


# ==============================================================================
# 5. 主程式入口
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="即時監控 SPX 0DTE 鐵蝶式翼幅點位，依據 SPX 現價動態平衡 MES 即時庫存"
    )
    parser.add_argument("--dry-run", action="store_true", help="模擬模式（僅查詢價格與庫存條件，不送出真實委託）")
    parser.add_argument("--no-line", action="store_true", help="不發送手機 LINE 推播通知")
    parser.add_argument("--once", action="store_true", help="強制僅執行單次檢查後結束（不進入 5 分鐘循環）")
    parser.add_argument("--loop", type=float, default=0.0, help="持續監控輪詢秒數 (例如 --loop 5 每 5 秒檢查一次，0 為單次執行)")
    parser.add_argument("--force-buy", action="store_true", help="測試用：強制設定目標庫存為 +qty")
    parser.add_argument("--force-sell", action="store_true", help="測試用：強制設定目標庫存為 -qty")

    args = parser.parse_args()

    print("=" * 70)
    print("🛡️ 【DTE0 SPX 翼幅突破庫存平衡對沖系統】(trade/DTE0_HEDGE.py)")
    print(f"⏰ 啟動時間: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if args.dry_run:
        print("💡 [模式設定] DRY-RUN 模擬模式已啟動（安全檢視，不向市場送出交易指令）")
    if args.loop > 0:
        print(f"🔄 [模式設定] 持續高頻輪詢模式已啟動 (間隔: {args.loop} 秒)")
    print("=" * 70)

    try:
        connect_ib()

        force_act = 'BUY' if args.force_buy else ('SELL' if args.force_sell else None)

        if args.loop > 0:
            last_hedge_time = 0.0
            cooldown_seconds = 60.0
            while True:
                try:
                    res = check_and_hedge(dry_run=args.dry_run, no_line=args.no_line, force_action=force_act)
                    if res.get('triggered') and res.get('success'):
                        last_hedge_time = time.time()
                        print(f"⏳ [冷卻中] 調倉已觸發，進入 {cooldown_seconds} 秒保護冷卻以防高頻重複對沖...")
                        time.sleep(cooldown_seconds)
                    else:
                        time.sleep(args.loop)
                except KeyboardInterrupt:
                    print("\n🛑 使用者中斷，停止監控。")
                    break
                except Exception as inner_e:
                    print(f"⚠️ 輪詢異常: {inner_e}")
                    time.sleep(args.loop)
        else:
            check_and_hedge(dry_run=args.dry_run, no_line=args.no_line, force_action=force_act)

        print("\n" + "=" * 70)
        print("🎉 【執行完畢】")
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
    is_once = '--once' in sys.argv or ('--dry-run' in sys.argv and '--loop' not in sys.argv)
    if is_once:
        main()
        print("完成時間:", datetime.datetime.now().strftime('%H:%M:%S'))
    else:
        while True:
            try:
                main()
            except KeyboardInterrupt:
                print("\n🛑 使用者中斷，退出程式。")
                break
            except Exception as e:
                print(f"⚠️ 循環執行異常: {e}")
            time.sleep(60 * 5)
            print("完成時間:", datetime.datetime.now().strftime('%H:%M:%S'))
