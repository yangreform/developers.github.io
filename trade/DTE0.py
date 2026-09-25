import os
import json
import datetime
import time
import math
try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo
import requests
from ib_insync import *

from notifier import send_push_message, send_trade_notification
import sys

# 確保 Windows 主控台與子行程正確輸出 UTF-8 字符，避免 UnicodeEncodeError
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
        sys.stderr.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
    except Exception:
        pass

if "--no-line" in sys.argv:
    send_push_message = lambda *a, **kw: None
    send_trade_notification = lambda *a, **kw: None

# ==============================================================================
# 0. 讀取 .env 設定 (支援每次呼叫即時動態讀取)
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
        # 備援換算：UTC - 4 小時 (美東日光節約時區)
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
CLIENT_ID = int(env_config.get('IB_CLIENT_ID', 100)) + 7

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
    'SPX': 4.0,
    'NDX': 16.0,
    'RUT': 3.0,
    'DJX': 1.0,
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
        for offset in range(15):
            cur_id = base_id + offset
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
            raise ConnectionError(f"無法連接至 IBKR ({IB_HOST}:{IB_PORT})，已嘗試 clientId {base_id} 至 {base_id+14}")

        try:
            ib.reqMarketDataType(3)
        except Exception:
            pass
        actual_id = ib.client.clientId if ib.client else CLIENT_ID
        print(f"=== [DTE0] 成功連接至 IBKR ({IB_HOST}:{IB_PORT}) | ClientId={actual_id} | 指數 0DTE 專用系統 ===")


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
# ==============================================================================
# 4. 獲取 0DTE 雙腿組合合約 (SPX / NDX / RUT - Two Legs / Straddle)
# ==============================================================================
def get_dte0_butterfly_sets(underlying, opt_class, chains, wing_width=None, bid_up=None, iron='long', price_jump=30.0):
    iron = str(iron).strip().lower() if iron else 'long'
    is_long = (iron != 'short')
    center_action_desc = "買入 (BUY)" if is_long else "賣出 (SELL)"
    strategy_name = "Long Straddle (0DTE 雙腿買方)" if is_long else "Short Straddle (0DTE 雙腿賣方)"

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

    # 1. 在第一組找對 strike：NDX 是 25 點為單位，篩選符合 step 的有效 strike
    valid_strikes = [s for s in available_strikes if abs(s % step) < 1e-4 or abs((s % step) - step) < 1e-4]
    if valid_strikes:
        base_atm_strike = min(valid_strikes, key=lambda s: abs(s - ref_price))
    else:
        base_atm_strike = round(ref_price / step) * step

    print(f"-> 基準平價履約價 (Base ATM): {base_atm_strike} (標的現價: {ref_price:.2f}, 單位階梯: {step} 點)")

    # 確保 price_jump 符合該指數履約價階梯 (NDX 為 25 點整數倍)
    if price_jump is None or float(price_jump) <= 0:
        price_jump = DEFAULT_PRICE_JUMP_MAP.get(underlying.symbol, step)
    else:
        price_jump = float(price_jump)

    if abs(price_jump % step) > 1e-4:
        aligned_jump = max(step, round(price_jump / step) * step)
        print(f"-> [提示] price_jump ({price_jump}) 非 {step} 點整數倍，自動對齊為: {aligned_jump}")
        price_jump = aligned_jump

    # 2. 後面四組不用找了：直接以第一組 base_atm_strike 依 price_jump 數學推算，不再至鏈條中搜尋
    set_definitions = [
        {
            'label': 'ATM 基準組',
            'short_tag': 'ATM',
            'center_strike': base_atm_strike,
            'offset': 0.0,
        },
        {
            'label': f'ATM+{price_jump:g} 上方偏置組',
            'short_tag': f'ATM+{price_jump:g}',
            'center_strike': base_atm_strike + price_jump,
            'offset': price_jump,
        },
        {
            'label': f'ATM-{price_jump:g} 下方偏置組',
            'short_tag': f'ATM-{price_jump:g}',
            'center_strike': base_atm_strike - price_jump,
            'offset': -price_jump,
        },
        {
            'label': f'ATM+2*{price_jump:g} 上上方偏置組',
            'short_tag': f'ATM+2*{price_jump:g}',
            'center_strike': base_atm_strike + 2.0 * price_jump,
            'offset': 2.0 * price_jump,
        },
        {
            'label': f'ATM-2*{price_jump:g} 下下方偏置組',
            'short_tag': f'ATM-2*{price_jump:g}',
            'center_strike': base_atm_strike - 2.0 * price_jump,
            'offset': -2.0 * price_jump,
        },
    ]

    butterfly_sets = []

    for s_def in set_definitions:
        label = s_def['label']
        short_tag = s_def['short_tag']
        center_strike = s_def['center_strike']

        print(f"-> [{label}] 履約價架構: 雙腿 Call & Put @ {center_strike} ({center_action_desc})")

        c_center = Option(underlying.symbol, closest_expiry, center_strike, 'C', opt_exchange, tradingClass=chosen_class)
        p_center = Option(underlying.symbol, closest_expiry, center_strike, 'P', opt_exchange, tradingClass=chosen_class)

        qualified = ib.qualifyContracts(c_center, p_center)
        if len(qualified) < 2:
            print(f"-> ❌ [{label}] 雙腿期權合約無法全數取得 IBKR 資格確認。")
            continue

        tickers = ib.reqTickers(c_center, p_center)
        ib.sleep(1.5)

        t_map = {t.contract.conId: t for t in tickers}
        tc_center = t_map.get(c_center.conId)
        tp_center = t_map.get(p_center.conId)

        def get_greeks(t):
            if not t:
                return 0.0, 0.0
            mg = getattr(t, 'modelGreeks', None)
            if mg:
                return getattr(mg, 'delta', 0.0) or 0.0, getattr(mg, 'theta', 0.0) or 0.0
            return 0.0, 0.0

        d_cc, th_cc = get_greeks(tc_center)
        d_pc, th_pc = get_greeks(tp_center)

        total_delta = (d_cc + d_pc) if is_long else -(d_cc + d_pc)
        total_theta = (th_cc + th_pc) if is_long else -(th_cc + th_pc)

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

        legs_quote_str = "\n".join([
            fmt_q(tc_center, f"Call ({center_action_desc})", center_strike, "C", center_action_desc),
            fmt_q(tp_center, f"Put  ({center_action_desc})", center_strike, "P", center_action_desc),
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
            'center_strike': center_strike,
            'bid_up': bid_up,
            'iron': iron,
            'is_long': is_long,
            'strategy_name': strategy_name,
            'center_action_desc': center_action_desc,
            'legs_quote_str': legs_quote_str,
            'dte': current_dte,
            'expiry': closest_expiry,
            'total_delta': total_delta,
            'total_theta': total_theta,
            'tc_center': tc_center,
            'tp_center': tp_center,
        })

    return butterfly_sets


