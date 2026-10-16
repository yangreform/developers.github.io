#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DTE0 SPX Short Iron Butterfly with Bracket Take-Profit (trade/DTE0_SELL.py)
================================================================================
專門處理 0DTE 指數期權 (SPX) 雙賣 ATM、雙買外翼 (Short Iron Butterfly)，
並自 trade/.env 中的 OP_HEDGE_CONFIG_JSON 讀取 "TP" (Take Profit 停利價)，
在下單時一併掛出「括號停利單 (Attached Bracket Take-Profit Limit Order)」：
  1. 母單 (Parent Order):
     - 委託動作: 限價賣出 (SELL) 1 口
     - 委託價格: bid_up (如 $9.0)
     - 結構: 雙賣 ATM C&P，雙買 wing_width_call / wing_width_put 外翼
  2. 括號停利子單 (Attached Child Take-Profit Order):
     - 委託動作: 限價買回 (BUY) 1 口平倉
     - 委託價格: TP (如 $4.0)
     - 關聯: parentId = 母單 orderId, transmit = True (自動隨母單成交而啟動生效)
  3. 整合即時報價、Greeks 量化分析與手機 LINE 彙總推播。

指令範例:
  python trade/DTE0_SELL.py --dry-run          # 模擬檢視模式 (不實際向 IBKR 送單)
  python trade/DTE0_SELL.py --dry-run --no-line# 模擬檢視且不發 LINE
  python trade/DTE0_SELL.py                    # 正式執行掛單 (含 5 組母單 + 5 組停利單)
