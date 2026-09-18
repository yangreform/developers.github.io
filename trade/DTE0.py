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
# 1. 交易參數與指數標的配置 (專門處理 SPX, NDX)
# ==============================================================================
env_config = load_env_config()
IB_HOST = env_config.get('IB_HOST', '127.0.0.1')
IB_PORT = int(env_config.get('IB_PORT', 4001))
CLIENT_ID = int(env_config.get('IB_CLIENT_ID', 100)) + 7

TRADE_QTY = 1
INDEX_SYMBOLS = {'SPX', 'NDX'}

INDEX_SYMBOL_MAP = {
    'SPX': 'SPX', 'SPXW': 'SPX',
    'NDX': 'NDX', 'NDXP': 'NDX',
}

PREFERRED_TRADING_CLASS_MAP = {
    'SPX': 'SPXW',
    'NDX': 'NDXP',
}

WING_WIDTH_MAP = {
    'SPX': 10,
    'NDX': 40,
}

DEFAULT_BID_UP_MAP = {
    'SPX': 4.0,
    'NDX': 16.0,
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

    exchange = 'CBOE' if underlying_sym == 'SPX' else 'NASDAQ'
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
# 4. 獲取 0DTE Butterfly 四腿合約 (SPX / NDX)
# ==============================================================================
def get_dte0_butterfly_legs(underlying, opt_class, chains, wing_width=None, bid_up=None, iron='long'):
    iron = str(iron).strip().lower() if iron else 'long'
    is_long = (iron != 'short')
    center_action_desc = "買入 (BUY)" if is_long else "賣出 (SELL)"
    wing_action_desc = "賣出 (SELL)" if is_long else "買入 (BUY)"
    strategy_name = "Long Butterfly (0DTE 蝶式買方)" if is_long else "Short Butterfly (0DTE 蝶式賣方/鐵蝶)"

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
        return None

    class_candidates.sort(key=lambda x: x[3])
    chosen_class, closest_expiry, available_strikes, current_dte, multiplier = class_candidates[0]
    print(f"-> 🎯 選定合約: {underlying.symbol} (Class: {chosen_class}, 到期日: {closest_expiry}, DTE: {current_dte}天)")

    ref_price = get_underlying_price(underlying)
    if not is_valid_price(ref_price):
        print(f"-> ❌ 無法取得 {underlying.symbol} 底層指數價格，跳過。")
        return None

    print(f"-> 標的現價: {ref_price:.2f}")

    strikes = sorted(list(available_strikes))
    center_strike = min(strikes, key=lambda s: abs(s - ref_price))

    if wing_width is None or float(wing_width) <= 0:
        wing_width = WING_WIDTH_MAP.get(underlying.symbol, 10.0)
    else:
        wing_width = float(wing_width)

    target_call_wing = center_strike + wing_width
    target_put_wing = center_strike - wing_width

    call_wing_strike = min([s for s in strikes if s > center_strike], key=lambda s: abs(s - target_call_wing), default=None)
    put_wing_strike = min([s for s in strikes if s < center_strike], key=lambda s: abs(s - target_put_wing), default=None)

    if not call_wing_strike or not put_wing_strike:
        print(f"-> ❌ 履約價跨度不足，無法建立對稱上下翼 (Center: {center_strike}, Target Wings: +{wing_width}/-{wing_width})。")
        return None

    actual_call_width = call_wing_strike - center_strike
    actual_put_width = center_strike - put_wing_strike
    print(f"-> 履約價架構: 下翼 Put {put_wing_strike} (-{actual_put_width:.1f}) | 中心 ATM {center_strike} | 上翼 Call {call_wing_strike} (+{actual_call_width:.1f})")

    opt_exchange = 'SMART'
    c_center = Option(underlying.symbol, closest_expiry, center_strike, 'C', opt_exchange, tradingClass=chosen_class)
    p_center = Option(underlying.symbol, closest_expiry, center_strike, 'P', opt_exchange, tradingClass=chosen_class)
    c_wing = Option(underlying.symbol, closest_expiry, call_wing_strike, 'C', opt_exchange, tradingClass=chosen_class)
    p_wing = Option(underlying.symbol, closest_expiry, put_wing_strike, 'P', opt_exchange, tradingClass=chosen_class)

    qualified = ib.qualifyContracts(c_center, p_center, c_wing, p_wing)
    if len(qualified) < 4:
        print("-> ❌ 四腿期權合約無法全數取得 IBKR 資格確認。")
        return None

    tickers = ib.reqTickers(c_center, p_center, c_wing, p_wing)
    ib.sleep(2)

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
    d_cw, th_cw = get_greeks(tc_wing)
    d_pw, th_pw = get_greeks(tp_wing)

    if is_long:
        total_delta = (d_cc + d_pc) - (d_cw + d_pw)
        total_theta = (th_cc + th_pc) - (th_cw + th_pw)
    else:
        total_delta = (d_cw + d_pw) - (d_cc + d_pc)
        total_theta = (th_cw + th_pw) - (th_cc + th_pc)

    def fmt_q(t, label, strike, right, action_text):
        if not t:
            return f"    • {label} {strike}{right} [{action_text}]: 無即時報價"
        bid = f"{t.bid:.2f}" if is_valid_price(t.bid) else "-"
        ask = f"{t.ask:.2f}" if is_valid_price(t.ask) else "-"
        last = f"{t.last:.2f}" if is_valid_price(t.last) else "-"
        mg = getattr(t, 'modelGreeks', None)
        d = f"{mg.delta:+.3f}" if (mg and mg.delta is not None) else "-"
        th = f"{mg.theta:.2f}" if (mg and mg.theta is not None) else "-"
        iv = f"{mg.impliedVol*100:.1f}%" if (mg and mg.impliedVol is not None) else "-"
        return f"    • {label} {strike}{right} [{action_text}]: Bid={bid} | Ask={ask} | Last={last} | Delta={d} | Theta={th} | IV={iv}"

    legs_quote_str = "\n".join([
        fmt_q(tc_center, "中心ATM Call", center_strike, "C", center_action_desc),
        fmt_q(tp_center, "中心ATM Put ", center_strike, "P", center_action_desc),
        fmt_q(tc_wing,   "上翼   Call", call_wing_strike, "C", wing_action_desc),
        fmt_q(tp_wing,   "下翼   Put ", put_wing_strike, "P", wing_action_desc),
    ])

    return {
        'symbol': underlying.symbol,
        'exchange': opt_exchange,
        'underlying_price': ref_price,
        'c_center': c_center,
        'p_center': p_center,
        'c_wing': c_wing,
        'p_wing': p_wing,
        'center_strike': center_strike,
        'call_wing_strike': call_wing_strike,
        'put_wing_strike': put_wing_strike,
        'wing_width': wing_width,
        'bid_up': bid_up,
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
    }


# ==============================================================================
# 5. 建立並送出 0DTE Butterfly 組合單 (執行 bid_up 限價保護)
# ==============================================================================
def execute_dte0_butterfly(legs):
    symbol = legs['symbol']
    exchange = legs['exchange']
    iron = legs.get('iron', 'long')
    is_long = legs.get('is_long', True)
    strategy_name = legs.get('strategy_name', 'Long Butterfly')
    center_desc = legs.get('center_action_desc', '買入 (BUY)')
    wing_desc = legs.get('wing_action_desc', '賣出 (SELL)')
    legs_quote_str = legs.get('legs_quote_str', '')

    combo_contract = Contract(symbol=symbol, secType='BAG', currency='USD', exchange=exchange)
    combo_contract.comboLegs = [
        ComboLeg(conId=legs['c_center'].conId, ratio=1, action='BUY', exchange=exchange),
        ComboLeg(conId=legs['p_center'].conId, ratio=1, action='BUY', exchange=exchange),
        ComboLeg(conId=legs['c_wing'].conId, ratio=1, action='SELL', exchange=exchange),
        ComboLeg(conId=legs['p_wing'].conId, ratio=1, action='SELL', exchange=exchange),
    ]

    ticker = ib.reqMktData(combo_contract, '', False, False)
    ib.sleep(2)

    combo_bid = ticker.bid if (ticker and ticker.bid is not None and ticker.bid > 0) else 0.0

    c_center_bid = (legs['tc_center'].bid or 0.0) if legs['tc_center'] else 0.0
    p_center_bid = (legs['tp_center'].bid or 0.0) if legs['tp_center'] else 0.0
    c_wing_ask = (legs['tc_wing'].ask or 0.0) if legs['tc_wing'] else 0.0
    p_wing_ask = (legs['tp_wing'].ask or 0.0) if legs['tp_wing'] else 0.0

    synthetic_bid = 0.0
    if c_center_bid > 0 and p_center_bid > 0:
        synthetic_bid = (c_center_bid + p_center_bid) - (c_wing_ask + p_wing_ask)

    raw_bid = combo_bid if combo_bid > 0 else synthetic_bid
    ref_leg = legs.get('c_center')
    bid_up = legs.get('bid_up')

    # 執行 bid_up 限價上限保護: min(即時查詢到的bid價格, bid_up)
    chosen_bid = raw_bid
    if bid_up is not None and float(bid_up) > 0:
        bid_up_val = float(bid_up)
        if raw_bid > 0:
            if raw_bid > bid_up_val:
                print(f"-> 🛡️ [bid_up 上限保護] 即時查詢 Bid (${raw_bid}) 超過設定上限 (${bid_up_val})，依規則採用上限價: ${bid_up_val}")
                chosen_bid = bid_up_val
            else:
                print(f"-> ℹ️ [價格檢驗正常] 即時查詢 Bid (${raw_bid}) <= bid_up (${bid_up_val})，採用即時價: ${raw_bid}")
                chosen_bid = raw_bid
        else:
            chosen_bid = min(0.10, bid_up_val)

    if chosen_bid > 0:
        limit_price = round_to_valid_tick(symbol, chosen_bid, ref_leg)
    else:
        default_p = 0.05
        if bid_up is not None and float(bid_up) > 0:
            default_p = min(default_p, float(bid_up))
        limit_price = round_to_valid_tick(symbol, default_p, ref_leg)

    if bid_up is not None and float(bid_up) > 0 and limit_price > float(bid_up):
        limit_price = round_to_valid_tick(symbol, float(bid_up), ref_leg)

    limit_price = round(limit_price, 4)

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
        f"0DTE 指數蝶式組合單 ({strategy_name}):\n"
        f"  標的代號: {symbol} (市價: {legs['underlying_price']:.2f})\n"
        f"  策略方向 (iron): {iron.upper()} (中心 ATM: {center_desc} / 價外上下翼: {wing_desc})\n"
        f"  中心 ATM: {legs['center_strike']}\n"
        f"  上翼 Call: {legs['call_wing_strike']} (+{legs['wing_width']})\n"
        f"  下翼 Put : {legs['put_wing_strike']} (-{legs['wing_width']})\n"
        f"  到期日: {legs['expiry']} (DTE: {legs['dte']} 天)\n"
        f"  四腿即時報價與 Greeks:\n"
        f"{legs_quote_str}\n"
        f"  下單方式: BID LIMIT (限價單，保護上限: {bid_up if bid_up is not None else '無'})\n"
        f"  委託動作: {action_chinese} {order_action} {TRADE_QTY} 口 @ ${limit_price}\n"
        f"  淨 Delta: {legs['total_delta']:+.3f} | 淨 Theta: {legs['total_theta']:.2f}"
    )

    if send_live:
        RECENT_IB_ERRORS.clear()
        trade = ib.placeOrder(combo_contract, order)
        print(f"=== [已送出委託單至 IBKR] ===\n{summary_str}")

        for _ in range(5):
            ib.sleep(1)
            if trade.orderStatus.status not in ('PendingSubmit', ''):
                break

        order_errors = [e for e in RECENT_IB_ERRORS if e[0] == trade.order.orderId or e[1] in (110, 201, 103, 321, 200)]
        if order_errors:
            err_code, err_msg = order_errors[-1][1], order_errors[-1][2]
            print(f"❌ [下單被拒絕] IBKR 回報錯誤 {err_code}: {err_msg}")
            reject_msg = (
                f"【DTE0 指數期權下單被拒絕警示】\n"
                f"商品: {symbol}\n"
                f"錯誤代碼: {err_code}\n"
                f"錯誤訊息: {err_msg}\n"
                f"委託動作: {action_chinese} {order_action} (限價: ${limit_price})"
            )
            try:
                send_trade_notification(symbol, reject_msg, {"error_code": err_code, "error_msg": err_msg})
            except Exception:
                pass
        elif trade.orderStatus.status in ('PreSubmitted', 'Submitted'):
            print(f"✅ [委託成功確認] IBKR 已成功接收並排入市場 (狀態: {trade.orderStatus.status}, 限價: {limit_price})")
            submit_msg = (
                f"【DTE0 指數期權下單成功通知】\n"
                f"商品: {symbol} [0DTE]\n"
                f"方向: 中心 ATM {center_desc} / 價外上下翼 {wing_desc}\n"
                f"到期日: {legs['expiry']}\n"
                f"中心 ATM: {legs['center_strike']} | 上翼: {legs['call_wing_strike']} | 下翼: {legs['put_wing_strike']}\n"
                f"委託限價: ${limit_price} (上限 bid_up: {bid_up})\n"
                f"排單狀態: {trade.orderStatus.status}\n"
                f"淨 Delta: {legs['total_delta']:+.3f} | 淨 Theta: {legs['total_theta']:.2f}"
            )
            try:
                send_trade_notification(symbol, submit_msg, {"symbol": symbol, "price": limit_price, "status": trade.orderStatus.status})
            except Exception:
                pass

        end_time = time.time() + 55
        while time.time() < end_time:
            ib.sleep(1)
            if trade.orderStatus.status in ('Filled', 'Cancelled'):
                break

        if trade.orderStatus.status == 'Filled':
            print(f"=== [成交確認] {symbol} 0DTE 已完全成交，均價: {trade.orderStatus.avgFillPrice} ===")
    else:
        print(f"=== [測試模式 - 僅列印不送單] (OP_SEND_WEBHOOK=false 或 --dry-run) ===\n{summary_str}")

    ib.sleep(2)