# ==============================================================================
# 5. 建立並送出 0DTE 雙腿組合單 (Two Legs，直接採用 bid_up 下單 LIMIT)
# ==============================================================================
def execute_dte0_butterfly(legs):
    symbol = legs['symbol']
    exchange = legs['exchange']
    iron = legs.get('iron', 'long')
    is_long = legs.get('is_long', True)
    strategy_name = legs.get('strategy_name', 'Long Straddle')
    set_label = legs.get('set_label', 'ATM 基準組')
    short_tag = legs.get('short_tag', 'ATM')
    center_desc = legs.get('center_action_desc', '買入 (BUY)')
    legs_quote_str = legs.get('legs_quote_str', '')

    combo_contract = Contract(symbol=symbol, secType='BAG', currency='USD', exchange=exchange)
    combo_contract.comboLegs = [
        ComboLeg(conId=legs['c_center'].conId, ratio=1, action='BUY', exchange=exchange),
        ComboLeg(conId=legs['p_center'].conId, ratio=1, action='BUY', exchange=exchange),
    ]

    ticker = ib.reqMktData(combo_contract, '', False, False)
    ib.sleep(1.5)

    combo_bid = ticker.bid if (ticker and ticker.bid is not None and ticker.bid > 0) else 0.0

    c_center_bid = (legs['tc_center'].bid or 0.0) if legs.get('tc_center') else 0.0
    p_center_bid = (legs['tp_center'].bid or 0.0) if legs.get('tp_center') else 0.0

    synthetic_bid = 0.0
    if c_center_bid > 0 and p_center_bid > 0:
        synthetic_bid = c_center_bid + p_center_bid

    raw_bid = combo_bid if combo_bid > 0 else synthetic_bid
    ref_leg = legs.get('c_center')
    bid_up = legs.get('bid_up')

    # 每一組都直接用 bid_up 下單 LIMIT (若未設定則依標的預設值)
    if bid_up is not None and float(bid_up) > 0:
        bid_up_val = float(bid_up)
    else:
        bid_up_val = DEFAULT_BID_UP_MAP.get(symbol, 4.0)

    limit_price = round_to_valid_tick(symbol, bid_up_val, ref_leg)
    limit_price = round(limit_price, 4)
    print(f"-> 🎯 [{set_label}] 直接採用 bid_up 委託限價: ${limit_price} (設定值: ${bid_up_val:.2f} | 即時市場買盤參考: ${raw_bid:.2f})")

    order_action = 'BUY' if is_long else 'SELL'
    action_chinese = '限價買入' if is_long else '限價賣出'

    order = LimitOrder(order_action, TRADE_QTY, limit_price)
    order.tif = 'DAY'

    env_cfg = load_env_config()
    send_live = str(env_cfg.get('OP_SEND_WEBHOOK', 'false')).strip().lower() in ('true', '1')
    if "--dry-run" in sys.argv:
        send_live = False
    target_acct = env_cfg.get('IB_TARGET_ACCOUNT', '').strip()
    if target_acct:
        order.account = target_acct

    summary_str = (
        f"0DTE 指數雙腿組合單 [{set_label}] ({strategy_name}):\n"
        f"  標的代號: {symbol} (市價: {legs['underlying_price']:.2f})\n"
        f"  策略方向 (iron): {iron.upper()} (雙腿: {center_desc})\n"
        f"  履約價: {legs['center_strike']} [{short_tag}]\n"
        f"  到期日: {legs['expiry']} (DTE: {legs['dte']} 天)\n"
        f"  雙腿即時報價與 Greeks:\n"
        f"{legs_quote_str}\n"
        f"  下單方式: BID_UP LIMIT (直接以 bid_up 限價單掛單)\n"
        f"  委託動作: {action_chinese} {order_action} {TRADE_QTY} 口 @ ${limit_price} (bid_up: ${bid_up_val:.2f})\n"
        f"  淨 Delta: {legs['total_delta']:+.3f} | 淨 Theta: {legs['total_theta']:.2f}"
    )

    res = {
        'symbol': symbol,
        'set_label': set_label,
        'short_tag': short_tag,
        'center_strike': legs['center_strike'],
        'order_action': order_action,
        'action_chinese': action_chinese,
        'quantity': TRADE_QTY,
        'limit_price': limit_price,
        'bid_up_val': bid_up_val,
        'raw_bid': raw_bid,
        'total_delta': legs['total_delta'],
        'total_theta': legs['total_theta'],
        'status': 'DryRun',
        'order_id': None,
        'error_code': None,
        'error_msg': None,
        'avg_fill_price': 0.0,
        'send_live': send_live,
    }

    if send_live:
        RECENT_IB_ERRORS.clear()
        trade = ib.placeOrder(combo_contract, order)
        res['order_id'] = trade.order.orderId
        print(f"=== [已送出委託單至 IBKR] ===\n{summary_str}")

        for _ in range(5):
            ib.sleep(1)
            if trade.orderStatus.status not in ('PendingSubmit', ''):
                break

        res['status'] = trade.orderStatus.status

        order_errors = [e for e in RECENT_IB_ERRORS if e[0] == trade.order.orderId or e[1] in (110, 201, 103, 321, 200)]
        if order_errors:
            err_code, err_msg = order_errors[-1][1], order_errors[-1][2]
            print(f"❌ [下單被拒絕] IBKR 回報錯誤 {err_code}: {err_msg}")
            res['status'] = 'Rejected'
            res['error_code'] = err_code
            res['error_msg'] = err_msg
        elif trade.orderStatus.status in ('PreSubmitted', 'Submitted'):
            print(f"✅ [委託成功確認] IBKR 已成功接收並排入市場 (狀態: {trade.orderStatus.status}, 限價: {limit_price})")
            res['status'] = trade.orderStatus.status
        else:
            print(f"ℹ️ [委託狀態] 目前狀態: {trade.orderStatus.status} (限價: {limit_price})")
            res['status'] = trade.orderStatus.status

        end_time = time.time() + 55
        while time.time() < end_time:
            ib.sleep(1)
            if trade.orderStatus.status in ('Filled', 'Cancelled'):
                break

        if trade.orderStatus.status == 'Filled':
            print(f"=== [成交確認] {symbol} 0DTE [{set_label}] 已完全成交，均價: {trade.orderStatus.avgFillPrice} ===")
            res['status'] = 'Filled'
            res['avg_fill_price'] = trade.orderStatus.avgFillPrice
        elif trade.orderStatus.status == 'Cancelled':
            res['status'] = 'Cancelled'
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
    strategy_name = first_leg.get('strategy_name', '0DTE 雙腿部位')
    center_action_desc = first_leg.get('center_action_desc', '買入 (BUY)')
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    send_live = any(r.get('send_live', False) for r in results)
    mode_text = '正式送單 (Live)' if send_live else '模擬測試 (Dry-Run)'

    total_delta = sum(r.get('total_delta', 0.0) for r in results)
    total_theta = sum(r.get('total_theta', 0.0) for r in results)

    lines = [
        f"🎯【0DTE 指數雙腿組合建倉總結 - {symbol}】",
        f"🕒 時間：{now_str}",
        f"⚙️ 模式：{mode_text}",
        f"📈 現價：{underlying_price:.2f} | 到期：{expiry} (DTE: {dte}天)",
        f"🧭 策略：{strategy_name} ({center_action_desc})",
        f"📋 五組部位委託明細：",
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
        lines.append(
            f"  {idx}. [{r.get('short_tag')}] K={r.get('center_strike')}: "
            f"{status_desc} | 限價: ${r.get('limit_price', 0):.2f} "
            f"(Δ:{r.get('total_delta', 0):+.3f}, θ:{r.get('total_theta', 0):.2f})"
        )

    lines.extend([
        f"",
        f"📊 投組 Greeks 彙總：",
        f"  • 總淨 Delta: {total_delta:+.3f}",
        f"  • 總淨 Theta: {total_theta:.2f}",
        f"  • 總組數: {len(results)} 組 (共 {len(results)*2} 腿)"
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
                "order_id": r.get('order_id')
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
    hedge_config = load_op_hedge_config()
    if not hedge_config:
        print("[資訊] trade/.env 中的 OP_HEDGE_CONFIG_JSON 為空，跳過。")
        return

    # 篩選包含 SPX, NDX, RUT 或 DJX 的群組
    target_groups = {}
    for g_name, g_info in hedge_config.items():
        syms = g_info.get('symbols', [])
        if any(s in INDEX_SYMBOLS or any(idx_s in s for idx_s in INDEX_SYMBOLS) for s in syms) or any(k in g_name for k in ['SPX', 'NDX', 'RUT', 'DJX', '羅素', 'Russell', '道瓊', 'Dow']):
            target_groups[g_name] = g_info

    if not target_groups:
        print("[資訊] OP_HEDGE_CONFIG_JSON 中未找到 SPX, NDX, RUT 或 DJX 指數群組。")
        return

    market_date = get_market_today()
    print(f"\n[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 啟動 0DTE 指數期權策略掃描 (基準日: {market_date}，共 {len(target_groups)} 個指數群組)...")

    for g_name, g_info in target_groups.items():
        print(f"\n================ 開始處理指數群組: {g_name} ================")
        symbols = g_info.get('symbols', [])
        wing_width = g_info.get('wing_width')
        bid_up = g_info.get('bid_up')
        iron = str(g_info.get('iron', 'long')).strip().lower()

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
            print(f"-> 準備為 {underlying.symbol} 建立 5 組 0DTE 雙腿部位 (方向: {iron.upper()}, bid_up: {bid_up or '預設'}, price_jump: {price_jump})...")
            sets = get_dte0_butterfly_sets(
                underlying=underlying,
                opt_class=opt_class,
                chains=chains,
                wing_width=wing_width,
                bid_up=bid_up,
                iron=iron,
                price_jump=price_jump
            )
            if sets:
                results = []
                for idx, s in enumerate(sets, 1):
                    print(f"\n--- [組別 {idx}/{len(sets)}] 執行 {s['set_label']} ---")
                    r = execute_dte0_butterfly(s)
                    results.append(r)

                # 每個 symbol 統一傳一次手機 LINE 彙總
                send_symbol_dte0_summary(underlying.symbol, sets, results)
            else:
                print(f"-> ⚠️ 未能成功建構 {underlying.symbol} 0DTE 雙腿組合。")
        else:
            print(f"-> {underlying.symbol} 已有指數期權部位 ({len(current_pos)} 筆)，跳過重複建倉。")


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description="0DTE 指數期權 (SPX/NDX/RUT/DJX) 雙腿 (Two Legs) 自動建倉腳本")
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