================================================================================
"""

import os
import sys
import json
import datetime
import time
import math

try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo

from ib_insync import (
    IB,
    Index,
    Option,
    Contract,
    ComboLeg,
    LimitOrder,
)

# 確保 Windows 主控台與子行程正確輸出 UTF-8 字符，避免 UnicodeEncodeError
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
        sys.stderr.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
    except Exception:
        pass

try:
    from notifier import send_push_message, send_trade_notification
except ImportError:
    try:
        from trade.notifier import send_push_message, send_trade_notification
    except ImportError:
        send_push_message = lambda *a, **kw: False
        send_trade_notification = lambda *a, **kw: False

if "--no-line" in sys.argv:
    send_push_message = lambda *a, **kw: None
    send_trade_notification = lambda *a, **kw: None

try:
    from skills.walk_up_skill import walk_up_limit_price
except ImportError:
    try:
        from trade.skills.walk_up_skill import walk_up_limit_price
    except ImportError:
        walk_up_limit_price = None

try:
    from dte0_config import update_dte0_wings, load_dte0_config
except ImportError:
    try:
        from trade.dte0_config import update_dte0_wings, load_dte0_config
    except ImportError:
        update_dte0_wings = None
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


def load_op_hedge_config():
    """即時解析 trade/.env 中的 OP_HEDGE_CONFIG_JSON。"""
    cfg = load_env_config()
    raw = cfg.get('OP_HEDGE_CONFIG_JSON', '{}')
    try:
        return json.loads(raw)
    except Exception as e:
        print(f"[錯誤] 即時解析 OP_HEDGE_CONFIG_JSON 失敗: {e}")
        return {}


def get_market_today():
    """
    獲取美東時間 (US Eastern Date)，作為美股/CBOE 指數期權 0DTE 的計算基準。
    """
    try:
        us_eastern = zoneinfo.ZoneInfo("America/New_York")
        return datetime.datetime.now(us_eastern).date()
    except Exception:
        return (datetime.datetime.utcnow() - datetime.timedelta(hours=4)).date()


def is_valid_price(p):
    """驗證價格是否為有效大於 0 的非 NaN 數值。"""
    if p is None:
        return False
    try:
        val = float(p)
        return not math.isnan(val) and val > 0
    except (ValueError, TypeError):
        return False


# ==============================================================================
# 1. 交易參數與指數標的配置 (專門處理 SPX, NDX, RUT, DJX)
# ==============================================================================
env_config = load_env_config()
IB_HOST = env_config.get('IB_HOST', '127.0.0.1')
IB_PORT = int(env_config.get('IB_PORT', 4001))
CLIENT_ID = int(env_config.get('IB_CLIENT_ID', 100)) + 9

TRADE_QTY = 1
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

WING_WIDTH_MAP = {
    'SPX': 10,
    'NDX': 40,
    'RUT': 10,
    'DJX': 2,
}

DEFAULT_BID_UP_MAP = {
    'SPX': 9.0,
    'NDX': 8.0,
    'RUT': 3.0,
    'DJX': 1.0,
}

DEFAULT_TP_MAP = {
    'SPX': 4.0,
    'NDX': 3.0,
    'RUT': 1.0,
    'DJX': 0.5,
}

DEFAULT_PRICE_JUMP_MAP = {
    'SPX': 20.0,
    'NDX': 25.0,
    'RUT': 10.0,
    'DJX': 2.0,
}

ib = IB()

RECENT_IB_ERRORS = []


def on_ib_error(reqId, errorCode, errorString, contract):
    RECENT_IB_ERRORS.append((reqId, errorCode, errorString, contract))
    if errorCode in (110, 201, 103, 321, 200, 399):
        print(f"[IB 委託拒絕/警示] ReqId: {reqId} | 代碼: {errorCode} | 訊息: {errorString}")


ib.errorEvent += on_ib_error

MARKET_RULES_CACHE = {}


def get_price_increment_rules(contract):
    if not contract or not getattr(contract, 'conId', None):
        return None
    con_id = contract.conId
    if con_id in MARKET_RULES_CACHE:
        return MARKET_RULES_CACHE[con_id]

    try:
        details = ib.reqContractDetails(contract)
        if details:
            mr_ids = getattr(details[0], 'marketRuleIds', '')
            if mr_ids:
                first_rule_id = int(mr_ids.split(',')[0])
                rules = ib.reqMarketRule(first_rule_id)
                if rules:
                    sorted_rules = sorted([(r.lowEdge, r.increment) for r in rules], key=lambda x: x[0])
                    MARKET_RULES_CACHE[con_id] = sorted_rules
                    return sorted_rules
    except Exception:
        pass

    return None


def round_to_valid_tick(symbol, price, contract=None):
    """修整 SPX / NDX 價格至合法的跳動點 (<3.0 -> 0.05, >=3.0 -> 0.10)。"""
    if price is None or price <= 0:
        price = 0.05

    rules = None
    if contract:
        rules = get_price_increment_rules(contract)

    if not rules:
        FALLBACK_RULES = {
            'SPX': [(0.0, 0.05), (3.0, 0.10)],
            'NDX': [(0.0, 0.05), (3.0, 0.10)],
            'RUT': [(0.0, 0.05), (3.0, 0.10)],
            'DJX': [(0.0, 0.01), (3.0, 0.05)],
        }
        rules = FALLBACK_RULES.get(symbol, [(0.0, 0.05), (3.0, 0.10)])

    if rules:
        inc = rules[0][1]
        for low_edge, increment in rules:
            if price >= low_edge:
                inc = increment
            else:
                break
        valid_price = round(price / inc) * inc
        return round(valid_price, 4)

    return round(round(price / 0.05) * 0.05, 4)


def connect_ib():
    if not ib.isConnected():
        base_id = CLIENT_ID
        connected = False
        # 優先嘗試 Master ClientId 0 (具備查詢與取消全帳號所有 Client 委託之最高權限)
        # 若 0 被佔用，則依序嘗試 base_id (1010) 及備援 ID
        cids_to_try = [0, base_id] + [base_id + offset for offset in range(1, 15)]
        for cur_id in cids_to_try:
            try:
                ib.connect(IB_HOST, IB_PORT, clientId=cur_id, timeout=4)
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
            raise ConnectionError(f"無法連接至 IBKR ({IB_HOST}:{IB_PORT})，已嘗試 clientId 0, {base_id} 至 {base_id+14}")

        try:
            ib.reqMarketDataType(3)
        except Exception:
            pass
        actual_id = ib.client.clientId if ib.client else CLIENT_ID
        print(f"=== [DTE0_SELL] 成功連接至 IBKR ({IB_HOST}:{IB_PORT}) | ClientId={actual_id} | 0DTE 賣方+括號停利專用系統 ===")


def cancel_conflicting_index_orders(symbol):
    """
    掃描全帳戶掛單，若發現同指數商品 (如 SPX) 的未成交掛單，立即發送撤單。
    防止遭遇 IBKR Error 201: 'Cannot have open orders on both sides of the same US Option contract.'
    (美股期權法規禁止一般客戶在同一期權合約兩側同時持有反向掛單)
    """
    try:
        open_trades = ib.reqAllOpenOrders()
    except Exception:
        open_trades = ib.openTrades()

    conflicts = []
    for t in open_trades:
        status = t.orderStatus.status
        if status in ('Filled', 'Cancelled', 'Inactive', 'ApiCancelled'):
            continue
        c = t.contract
        s = getattr(c, 'symbol', '') or ''
        sec_type = getattr(c, 'secType', '')
        if s == symbol or symbol in s:
            conflicts.append(t)
        elif sec_type == 'BAG':
            legs = getattr(c, 'comboLegs', []) or []
            if any(getattr(leg, 'symbol', '') == symbol for leg in legs):
                conflicts.append(t)
        elif sec_type in ('BAG', 'OPT') and symbol in ('SPX', 'NDX', 'RUT', 'DJX') and s in ('SPX', 'NDX', 'RUT', 'DJX', 'SPXW', 'NDXP', 'RUTW', 'DJXW'):
            conflicts.append(t)

    if conflicts:
        print(f"-> 🛡️ [自動防衝突] 檢測到 {len(conflicts)} 筆 {symbol} 現存舊掛單，為避免 Error 201 兩側掛單衝突，立即進行預先撤單...")
        for t in conflicts:
            try:
                ib.cancelOrder(t.order)
                try:
                    ib.client.cancelOrder(t.order.orderId)
                except Exception:
                    pass
                print(f"   ⚡ 已送出撤單: OrderId #{t.order.orderId} (ClientId: {t.order.clientId}) | {t.order.action} {t.order.totalQuantity}口 @ {t.order.lmtPrice}")
            except Exception as e:
                print(f"   ⚠️ 撤單 OrderId #{t.order.orderId} 異常: {e}")
        ib.sleep(1.5)


# ==============================================================================
# 2. 獲取標的現價
# ==============================================================================
def get_underlying_price(underlying):
    try:
        ib.reqMarketDataType(3)
    except Exception:
        pass

    ticker = ib.reqMktData(underlying, '', False, False)
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
        ib.cancelMktData(underlying)
    except Exception:
        pass

    if not is_valid_price(price):
        try:
            bars = ib.reqHistoricalData(
                underlying,
                endDateTime='',
                durationStr='2 D',
                barSizeSetting='1 day',
                whatToShow='TRADES',
                useRTH=False,
                formatDate=1
            )
            if bars:
                last_close = bars[-1].close
                if is_valid_price(last_close):
                    price = float(last_close)
        except Exception:
            pass

    return price if is_valid_price(price) else None


# ==============================================================================
# 3. 解析指數群組商品 (專責 SPX / NDX)
# ==============================================================================
def resolve_index_symbols(group_name, symbols):
    underlying_sym = None
    for sym in symbols:
        if sym in INDEX_SYMBOL_MAP:
            underlying_sym = INDEX_SYMBOL_MAP[sym]
            break
    if not underlying_sym and symbols:
        underlying_sym = INDEX_SYMBOL_MAP.get(symbols[0])

    if not underlying_sym or underlying_sym not in INDEX_SYMBOLS:
        return None, None, []

    if underlying_sym == 'SPX':
        exchange = 'CBOE'
    elif underlying_sym == 'NDX':
        exchange = 'NASDAQ'
    elif underlying_sym == 'RUT':
        exchange = 'RUSSELL'
    elif underlying_sym == 'DJX':
        exchange = 'CBOE'
    else:
        exchange = 'SMART'
    underlying_contract = Index(underlying_sym, exchange, currency='USD')
    try:
        ib.qualifyContracts(underlying_contract)
    except Exception:
        underlying_contract = Index(underlying_sym, 'SMART', currency='USD')
        try:
            ib.qualifyContracts(underlying_contract)
        except Exception:
            pass

    chains = ib.reqSecDefOptParams(underlying_contract.symbol, '', underlying_contract.secType, underlying_contract.conId)
    if not chains:
        chains = ib.reqSecDefOptParams(underlying_contract.symbol, 'SMART', underlying_contract.secType, underlying_contract.conId)

    preferred = PREFERRED_TRADING_CLASS_MAP.get(underlying_sym)
    trading_classes = set(c.tradingClass for c in chains) if chains else set()
    opt_class = preferred if (preferred and preferred in trading_classes) else (chains[0].tradingClass if chains else underlying_sym)
    return underlying_contract, opt_class, chains


# ==============================================================================
# 4. 獲取 0DTE 組合合約 (SPX - 4 腿鐵蝶式 Iron Butterfly 或 2 腿 Straddle)
# ==============================================================================
def get_dte0_butterfly_sets(underlying, opt_class, chains, wing_width=None, wing_width_call=None, wing_width_put=None, bid_up=None, tp=None, iron='short', price_jump=20.0):
    iron = str(iron).strip().lower() if iron else 'short'
    is_long = (iron != 'short')

    # 決定動態翼寬 wing_width_call 與 wing_width_put
    if wing_width_call is None and wing_width is not None:
        wing_width_call = wing_width
    if wing_width_put is None and wing_width is not None:
        wing_width_put = wing_width

    def_wing = float(WING_WIDTH_MAP.get(underlying.symbol, 10))
    try:
        wing_width_call = float(wing_width_call) if wing_width_call is not None else def_wing
    except (ValueError, TypeError):
        wing_width_call = def_wing

    try:
        wing_width_put = float(wing_width_put) if wing_width_put is not None else def_wing
    except (ValueError, TypeError):
        wing_width_put = def_wing

    is_atm_only = (wing_width_call == 0 and wing_width_put == 0)

    if is_atm_only:
        strategy_name = "Long Straddle (0DTE 雙腿買方)" if is_long else "Short Straddle (0DTE 雙腿賣方)"
        center_action_desc = "買入 (BUY)" if is_long else "賣出 (SELL)"
        wing_action_desc = ""
    else:
        strategy_name = "Long Iron Butterfly (0DTE 鐵蝶式買方)" if is_long else "Short Iron Butterfly (0DTE 鐵蝶式賣方 / 雙賣ATM雙買外翼)"
        center_action_desc = "買入 (BUY)" if is_long else "賣出 (SELL)"
        wing_action_desc = "賣出 (SELL)" if is_long else "買入 (BUY)"

    market_today = get_market_today()

    class_candidates = []
    if opt_class:
        for c in chains:
            if c.tradingClass == opt_class:
                for exp in c.expirations:
                    try:
                        exp_date = datetime.datetime.strptime(exp, '%Y%m%d').date()
                        dte = (exp_date - market_today).days
                        if dte == 0:  # 嚴格 0DTE
                            class_candidates.append((c.tradingClass, exp, c.strikes, dte, c.multiplier))
                    except Exception:
                        pass

    # 若休市無當天 0DTE 合約，選取 dte >= 0 且最近之到期日備選
    if not class_candidates and chains:
        for c in chains:
            if opt_class and c.tradingClass != opt_class:
                continue
            for exp in c.expirations:
                try:
                    exp_date = datetime.datetime.strptime(exp, '%Y%m%d').date()
                    dte = (exp_date - market_today).days
                    if dte >= 0:
                        class_candidates.append((c.tradingClass, exp, c.strikes, dte, c.multiplier))
                except Exception:
                    pass

    if not class_candidates:
        print(f"-> ❌ [無可用到期日] {underlying.symbol} 未找到符合 0DTE 之到期合約。")
        return []

    class_candidates.sort(key=lambda x: x[3])
    chosen_class, closest_expiry, available_strikes, current_dte, multiplier = class_candidates[0]
    print(f"-> 🎯 選定合約: {underlying.symbol} (Class: {chosen_class}, 到期日: {closest_expiry}, DTE: {current_dte}天)")

    ref_price = get_underlying_price(underlying)
    if not is_valid_price(ref_price):
        print(f"-> ❌ 無法取得 {underlying.symbol} 底層指數價格，跳過。")
        return []

    print(f"-> 標的現價: {ref_price:.2f}")

    opt_exchange = 'CBOE' if underlying.symbol == 'RUT' else 'SMART'

    # 各指數的履約價階梯單位 (NDX 為 25 點單位，SPX/RUT 為 5 點，DJX 為 1 點)
    STRIKE_STEP_MAP = {
        'NDX': 25.0,
        'SPX': 5.0,
        'RUT': 5.0,
        'DJX': 1.0,
    }
    step = STRIKE_STEP_MAP.get(underlying.symbol, 1.0 if underlying.symbol == 'DJX' else (25.0 if underlying.symbol == 'NDX' else 5.0))

    # 1. 在第一組找對 strike
    valid_strikes = [s for s in available_strikes if abs(s % step) < 1e-4 or abs((s % step) - step) < 1e-4]
    if valid_strikes:
        base_atm_strike = min(valid_strikes, key=lambda s: abs(s - ref_price))
    else:
        base_atm_strike = round(ref_price / step) * step

    print(f"-> 基準平價履約價 (Base ATM): {base_atm_strike} (標的現價: {ref_price:.2f}, 單位階梯: {step} 點)")

    # 確保 price_jump 符合該指數履約價階梯
    if price_jump is None or float(price_jump) <= 0:
        price_jump = DEFAULT_PRICE_JUMP_MAP.get(underlying.symbol, step)
    else:
        price_jump = float(price_jump)

    if abs(price_jump % step) > 1e-4:
        aligned_jump = max(step, round(price_jump / step) * step)
        print(f"-> [提示] price_jump ({price_jump}) 非 {step} 點整數倍，自動對齊為: {aligned_jump}")
        price_jump = aligned_jump

    # 2. 5 組偏置設定
    set_definitions = [
        {
            'label': 'ATM 基準組',
            'short_tag': 'ATM',
            'center_strike': base_atm_strike,
            'offset': 0.0,
        },
    ]

    butterfly_sets = []

    for s_def in set_definitions:
        label = s_def['label']
        short_tag = s_def['short_tag']
        center_strike = s_def['center_strike']

        if is_atm_only:
            call_wing_strike = None
            put_wing_strike = None
            print(f"-> [{label}] 履約價架構: ATM 雙腿 Call & Put @ {center_strike} ({center_action_desc})")

            c_center = Option(underlying.symbol, closest_expiry, center_strike, 'C', opt_exchange, tradingClass=chosen_class)
            p_center = Option(underlying.symbol, closest_expiry, center_strike, 'P', opt_exchange, tradingClass=chosen_class)
            c_wing = None
            p_wing = None

            qualified = ib.qualifyContracts(c_center, p_center)
            if len(qualified) < 2:
                print(f"-> ❌ [{label}] 雙腿期權合約無法全數取得 IBKR 資格確認。")
                continue

            tickers = ib.reqTickers(c_center, p_center)
            ib.sleep(1.5)

            t_map = {t.contract.conId: t for t in tickers}
            tc_center = t_map.get(c_center.conId)
            tp_center = t_map.get(p_center.conId)
            tc_wing = None
            tp_wing = None
        else:
            target_upper = center_strike + wing_width_call
            target_lower = center_strike - wing_width_put

            upper_candidates = [s for s in valid_strikes if s > center_strike] if valid_strikes else []
            call_wing_strike = min(upper_candidates, key=lambda s: abs(s - target_upper)) if upper_candidates else target_upper

            lower_candidates = [s for s in valid_strikes if s < center_strike] if valid_strikes else []
            put_wing_strike = min(lower_candidates, key=lambda s: abs(s - target_lower)) if lower_candidates else target_lower

            print(
                f"-> [{label}] 鐵蝶式履約價架構: "
                f"中心 {center_action_desc} C&P @ {center_strike} | "
                f"上翼 {wing_action_desc} C @ {call_wing_strike} (+{wing_width_call:g}) | "
                f"下翼 {wing_action_desc} P @ {put_wing_strike} (-{wing_width_put:g})"
            )

            c_center = Option(underlying.symbol, closest_expiry, center_strike, 'C', opt_exchange, tradingClass=chosen_class)
            p_center = Option(underlying.symbol, closest_expiry, center_strike, 'P', opt_exchange, tradingClass=chosen_class)
            c_wing = Option(underlying.symbol, closest_expiry, call_wing_strike, 'C', opt_exchange, tradingClass=chosen_class)
            p_wing = Option(underlying.symbol, closest_expiry, put_wing_strike, 'P', opt_exchange, tradingClass=chosen_class)

            qualified = ib.qualifyContracts(c_center, p_center, c_wing, p_wing)
            if len(qualified) < 4:
                print(f"-> ❌ [{label}] 4 腿期權合約無法全數取得 IBKR 資格確認 (成功數: {len(qualified)}/4)。")
                continue

            tickers = ib.reqTickers(c_center, p_center, c_wing, p_wing)
            ib.sleep(1.5)

            t_map = {t.contract.conId: t for t in tickers}
            tc_center = t_map.get(c_center.conId)
            tp_center = t_map.get(p_center.conId)
            tc_wing = t_map.get(c_wing.conId)
            tp_wing = t_map.get(p_wing.conId)

        def get_greeks(t):
            if not t:
                return 0.0, 0.0
            mg = getattr(t, 'modelGreeks', None)
            if mg:
                return getattr(mg, 'delta', 0.0) or 0.0, getattr(mg, 'theta', 0.0) or 0.0
            return 0.0, 0.0

        d_cc, th_cc = get_greeks(tc_center)
        d_pc, th_pc = get_greeks(tp_center)

        if is_atm_only:
            total_delta = (d_cc + d_pc) if is_long else -(d_cc + d_pc)
            total_theta = (th_cc + th_pc) if is_long else -(th_cc + th_pc)
        else:
            d_cw, th_cw = get_greeks(tc_wing)
            d_pw, th_pw = get_greeks(tp_wing)
            if is_long:
                total_delta = (d_cc + d_pc) - (d_cw + d_pw)
                total_theta = (th_cc + th_pc) - (th_cw + th_pw)
            else:
                # 雙賣 ATM，雙買 Wings
                total_delta = -(d_cc + d_pc) + (d_cw + d_pw)
                total_theta = -(th_cc + th_pc) + (th_cw + th_pw)

        def fmt_q(t, label_text, strike, right, action_text):
            if not t:
                return f"    • {label_text} {strike}{right} [{action_text}]: 無即時報價"
            bid = f"{t.bid:.2f}" if is_valid_price(t.bid) else "-"
            ask = f"{t.ask:.2f}" if is_valid_price(t.ask) else "-"
            last = f"{t.last:.2f}" if is_valid_price(t.last) else "-"
            mg = getattr(t, 'modelGreeks', None)
            d = f"{mg.delta:+.3f}" if (mg and mg.delta is not None) else "-"
            th = f"{mg.theta:.2f}" if (mg and mg.theta is not None) else "-"
            iv = f"{mg.impliedVol*100:.1f}%" if (mg and mg.impliedVol is not None) else "-"
            return f"    • {label_text} {strike}{right} [{action_text}]: Bid={bid} | Ask={ask} | Last={last} | Delta={d} | Theta={th} | IV={iv}"

        if is_atm_only:
            legs_quote_str = "\n".join([
                fmt_q(tc_center, f"Call ({center_action_desc})", center_strike, "C", center_action_desc),
                fmt_q(tp_center, f"Put  ({center_action_desc})", center_strike, "P", center_action_desc),
            ])
        else:
            legs_quote_str = "\n".join([
                fmt_q(tc_center, f"中心 Call ({center_action_desc})", center_strike, "C", center_action_desc),
                fmt_q(tp_center, f"中心 Put  ({center_action_desc})", center_strike, "P", center_action_desc),
                fmt_q(tc_wing, f"上翼 Call ({wing_action_desc})", call_wing_strike, "C", wing_action_desc),
                fmt_q(tp_wing, f"下翼 Put  ({wing_action_desc})", put_wing_strike, "P", wing_action_desc),
            ])

        butterfly_sets.append({
            'symbol': underlying.symbol,
            'exchange': opt_exchange,
            'underlying_price': ref_price,
            'set_label': label,
            'short_tag': short_tag,
            'offset': s_def['offset'],
            'c_center': c_center,
            'p_center': p_center,
            'c_wing': c_wing,
            'p_wing': p_wing,
            'center_strike': center_strike,
            'call_wing_strike': call_wing_strike,
            'put_wing_strike': put_wing_strike,
            'wing_width_call': wing_width_call,
            'wing_width_put': wing_width_put,
            'is_atm_only': is_atm_only,
            'bid_up': bid_up,
            'tp': tp,
            'iron': iron,
            'is_long': is_long,
            'strategy_name': strategy_name,
            'center_action_desc': center_action_desc,
            'wing_action_desc': wing_action_desc,
            'legs_quote_str': legs_quote_str,
            'dte': current_dte,
            'expiry': closest_expiry,
            'total_delta': total_delta,
            'total_theta': total_theta,
            'tc_center': tc_center,
            'tp_center': tp_center,
            'tc_wing': tc_wing,
            'tp_wing': tp_wing,
        })

    return butterfly_sets


# ==============================================================================
# 5. 建立並送出 0DTE 組合單 + 括號停利單 (Bracket Take-Profit Order)
# ==============================================================================
def execute_dte0_butterfly_with_tp(legs):
    symbol = legs['symbol']
    exchange = legs['exchange']
    iron = legs.get('iron', 'short')
    is_long = legs.get('is_long', False)
    is_atm_only = legs.get('is_atm_only', False)
    strategy_name = legs.get('strategy_name', 'Short Iron Butterfly')
    set_label = legs.get('set_label', 'ATM 基準組')
    short_tag = legs.get('short_tag', 'ATM')
    center_desc = legs.get('center_action_desc', '賣出 (SELL)')
    wing_desc = legs.get('wing_action_desc', '買入 (BUY)')
    legs_quote_str = legs.get('legs_quote_str', '')

    combo_contract = Contract(symbol=symbol, secType='BAG', currency='USD', exchange=exchange)
    if is_atm_only:
        combo_contract.comboLegs = [
            ComboLeg(conId=legs['c_center'].conId, ratio=1, action='BUY', exchange=exchange),
            ComboLeg(conId=legs['p_center'].conId, ratio=1, action='BUY', exchange=exchange),
        ]
    else:
        # 4 腿 (Iron Butterfly 蝶式組合單)：
        # 中心 ATM 2 腿定義為 BUY，價外上下翼定義為 SELL
        # 若 iron == 'long': 送出 BUY 訂單 (BUY*BUY=買中心ATM, BUY*SELL=賣雙翼)
        # 若 iron == 'short': 送出 SELL 訂單 (SELL*BUY=賣中心ATM, SELL*SELL=買雙翼)
        # 組合單價格為正 (Straddle - Wings > 0)，完全符合 CBOE/SMART 正限價規範
        combo_contract.comboLegs = [
            ComboLeg(conId=legs['c_center'].conId, ratio=1, action='BUY', exchange=exchange),
            ComboLeg(conId=legs['p_center'].conId, ratio=1, action='BUY', exchange=exchange),
            ComboLeg(conId=legs['c_wing'].conId, ratio=1, action='SELL', exchange=exchange),
            ComboLeg(conId=legs['p_wing'].conId, ratio=1, action='SELL', exchange=exchange),
        ]

    ticker = ib.reqMktData(combo_contract, '', False, False)
    ib.sleep(1.5)

    combo_bid = ticker.bid if (ticker and ticker.bid is not None and ticker.bid > 0) else 0.0

    c_center_bid = (legs['tc_center'].bid or 0.0) if legs.get('tc_center') else 0.0
    p_center_bid = (legs['tp_center'].bid or 0.0) if legs.get('tp_center') else 0.0

    synthetic_bid = 0.0
    if is_atm_only:
        if c_center_bid > 0 and p_center_bid > 0:
            synthetic_bid = c_center_bid + p_center_bid
    else:
        c_wing_ask = (legs['tc_wing'].ask or 0.0) if legs.get('tc_wing') else 0.0
        p_wing_ask = (legs['tp_wing'].ask or 0.0) if legs.get('tp_wing') else 0.0
        if c_center_bid > 0 and p_center_bid > 0:
            synthetic_bid = (c_center_bid + p_center_bid) - (c_wing_ask + p_wing_ask)

    raw_bid = combo_bid if combo_bid > 0 else synthetic_bid
    ref_leg = legs.get('c_center')
    bid_up = legs.get('bid_up')
    tp = legs.get('tp')

    # 1. 計算母單限價 (bid_up)
    if bid_up is not None and float(bid_up) > 0:
        bid_up_val = float(bid_up)
    else:
        bid_up_val = DEFAULT_BID_UP_MAP.get(symbol, 9.0)

    limit_price = round_to_valid_tick(symbol, bid_up_val, ref_leg)
    limit_price = round(limit_price, 4)

    # 2. 計算括號停利子單限價 (TP)
    if tp is not None and float(tp) > 0:
        tp_val = float(tp)
    else:
        tp_val = DEFAULT_TP_MAP.get(symbol, 4.0)

    tp_limit_price = round_to_valid_tick(symbol, tp_val, ref_leg)
    tp_limit_price = round(tp_limit_price, 4)

    order_action = 'BUY' if is_long else 'SELL'
    action_chinese = '限價買入' if is_long else '限價賣出'

    tp_action = 'SELL' if is_long else 'BUY'
    tp_action_chinese = '限價賣出 (停利平倉)' if is_long else '限價買回 (停利平倉)'

    print(f"-> 🎯 [{set_label}] 母單委託限價: ${limit_price} (bid_up: ${bid_up_val:.2f}) | 括號停利限價: ${tp_limit_price} (TP: ${tp_val:.2f}) | 即時市場買盤參考: ${raw_bid:.2f}")

    # 建立母單 (Parent Limit Order)
    parent_order = LimitOrder(order_action, TRADE_QTY, limit_price)
    parent_order.tif = 'DAY'

    # 建立括號停利子單 (Child Limit Order)
    tp_order = LimitOrder(tp_action, TRADE_QTY, tp_limit_price)
    tp_order.tif = 'DAY'

    # 綁定 Parent-Child 關聯 (Bracket 結構)
    try:
        parent_order.orderId = ib.client.getReqId()
    except Exception:
        pass
    parent_order.transmit = False

    if getattr(parent_order, 'orderId', 0):
        tp_order.parentId = parent_order.orderId
    tp_order.transmit = True

    env_cfg = load_env_config()
    send_live = str(env_cfg.get('OP_SEND_WEBHOOK', 'false')).strip().lower() in ('true', '1')
    if "--dry-run" in sys.argv:
        send_live = False
    target_acct = env_cfg.get('IB_TARGET_ACCOUNT', '').strip()
    if target_acct:
        parent_order.account = target_acct
        tp_order.account = target_acct

    if is_atm_only:
        structure_desc = f"履約價: {legs['center_strike']} [{short_tag}]"
    else:
        structure_desc = (
            f"中心履約價: {legs['center_strike']} [{short_tag}] ({center_desc} C & P)\n"
            f"  保護翼履約價: 上翼 {legs['call_wing_strike']}C ({wing_desc}), 下翼 {legs['put_wing_strike']}P ({wing_desc})"
        )

    summary_str = (
        f"0DTE 指數組合單 [{set_label}] ({strategy_name}):\n"
        f"  標的代號: {symbol} (市價: {legs['underlying_price']:.2f})\n"
        f"  策略方向 (iron): {iron.upper()} (中心: {center_desc}" + (f" / 外翼: {wing_desc})" if not is_atm_only else ")") + "\n"
        f"  {structure_desc}\n"
        f"  到期日: {legs['expiry']} (DTE: {legs['dte']} 天)\n"
        f"  組合即時報價與 Greeks:\n"
        f"{legs_quote_str}\n"
        f"  下單方式: BID_UP LIMIT + 括號停利單 (Bracket Take-Profit)\n"
        f"  母單委託: {action_chinese} {order_action} {TRADE_QTY} 口 @ ${limit_price} (bid_up: ${bid_up_val:.2f})\n"
        f"  子單停利: {tp_action_chinese} {tp_action} {TRADE_QTY} 口 @ ${tp_limit_price} (TP 設定: ${tp_val:.2f})\n"
        f"  淨 Delta: {legs['total_delta']:+.3f} | 淨 Theta: {legs['total_theta']:.2f}"
    )

    res = {
        'symbol': symbol,
        'set_label': set_label,
        'short_tag': short_tag,
        'center_strike': legs['center_strike'],
        'call_wing_strike': legs.get('call_wing_strike'),
        'put_wing_strike': legs.get('put_wing_strike'),
        'wing_width_call': legs.get('wing_width_call'),
        'wing_width_put': legs.get('wing_width_put'),
        'is_atm_only': is_atm_only,
        'order_action': order_action,
        'action_chinese': action_chinese,
        'quantity': TRADE_QTY,
        'limit_price': limit_price,
        'bid_up_val': bid_up_val,
        'tp_action': tp_action,
        'tp_limit_price': tp_limit_price,
        'tp_val': tp_val,
        'raw_bid': raw_bid,
        'total_delta': legs['total_delta'],
        'total_theta': legs['total_theta'],
        'status': 'DryRun',
        'tp_status': 'DryRun',
        'order_id': None,
        'tp_order_id': None,
        'error_code': None,
        'error_msg': None,
        'avg_fill_price': 0.0,
        'send_live': send_live,
    }

    if send_live:
        RECENT_IB_ERRORS.clear()
        parent_trade = ib.placeOrder(combo_contract, parent_order)
        # 確保 tp_order 之 parentId 確實對齊母單
        if not getattr(tp_order, 'parentId', 0):
            tp_order.parentId = parent_trade.order.orderId
        tp_trade = ib.placeOrder(combo_contract, tp_order)

        res['order_id'] = parent_trade.order.orderId
        res['tp_order_id'] = tp_trade.order.orderId
        print(f"=== [已送出母單與括號停利單至 IBKR] ===\n{summary_str}")

        for _ in range(5):
            ib.sleep(1)
            if parent_trade.orderStatus.status not in ('PendingSubmit', ''):
                break

        res['status'] = parent_trade.orderStatus.status
        res['tp_status'] = tp_trade.orderStatus.status

        order_errors = [e for e in RECENT_IB_ERRORS if e[0] in (parent_trade.order.orderId, tp_trade.order.orderId) or e[1] in (110, 201, 103, 321, 200)]
        if order_errors:
            err_code, err_msg = order_errors[-1][1], order_errors[-1][2]
            # 自動處理 Error 201: Cannot have open orders on both sides of the same US Option contract
            if err_code == 201 and "Cannot have open orders on both sides" in err_msg:
                print(f"⚠️ [觸發 Error 201 自動防衝突機制] 偵測到反向舊單衝突，正在自動撤銷衝突舊單並重試...")
                cancel_conflicting_index_orders(symbol)
                RECENT_IB_ERRORS.clear()
                parent_order.orderId = ib.client.getReqId()
                parent_order.transmit = False
                tp_order.orderId = ib.client.getReqId()
                tp_order.parentId = parent_order.orderId
                tp_order.transmit = True
                parent_trade = ib.placeOrder(combo_contract, parent_order)
                tp_trade = ib.placeOrder(combo_contract, tp_order)
                res['order_id'] = parent_trade.order.orderId
                res['tp_order_id'] = tp_trade.order.orderId
                for _ in range(5):
                    ib.sleep(1)
                    if parent_trade.orderStatus.status not in ('PendingSubmit', ''):
                        break
                order_errors = [e for e in RECENT_IB_ERRORS if e[0] in (parent_trade.order.orderId, tp_trade.order.orderId) or e[1] in (110, 201, 103, 321, 200)]

        if order_errors:
            err_code, err_msg = order_errors[-1][1], order_errors[-1][2]
            print(f"❌ [下單被拒絕] IBKR 回報錯誤 {err_code}: {err_msg}")
            res['status'] = 'Rejected'
            res['error_code'] = err_code
            res['error_msg'] = err_msg
        elif parent_trade.orderStatus.status in ('PreSubmitted', 'Submitted'):
            print(f"✅ [委託成功確認] IBKR 母單已成功接收排入市場 (狀態: {parent_trade.orderStatus.status}, 限價: {limit_price}) | 停利子單就緒 (狀態: {tp_trade.orderStatus.status}, 限價: {tp_limit_price})")
            res['status'] = parent_trade.orderStatus.status
        else:
            print(f"ℹ️ [委託狀態] 目前母單狀態: {parent_trade.orderStatus.status} (限價: {limit_price})")
            res['status'] = parent_trade.orderStatus.status

        # 步進式動態修改限價 (Custom Walk-Up 引擎：每 3 秒讓步 $0.05，最多讓步 $0.15，逾時徹底撤單)
        is_filled = False
        if walk_up_limit_price and parent_trade.orderStatus.status not in ('Filled', 'Cancelled', 'ApiCancelled', 'Rejected'):
            print(f"⏳ [自適應步進修單] 啟動 Custom Walk-Up 引擎監控與步進調整 {symbol} 0DTE [{set_label}] 母單...")
            is_filled = walk_up_limit_price(
                ib=ib,
                contract=combo_contract,
                order=parent_order,
                current_mid=limit_price,
                max_slippage=0.15,
                step=0.05,
                step_time=3.0,
                max_steps=3,
                symbol=f"{symbol} 0DTE [{set_label}]",
                trade=parent_trade
            )
            # 等待母單最新狀態同步
            for _ in range(4):
                if is_filled and parent_trade.orderStatus.status == 'Filled':
                    break
                if not is_filled and parent_trade.orderStatus.status in ('Cancelled', 'ApiCancelled', 'Inactive'):
                    break
                ib.sleep(0.5)
        else:
            end_time = time.time() + 9
            while time.time() < end_time:
                ib.sleep(1)
                if parent_trade.orderStatus.status in ('Filled', 'Cancelled'):
                    break

        if parent_trade.orderStatus.status == 'Filled' or is_filled:
            avg_p = parent_trade.orderStatus.avgFillPrice or parent_order.lmtPrice
            print(f"=== [成交確認] {symbol} 0DTE [{set_label}] 母單已完全成交，均價: {avg_p} (停利子單已自動掛出 @ ${tp_limit_price}) ===")
            res['status'] = 'Filled'
            res['avg_fill_price'] = avg_p
        elif parent_trade.orderStatus.status in ('Cancelled', 'ApiCancelled') or (walk_up_limit_price and not is_filled):
            print(f"⚠️ [訂單取消] {symbol} 0DTE [{set_label}] 母單未成交並已撤單。")
            res['status'] = 'Cancelled'
            # 確保停利子單也同步取消
            try:
                ib.cancelOrder(tp_order)
            except Exception:
                pass
        else:
            res['status'] = parent_trade.orderStatus.status
    else:
        print(f"=== [測試模式 - 僅列印不送單] (OP_SEND_WEBHOOK=false 或 --dry-run) ===\n{summary_str}")

    ib.sleep(1.5)
    return res


# ==============================================================================
# 5.5 每個 Symbol 統一發送一次手機 LINE 彙總推播
# ==============================================================================
def send_symbol_dte0_summary(symbol, legs_sets, results):
    if not results:
        return

    env_cfg = load_env_config()
    op_send_webhook = str(env_cfg.get('OP_SEND_WEBHOOK', 'false')).strip().lower() in ('true', '1')
    if '--no-line' in sys.argv:
        print(f"-> ℹ️ [{symbol}] 依指令參數 --no-line，略過 LINE 推播。")
        return

    if not op_send_webhook:
        print(f"-> ℹ️ [{symbol}] OP_SEND_WEBHOOK 未啟用，略過 LINE 推播。")
        return

    first_leg = legs_sets[0] if legs_sets else {}
    underlying_price = first_leg.get('underlying_price', 0.0)
    expiry = first_leg.get('expiry', '')
    dte = first_leg.get('dte', 0)
    strategy_name = first_leg.get('strategy_name', '0DTE 組合部位')
    center_action_desc = first_leg.get('center_action_desc', '賣出 (SELL)')
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    send_live = any(r.get('send_live', False) for r in results)
    mode_text = '正式送單 (Live)' if send_live else '模擬測試 (Dry-Run)'

    total_delta = sum(r.get('total_delta', 0.0) for r in results)
    total_theta = sum(r.get('total_theta', 0.0) for r in results)

    is_atm_all = all(r.get('is_atm_only', False) for r in results)
    leg_count = 2 if is_atm_all else 4

    lines = [
        f"🎯【0DTE 指數組合建倉總結 (含括號停利) - {symbol}】",
        f"🕒 時間：{now_str}",
        f"⚙️ 模式：{mode_text}",
        f"📈 現價：{underlying_price:.2f} | 到期：{expiry} (DTE: {dte}天)",
        f"🧭 策略：{strategy_name} ({center_action_desc})",
        f"📋 五組部位委託明細 (母單限價 / 括號停利 TP)：",
    ]

    for idx, r in enumerate(results, 1):
        status = r.get('status', 'Unknown')
        status_map = {
            'Submitted': '已排單 (Submitted)',
            'PreSubmitted': '預排單 (PreSubmitted)',
            'Filled': f"已成交 (${r.get('avg_fill_price', 0):.2f})",
            'Rejected': f"❌ 拒絕 (Err {r.get('error_code')})",
            'Cancelled': '已取消',
            'DryRun': '模擬 (Dry-Run)'
        }
        status_desc = status_map.get(status, status)
        if r.get('is_atm_only'):
            k_info = f"K={r.get('center_strike')}"
        else:
            k_info = f"K={r.get('center_strike')} (Wings: {r.get('call_wing_strike')}C/{r.get('put_wing_strike')}P)"
        tp_str = f" [🎯TP: ${r.get('tp_limit_price', 0):.2f}]" if r.get('tp_limit_price') else ""
        lines.append(
            f"  {idx}. [{r.get('short_tag')}] {k_info}: "
            f"{status_desc} | 賣單: ${r.get('limit_price', 0):.2f}{tp_str} "
            f"(Δ:{r.get('total_delta', 0):+.3f}, θ:{r.get('total_theta', 0):.2f})"
        )

    lines.extend([
        f"",
        f"📊 投組 Greeks 彙總：",
        f"  • 總淨 Delta: {total_delta:+.3f}",
        f"  • 總淨 Theta: {total_theta:.2f}",
        f"  • 總組數: {len(results)} 組 (共 {len(results)*leg_count} 腿)",
        f"  • 停利保護機制: 每組均已附帶 Attached Bracket 停利買回單"
    ])

    error_items = [r for r in results if r.get('error_msg')]
    if error_items:
        lines.append("\n⚠️ 委託異常警示：")
        for err_r in error_items:
            lines.append(f"  • [{err_r.get('short_tag')}] 代碼 {err_r.get('error_code')}: {err_r.get('error_msg')}")

    message_text = "\n".join(lines)

    payload = {
        "symbol": symbol,
        "mode": "live" if send_live else "dry_run",
        "expiry": expiry,
        "underlying_price": underlying_price,
        "total_delta": round(total_delta, 3),
        "total_theta": round(total_theta, 2),
        "orders": [
            {
                "tag": r.get('short_tag'),
                "strike": r.get('center_strike'),
                "status": r.get('status'),
                "limit_price": r.get('limit_price'),
                "tp_price": r.get('tp_limit_price'),
                "order_id": r.get('order_id'),
                "tp_order_id": r.get('tp_order_id')
            }
            for r in results
        ]
    }

    try:
        ok = send_trade_notification(symbol, message_text, payload)
        if ok:
            print(f"-> 📲 [LINE 推播成功] 已發送 {symbol} 0DTE 五組統一彙總通知至手機！")
        else:
            print(f"-> ⚠️ [LINE 推播] {symbol} 0DTE 彙總通知發送失敗。")
    except Exception as e:
        print(f"-> ❌ [LINE 推播異常] {e}")


# ==============================================================================
# 6. 主執行迴圈 (處理 SPX, NDX, RUT, DJX)
# ==============================================================================
def run_dte0_cycle():
    target_groups = {}

    # 1. 優先自 trade/.env 之 DTE0_CONFIG_JSON 讀取 SPX 設定 (不使用 HEDGE_CONFIG_JSON)
    dte0_cfg = load_dte0_config() if load_dte0_config else {}
    if "SPX" in dte0_cfg or "標普指數(SPX)" in dte0_cfg:
        spx_info = dte0_cfg.get("SPX") or dte0_cfg.get("標普指數(SPX)")
        if 'symbols' not in spx_info or not spx_info['symbols']:
            spx_info['symbols'] = ['SPX']
        target_groups["標普指數(SPX)"] = spx_info

    # 2. 如果 DTE0_CONFIG_JSON 還有其他指數群組，一併加入
    for g_name, g_info in dte0_cfg.items():
        if g_name in ("SPX", "標普指數(SPX)"):
            continue
        syms = g_info.get('symbols', [])
        if any(s in INDEX_SYMBOLS for s in syms) or any(k in g_name for k in ['NDX', 'RUT', 'DJX']):
            target_groups[g_name] = g_info

    if not target_groups:
        print("[資訊] DTE0_CONFIG_JSON (與 OP_HEDGE_CONFIG_JSON) 中未找到 SPX, NDX, RUT 或 DJX 指數群組。")
        return

    market_date = get_market_today()
    print(f"\n[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 啟動 0DTE 指數期權賣方策略掃描 (含括號停利單 TP，基準日: {market_date}，共 {len(target_groups)} 個指數群組)...")

    for g_name, g_info in target_groups.items():
        print(f"\n================ 開始處理指數群組: {g_name} ================")
        symbols = g_info.get('symbols', [])
        wing_width = g_info.get('wing_width')
        wing_width_call = g_info.get('wing_width_call', wing_width)
        wing_width_put = g_info.get('wing_width_put', wing_width)
        bid_up = g_info.get('bid_up')
        tp = g_info.get('TP') or g_info.get('tp')
        iron = str(g_info.get('iron', 'short')).strip().lower()

        # 讀取 price_jump
        raw_jump = g_info.get('price_jump') or env_config.get('PRICE_JUMP') or env_config.get('price_jump')
        try:
            price_jump = float(raw_jump)
        except (ValueError, TypeError):
            price_jump = None

        underlying, opt_class, chains = resolve_index_symbols(g_name, symbols)
        if not underlying:
            print(f"[略過] 無法解析 {g_name} 的指數標的結構。")
            continue

        if price_jump is None:
            price_jump = DEFAULT_PRICE_JUMP_MAP.get(underlying.symbol, 20.0)

        current_pos = [p for p in ib.portfolio() if p.contract.symbol == underlying.symbol and p.contract.secType in ['OPT', 'IND']]
        is_dry_run = '--dry-run' in sys.argv
        is_force = '--force' in sys.argv
        if not current_pos or is_dry_run or is_force:
            if current_pos and (is_dry_run or is_force):
                print(f"-> ⚠️ 注意: 帳戶目前已有 {underlying.symbol} 期權部位 ({len(current_pos)} 筆)，但因指定了 {'--dry-run' if is_dry_run else '--force'}，繼續執行。")
            
            wing_desc = f"wings: +{wing_width_call}/-{wing_width_put}" if (wing_width_call or wing_width_put) else "ATM 雙腿"
            tp_desc = f"TP: ${float(tp):.2f}" if tp else "TP: 預設"
            print(f"-> 準備為 {underlying.symbol} 建立 5 組 0DTE 賣方組合部位 (方向: {iron.upper()}, {wing_desc}, bid_up: ${bid_up or '預設'}, {tp_desc}, price_jump: {price_jump})...")
            sets = get_dte0_butterfly_sets(
                underlying=underlying,
                opt_class=opt_class,
                chains=chains,
                wing_width=wing_width,
                wing_width_call=wing_width_call,
                wing_width_put=wing_width_put,
                bid_up=bid_up,
                tp=tp,
                iron=iron,
                price_jump=price_jump
            )
            if sets:
                # 若為正式送單模式，先主動清理舊有反向掛單，避免 Error 201 衝突
                env_cfg = load_env_config()
                send_live = str(env_cfg.get('OP_SEND_WEBHOOK', 'false')).strip().lower() in ('true', '1') and not is_dry_run
                if send_live:
                    cancel_conflicting_index_orders(underlying.symbol)

                results = []
                for idx, s in enumerate(sets, 1):
                    print(f"\n--- [組別 {idx}/{len(sets)}] 執行 {s['set_label']} ---")
                    r = execute_dte0_butterfly_with_tp(s)
                    results.append(r)

                # 同步將 [ATM] 組之上下翼點位寫入 trade/.env 之 DTE0_CONFIG_JSON
                if update_dte0_wings:
                    atm_set = next((s for s in sets if s.get('short_tag') == 'ATM'), sets[0] if sets else None)
                    if atm_set and atm_set.get('call_wing_strike') and atm_set.get('put_wing_strike'):
                        c_w = atm_set['call_wing_strike']
                        p_w = atm_set['put_wing_strike']
                        update_dte0_wings(
                            symbol=underlying.symbol,
                            wing_call=f"{c_w:g}",
                            wing_put=f"{p_w:g}",
                            hedge_sym="MES",
                            hedge_qty=1
                        )

                # 每個 symbol 統一傳一次手機 LINE 彙總
                send_symbol_dte0_summary(underlying.symbol, sets, results)
            else:
                print(f"-> ⚠️ 未能成功建構 {underlying.symbol} 0DTE 組合。")
        else:
            print(f"-> {underlying.symbol} 已有指數期權部位 ({len(current_pos)} 筆)，跳過重複建倉。")


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description="0DTE 指數期權 (SPX/NDX/RUT/DJX) 雙賣ATM/雙買外翼 + 括號停利單 (Iron Butterfly + Attached TP Limit) 自動建倉腳本")
    parser.add_argument("--dry-run", action="store_true", help="模擬模式（不實際送單至 IBKR）")
    parser.add_argument("--no-line", action="store_true", help="不發送 LINE 通知")
    parser.add_argument("--force", action="store_true", help="強制執行下單（忽略已有部位檢查）")
    args = parser.parse_args()

    if args.dry_run:
        print("💡 [DRY-RUN 模擬模式] 不實際向 IBKR 送單。")

    try:
        connect_ib()
        run_dte0_cycle()
    except Exception as e:
        print(f"\n[錯誤] 執行異常: {e}")
    finally:
        if ib.isConnected():
            try:
                ib.disconnect()
            except Exception:
                pass

    if not args.dry_run and sys.stdin and hasattr(sys.stdin, 'isatty') and sys.stdin.isatty():
        try:
            time.sleep(60 * 60 * 20)
        except KeyboardInterrupt:
            pass

    print("完成時間:", datetime.datetime.now().strftime('%H:%M:%S'))