# ==============================================================================
# 6. 主執行迴圈 (僅掃描 SPX, NDX)
# ==============================================================================
def run_dte0_cycle():
    hedge_config = load_op_hedge_config()
    if not hedge_config:
        print("[資訊] trade/.env 中的 OP_HEDGE_CONFIG_JSON 為空，跳過。")
        return

    # 僅篩選包含 SPX 或 NDX 的群組
    target_groups = {}
    for g_name, g_info in hedge_config.items():
        syms = g_info.get('symbols', [])
        if any(s in INDEX_SYMBOLS or 'SPX' in s or 'NDX' in s for s in syms) or 'SPX' in g_name or 'NDX' in g_name:
            target_groups[g_name] = g_info

    if not target_groups:
        print("[資訊] OP_HEDGE_CONFIG_JSON 中未找到 SPX 或 NDX 指數群組。")
        return

    market_date = get_market_today()
    print(f"\n[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 啟動 0DTE 指數期權策略掃描 (基準日: {market_date}，共 {len(target_groups)} 個指數群組)...")

    for g_name, g_info in target_groups.items():
        print(f"\n================ 開始處理指數群組: {g_name} ================")
        symbols = g_info.get('symbols', [])
        wing_width = g_info.get('wing_width')
        bid_up = g_info.get('bid_up')
        iron = str(g_info.get('iron', 'long')).strip().lower()

        underlying, opt_class, chains = resolve_index_symbols(g_name, symbols)
        if not underlying:
            print(f"[略過] 無法解析 {g_name} 的指數標的結構。")
            continue

        current_pos = [p for p in ib.portfolio() if p.contract.symbol == underlying.symbol and p.contract.secType in ['OPT', 'IND']]
        if not current_pos:
            print(f"-> 準備為 {underlying.symbol} 建立 0DTE Butterfly 部位 (方向: {iron.upper()}, 翼寬: {wing_width or '預設'}, bid_up: {bid_up or '預設'})...")
            legs = get_dte0_butterfly_legs(
                underlying=underlying,
                opt_class=opt_class,
                chains=chains,
                wing_width=wing_width,
                bid_up=bid_up,
                iron=iron
            )
            if legs:
                execute_dte0_butterfly(legs)
        else:
            print(f"-> {underlying.symbol} 已有指數期權部位 ({len(current_pos)} 筆)，跳過重複建倉。")


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description="0DTE 指數期權 (SPX/NDX) Butterfly 自動建倉腳本")
    parser.add_argument("--dry-run", action="store_true", help="模擬模式（不實際送單至 IBKR）")
    parser.add_argument("--no-line", action="store_true", help="不發送 LINE 通知")
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

    print("完成時間:", datetime.datetime.now().strftime('%H:%M:%S'))
