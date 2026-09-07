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
    獲取美東時間 (US Eastern Date)，作為美股/CBOE/CME期權到期日與 0DTE 的計算基準。
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
# 1. 預設交易參數與商品對應表
# ==============================================================================
env_config = load_env_config()
IB_HOST = env_config.get('IB_HOST', '127.0.0.1')
IB_PORT = int(env_config.get('IB_PORT', 4001))
CLIENT_ID = int(env_config.get('IB_CLIENT_ID', 100)) + 6

DEFAULT_MIN_DTE = 30
DEFAULT_MAX_DTE = 60
DEFAULT_WING_WIDTH = 50.0
TRADE_QTY = 1

# 期貨標的專屬交易所映射表 (避免 Error 321: Missing exchange for security type FUT)
FUTURE_EXCHANGE_MAP = {
    'ES': 'CME',
    'NQ': 'CME',
    'RTY': 'CME',
    'JPY': 'CME',
    'EUR': 'CME',
    'ZC': 'CBOT',
    'CL': 'NYMEX',
    'NG': 'NYMEX',
    'GC': 'COMEX',
    'HG': 'COMEX',
}

# 商品代碼映射 (Option Trading Class / Micro Symbol -> Standard Underlying Symbol)
FUTURE_SYMBOL_MAP = {
    'ZC': 'ZC', 'OZC': 'ZC', 'OCD': 'ZC', 'XC': 'ZC', 'YC': 'ZC',
    'JP': 'JPY', 'JPU': 'JPY', '6J': 'JPY', 'JPY': 'JPY', 'MJY': 'JPY',
    'EU': 'EUR', 'EUU': 'EUR', '6E': 'EUR', 'EUR': 'EUR', 'M6E': 'EUR',
    'HG': 'HG', 'HXE': 'HG', 'H1W': 'HG', 'H2W': 'HG', 'H3W': 'HG', 'H4W': 'HG', 'H1T': 'HG', 'MHG': 'HG',
    'ES': 'ES', 'EWN': 'ES', 'EW1': 'ES', 'EWQ': 'ES', 'ESU': 'ES', 'EW': 'ES', 'MES': 'ES',
    'NQ': 'NQ', 'QN': 'NQ', 'MNQ': 'NQ',
    'GC': 'GC', 'OG': 'GC', 'MGC': 'GC',
    'RTY': 'RTY', 'RTO': 'RTY', 'M2K': 'RTY', 'RT': 'RTY',
    'NG': 'NG', 'LN': 'NG', 'ON': 'NG', 'MNG': 'NG',
    'CL': 'CL', 'LO': 'CL', 'MCL': 'CL',
    # 新增指數類期權 (SPX, NDX)
    'SPX': 'SPX', 'SPXW': 'SPX',
    'NDX': 'NDX', 'NDXP': 'NDX',
}

# 優先/標準選擇權交易類別映射表 (SPX 優先 SPXW, NDX 優先 NDXP 以鎖定 0DTE/每日合約)
PREFERRED_TRADING_CLASS_MAP = {
    'CL': 'LO',
    'NG': 'LN',
    'GC': 'OG',
    'ZC': 'OZC',
    'HG': 'HXE',
    'ES': 'EW',
    'NQ': 'QN',
    'RTY': 'RTO',
    'EUR': 'EUU',
    'JPY': 'JPU',
    'SPX': 'SPXW',
    'NDX': 'NDXP',
}

# 預設動態翼寬設定 (若 .env 中的個別商品未指定 wing_width 則採用此表)
WING_WIDTH_MAP = {
    'ZC': 40,
    'NQ': 200,
    'ES': 200,
    'SPX': 50,
    'NDX': 200,
    'JPY': 0.0020,
    'EUR': 0.04,
    'HG': 0.20,
    'GC': 80,
    'RTY': 50,
    'NG': 0.20,
    'CL': 5.0,
}

# 各商品最小跳動點 (用於精準四捨五入至合法的 Limit Price)
MIN_TICK_MAP = {
    'ES': 0.25,
    'NQ': 0.25,
    'RTY': 0.1,
    'SPX': 0.05,
    'NDX': 0.10,
    'ZC': 0.125,
    'HG': 0.0005,
    'GC': 0.1,
    'EUR': 0.00005,
    'JPY': 0.0000005,
    'NG': 0.001,
    'CL': 0.01,
}

INDEX_SYMBOLS = {'SPX', 'NDX'}

ib = IB()

RECENT_IB_ERRORS = []


def on_ib_error(reqId, errorCode, errorString, contract):
    RECENT_IB_ERRORS.append((reqId, errorCode, errorString, contract))
    if errorCode in (110, 201, 103, 321, 200, 399):
        print(f"[IB 委託拒絕/警示] ReqId: {reqId} | 代碼: {errorCode} | 訊息: {errorString}")


ib.errorEvent += on_ib_error

MARKET_RULES_CACHE = {}


def get_price_increment_rules(contract):
    """
    動態向 IBKR 取得合約在交易所的價格跳動點階梯規則 (Market Rule)。
    若查無動態規則，退回內建階梯或預設值。
    """
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
    """
    根據 IBKR 交易所精確的階梯最小跳動點 (Market Rule) 將委託價修整為合法的限價：
    例如：
      - ES (Rule 235): <5 -> 0.05, 5~20 -> 0.10, 20~100 -> 0.25, >=100.0 -> 0.50
      - NQ (Rule 85):  <5 -> 0.05, 5~100 -> 0.25, 100~500 -> 0.50, >=500.0 -> 1.00
      - RTY (Rule 99): <5 -> 0.05, 5~20 -> 0.10, 20~100 -> 0.25, >=100.0 -> 0.50
      - SPX / NDX:     <3 -> 0.05, >=3.0 -> 0.10
    """
    if price is None or price <= 0:
        price = 0.01

    rules = None
    if contract:
        rules = get_price_increment_rules(contract)

    # 內建主要商品備援階梯表 (防止連線延遲或 API 查無時保證合法)
    if not rules:
        FALLBACK_RULES = {
            'ES': [(0.0, 0.05), (5.0, 0.1), (20.0, 0.25), (100.0, 0.5)],
            'NQ': [(0.0, 0.05), (5.0, 0.25), (100.0, 0.5), (500.0, 1.0)],
            'RTY': [(0.0, 0.05), (5.0, 0.1), (20.0, 0.25), (100.0, 0.5)],
            'SPX': [(0.0, 0.05), (3.0, 0.1)],
            'NDX': [(0.0, 0.05), (3.0, 0.1)],
        }
        rules = FALLBACK_RULES.get(symbol)

    if rules:
        inc = rules[0][1]
        for low_edge, increment in rules:
            if price >= low_edge:
                inc = increment
            else:
                break
        valid_price = round(price / inc) * inc
        return round(valid_price, 6)

    min_tick = MIN_TICK_MAP.get(symbol, 0.01)
    return round(round(price / min_tick) * min_tick, 6)


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
            # 啟用延遲/凍結市場數據 (MarketDataType=3)，防止無即時報價訂閱時噴出 Error 10168
            ib.reqMarketDataType(3)
        except Exception:
            pass
        actual_id = ib.client.clientId if ib.client else CLIENT_ID
        print(f"=== [系統] 成功連接至 IBKR ({IB_HOST}:{IB_PORT}) | ClientId={actual_id} | 延遲市場資料已啟用 ===")


def send_webhook_notification(action: str, symbol: str, qty: float, price: float, note: str = ""):
    payload = {
        "symbol": symbol,
        "action": action,
        "quantity": str(qty),
        "price": str(price),
        "strategy_name": "butterfly",
        "note": note
    }
    msg = f"選擇權交易通知: {action} {qty}口 @ {price} ({note})"
    try:
        cfg = load_env_config()
        send_live = str(cfg.get('OP_SEND_WEBHOOK', 'false')).strip().lower() in ('true', '1')
        if send_live:
            send_trade_notification(symbol, msg, payload)
        else:
            print(f"-> [測試模式通知] {msg}")
    except Exception as e:
        print(f"-> ❌ 推播通知發送異常: {e}")


# ==============================================================================
# 2. 穩健獲取標的現價 (支援 Live、Delayed、Close、MidPoint 與歷史 K 棒備援)
# ==============================================================================
def get_underlying_price(underlying):
    """
    穩健獲取底層標的市價，杜絕 nan：
    1. 訂閱市場數據 (reqMktData) 取得即時/延遲報價
    2. 檢查 marketPrice, last, markPrice, close, bid/ask 中間價
    3. 若皆為 nan，透過 reqHistoricalData (TRADES) 取得最新日 K 收盤價備援
    """
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

    # 若即時/延遲數據仍未取到，請求日 K 收盤價備援 (一律使用 TRADES 適用期貨與指數)
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
# 3. 動態解析群組商品 (支援期貨與 SPX/NDX 指數現貨標的)
# ==============================================================================
def resolve_group_symbols(group_name, symbols):
    underlying_sym = None
    for sym in symbols:
        if sym in FUTURE_SYMBOL_MAP:
            underlying_sym = FUTURE_SYMBOL_MAP[sym]
            break
    if not underlying_sym and symbols:
        underlying_sym = FUTURE_SYMBOL_MAP.get(symbols[0], symbols[0])

    if not underlying_sym:
        return None, None, []

    # 1. 處理指數類商品 (SPX, NDX)
    if underlying_sym in INDEX_SYMBOLS:
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

    # 2. 處理期貨類商品 (ES, NQ, CL, GC, ZC, HG...)
    fut_exchange = FUTURE_EXCHANGE_MAP.get(underlying_sym, '')
    details = ib.reqContractDetails(Future(symbol=underlying_sym, exchange=fut_exchange))
    valid_details = [d for d in details if d.contract.exchange not in ['QBALGO', 'SMART']]

    if valid_details:
        valid_details = sorted(valid_details, key=lambda d: d.contract.lastTradeDateOrContractMonth)
        underlying_fut = valid_details[0].contract

        trading_classes = set()
        chains = []
        for d in valid_details[:6]:
            res = ib.reqSecDefOptParams(d.contract.symbol, d.contract.exchange, d.contract.secType, d.contract.conId)
            if res:
                chains.extend(res)
                for c in res:
                    trading_classes.add(c.tradingClass)

        preferred = PREFERRED_TRADING_CLASS_MAP.get(underlying_fut.symbol)
        opt_class = None
        if preferred and preferred in trading_classes:
            opt_class = preferred

        if not opt_class:
            opt_class = next((s for s in symbols if s in trading_classes and not s.startswith(('M', 'W', 'X'))), None)

        if not opt_class and chains:
            std_candidates = [c.tradingClass for c in chains if not c.tradingClass.startswith(('W', 'X', 'M'))]
            opt_class = std_candidates[0] if std_candidates else chains[0].tradingClass

        return underlying_fut, opt_class, chains

    return None, None, []


# ==============================================================================
# 4. 獲取 Butterfly 蝶式四腿合約與當前市價
# ==============================================================================
def get_butterfly_legs(underlying, opt_class, chains, min_dte=DEFAULT_MIN_DTE, max_dte=DEFAULT_MAX_DTE, wing_width=None):
    """
    根據商品設定的 DTE 範圍篩選到期日（支援 0DTE 當天到期末日期權），並以市價最近之 ATM 履約價建立 Butterfly 蝶式：
    - 上翼（Upper Wing）：賣出 1 口 Call，履約價為 中心 + wing_width
    - 中心（Center Body）：買入 1 口 Call、買入 1 口 PUT，履約價鎖定 ATM 平價
    - 下翼（Lower Wing）：賣出 1 口 PUT，履約價為 中心 - wing_width
    """
    market_today = get_market_today()
    is_zero_dte = (min_dte == 0)
    target_dte = 0 if is_zero_dte else ((min_dte + max_dte) // 2)

    class_candidates = []
    # 優先從指定的標準 opt_class 尋找符合 DTE 範圍之合約
    if opt_class:
        for c in chains:
            if c.tradingClass == opt_class:
                for exp in c.expirations:
                    try:
                        exp_date = datetime.datetime.strptime(exp, '%Y%m%d').date()
                        dte = (exp_date - market_today).days
                        if dte >= 0 and (min_dte <= dte <= max_dte):
                            class_candidates.append((c.tradingClass, exp, c.strikes, dte, c.multiplier))
                    except Exception:
                        pass

    # 若指定類別未找到，放寬至該標的的其他月/週/日合約
    if not class_candidates and chains:
        for c in chains:
            if not c.tradingClass.startswith(('M', 'W', 'X')) or underlying.symbol in INDEX_SYMBOLS:
                for exp in c.expirations:
                    try:
                        exp_date = datetime.datetime.strptime(exp, '%Y%m%d').date()
                        dte = (exp_date - market_today).days
                        if dte >= 0 and (min_dte <= dte <= max_dte):
                            class_candidates.append((c.tradingClass, exp, c.strikes, dte, c.multiplier))
                    except Exception:
                        pass

    # 若指定 0DTE 但當前恰逢休市或當天無合約，自動備選大於等於 0 且最近之到期日
    if not class_candidates and chains:
        for c in chains:
            for exp in c.expirations:
                try:
                    exp_date = datetime.datetime.strptime(exp, '%Y%m%d').date()
                    dte = (exp_date - market_today).days
                    if dte >= 0:
                        class_candidates.append((c.tradingClass, exp, c.strikes, dte, c.multiplier))
                except Exception:
                    pass

    if not class_candidates:
        print(f"[錯誤] 找不到 {underlying.symbol} 符合條件的期權合約到期日 (DTE 設定: {min_dte}~{max_dte} 天)")
        return None

    # 若用戶指定 0DTE，優先過濾出 dte == 0 的合約
    if is_zero_dte:
        zero_candidates = [c for c in class_candidates if c[3] == 0]
        if zero_candidates:
            class_candidates = zero_candidates

    # 選取 DTE 最接近目標 DTE 的合約 (0DTE 模式下優先鎖定 0DTE，若非交易日則鎖定最近合約)
    best_candidate = min(class_candidates, key=lambda item: (abs(item[3] - target_dte), item[3]))
    chosen_opt_class, closest_expiry, strikes, current_dte, multiplier = best_candidate

    dte_desc = "當天末日輪 (0DTE)" if current_dte == 0 else f"DTE: {current_dte} 天"
    print(f"-> 鎖定 {underlying.symbol} (Class: {chosen_opt_class}) 到期日: {closest_expiry} ({dte_desc}，設定範圍: {min_dte}~{max_dte} 天)")

    # 取得底層標的最新市價 (期貨或指數，具備完整防 NaN 與日 K 備援)
    ref_price = get_underlying_price(underlying)
    if not is_valid_price(ref_price):
        print(f"[錯誤] 無法取得 {underlying.symbol} 的有效標的市價，無法定位 ATM 平價履約價。")
        return None

    print(f"-> {underlying.symbol} 當前標的市價: {ref_price:.2f}")

    # 決定動態翼寬 WING_WIDTH (優先採用群組個別設定，無設定則退回預設對應表)
    if wing_width is None or float(wing_width) <= 0:
        wing_width = WING_WIDTH_MAP.get(underlying.symbol, DEFAULT_WING_WIDTH)
    wing_width = float(wing_width)

    # 1. 鎖定中心 ATM 履約價
    valid_strikes = sorted(list(set(strikes)))
    center_strike = min(valid_strikes, key=lambda s: abs(s - ref_price))

    # 2. 鎖定上翼 (Upper Wing / 賣出 Call，履約價為 中心 + wing_width)
    target_upper = center_strike + wing_width
    upper_candidates = [s for s in valid_strikes if s > center_strike]
    if not upper_candidates:
        print(f"[錯誤] 找不到高於中心履約價 ({center_strike}) 的上翼履約價。")
        return None
    call_wing_strike = min(upper_candidates, key=lambda s: abs(s - target_upper))

    # 3. 鎖定下翼 (Lower Wing / 賣出 Put，履約價為 中心 - wing_width)
    target_lower = center_strike - wing_width
    lower_candidates = [s for s in valid_strikes if s < center_strike]
    if not lower_candidates:
        print(f"[錯誤] 找不到低於中心履約價 ({center_strike}) 的下翼履約價。")
        return None
    put_wing_strike = min(lower_candidates, key=lambda s: abs(s - target_lower))

    actual_upper_width = call_wing_strike - center_strike
    actual_lower_width = center_strike - put_wing_strike

    print(
        f"-> 蝶式結構 (Butterfly) 履約價確認:\n"
        f"   ~ 中心 (Center Body) ATM: {center_strike} (市價: {ref_price:.2f}) [買入 Call + 買入 Put]\n"
        f"   ~ 上翼 (Call Wing) 賣出 Call: {call_wing_strike} (設定翼寬: {wing_width}, 實際間距: {actual_upper_width})\n"
        f"   ~ 下翼 (Put Wing)  賣出 Put : {put_wing_strike} (設定翼寬: {wing_width}, 實際間距: {actual_lower_width})\n"
        f"   ~ 到期日: {closest_expiry} ({dte_desc})"
    )

    # 建立 4 條腿合約物件並驗證
    is_idx = (underlying.secType == 'IND')
    opt_exchange = 'SMART' if is_idx else underlying.exchange

    if is_idx:
        c_center = Option(underlying.symbol, closest_expiry, center_strike, 'C', opt_exchange, currency='USD', tradingClass=chosen_opt_class)
        p_center = Option(underlying.symbol, closest_expiry, center_strike, 'P', opt_exchange, currency='USD', tradingClass=chosen_opt_class)
        c_wing = Option(underlying.symbol, closest_expiry, call_wing_strike, 'C', opt_exchange, currency='USD', tradingClass=chosen_opt_class)
        p_wing = Option(underlying.symbol, closest_expiry, put_wing_strike, 'P', opt_exchange, currency='USD', tradingClass=chosen_opt_class)
    else:
        c_center = FuturesOption(underlying.symbol, closest_expiry, center_strike, 'C', opt_exchange, tradingClass=chosen_opt_class)
        p_center = FuturesOption(underlying.symbol, closest_expiry, center_strike, 'P', opt_exchange, tradingClass=chosen_opt_class)
        c_wing = FuturesOption(underlying.symbol, closest_expiry, call_wing_strike, 'C', opt_exchange, tradingClass=chosen_opt_class)
        p_wing = FuturesOption(underlying.symbol, closest_expiry, put_wing_strike, 'P', opt_exchange, tradingClass=chosen_opt_class)

    qualified = ib.qualifyContracts(c_center, p_center, c_wing, p_wing)
    if len(qualified) < 4:
        print(f"[錯誤] 合約驗證失敗，無法完整取得 4 腿合約 (成功數: {len(qualified)}/4)")
        return None

    # 取得報價與 Greeks
    print(f"-> 正在取得 4 腿期權合約即時報價與 Greeks...")
    tickers = ib.reqTickers(c_center, p_center, c_wing, p_wing)
    ib.sleep(3)

    t_map = {t.contract.conId: t for t in tickers}
    tc_center = t_map.get(c_center.conId)
    tp_center = t_map.get(p_center.conId)
    tc_wing = t_map.get(c_wing.conId)
    tp_wing = t_map.get(p_wing.conId)

    def get_greek(t, name):
        if t and t.modelGreeks and getattr(t.modelGreeks, name, None) is not None:
            return getattr(t.modelGreeks, name)
        return 0.0

    # 組合總淨 Delta = +C_center + P_center - C_wing - P_wing
    total_delta = (get_greek(tc_center, 'delta') + get_greek(tp_center, 'delta')
                   - get_greek(tc_wing, 'delta') - get_greek(tp_wing, 'delta'))
    # 組合總淨 Theta = +C_center + P_center - C_wing - P_wing
    total_theta = (get_greek(tc_center, 'theta') + get_greek(tp_center, 'theta')
                   - get_greek(tc_wing, 'theta') - get_greek(tp_wing, 'theta'))

    return {
        'symbol': underlying.symbol,
        'exchange': opt_exchange,
        'sec_type': underlying.secType,
        'underlying_price': ref_price,
        'c_center': c_center,
        'p_center': p_center,
        'c_wing': c_wing,
        'p_wing': p_wing,
        'center_strike': center_strike,
        'call_wing_strike': call_wing_strike,
        'put_wing_strike': put_wing_strike,
        'wing_width': wing_width,
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
# 5. 建立並送出 Butterfly 組合單 (全部以 BID LIMIT 限價送單)
# ==============================================================================
def execute_butterfly(legs):
    symbol = legs['symbol']
    exchange = legs['exchange']

    # 建立 BAG 組合單合約
    combo_contract = Contract(symbol=symbol, secType='BAG', currency='USD', exchange=exchange)

    # 4 腿定義：
    # 1. 買入 1 口 ATM Call (中心)
    # 2. 買入 1 口 ATM Put (中心)
    # 3. 賣出 1 口 OTM Call (上翼)
    # 4. 賣出 1 口 OTM Put (下翼)
    combo_contract.comboLegs = [
        ComboLeg(conId=legs['c_center'].conId, ratio=1, action='BUY', exchange=exchange),
        ComboLeg(conId=legs['p_center'].conId, ratio=1, action='BUY', exchange=exchange),
        ComboLeg(conId=legs['c_wing'].conId, ratio=1, action='SELL', exchange=exchange),
        ComboLeg(conId=legs['p_wing'].conId, ratio=1, action='SELL', exchange=exchange),
    ]

    ticker = ib.reqMktData(combo_contract, '', False, False)
    ib.sleep(2)

    combo_bid = ticker.bid if (ticker and ticker.bid is not None and ticker.bid > 0) else 0.0
    combo_ask = ticker.ask if (ticker and ticker.ask is not None and ticker.ask > 0) else 0.0

    # 若交易所尚未回傳組合單即時 Bid，從 4 腿報價計算合成 Bid (Synthetic Bid)
    # 買進腿取 Bid，賣出腿取 Ask：
    c_center_bid = (legs['tc_center'].bid or 0.0) if legs['tc_center'] else 0.0
    p_center_bid = (legs['tp_center'].bid or 0.0) if legs['tp_center'] else 0.0
    c_wing_ask = (legs['tc_wing'].ask or 0.0) if legs['tc_wing'] else 0.0
    p_wing_ask = (legs['tp_wing'].ask or 0.0) if legs['tp_wing'] else 0.0

    synthetic_bid = 0.0
    if c_center_bid > 0 and p_center_bid > 0:
        synthetic_bid = (c_center_bid + p_center_bid) - (c_wing_ask + p_wing_ask)

    # 嚴格執行「全部都下 BID LIMIT」，並套用交易所合法的階梯跳動點 (Market Rule)
    raw_bid = combo_bid if combo_bid > 0 else synthetic_bid
    ref_leg = legs.get('c_center')

    if raw_bid > 0:
        limit_price = round_to_valid_tick(symbol, raw_bid, ref_leg)
    else:
        # 若休市無買盤報價，依最小跳動點建立
        limit_price = round_to_valid_tick(symbol, 0.05, ref_leg)

    limit_price = round(limit_price, 6)

    # 建立 BUY 1 口 BID LIMIT 訂單
    order = LimitOrder('BUY', TRADE_QTY, limit_price)
    order.tif = 'DAY'

    # 即時查詢 trade/.env 中的 OP_SEND_WEBHOOK 開關
    env_cfg = load_env_config()
    send_live = str(env_cfg.get('OP_SEND_WEBHOOK', 'false')).strip().lower() in ('true', '1')

    summary_str = (
        f"Butterfly (蝶式四腿組合單):\n"
        f"  標的代號: {symbol} (市價: {legs['underlying_price']:.2f})\n"
        f"  中心 ATM (買入 Call & Put): {legs['center_strike']}\n"
        f"  上翼 (賣出 Call): {legs['call_wing_strike']} (+{legs['wing_width']})\n"
        f"  下翼 (賣出 Put) : {legs['put_wing_strike']} (-{legs['wing_width']})\n"
        f"  到期日: {legs['expiry']} (DTE: {legs['dte']} 天)\n"
        f"  下單方式: BID LIMIT (限價買入)\n"
        f"  委託限價: ${limit_price} (Market Bid: {combo_bid}, Synthetic Bid: {synthetic_bid:.4f})\n"
        f"  淨 Delta: {legs['total_delta']:+.3f} | 淨 Theta: {legs['total_theta']:.2f}"
    )

    if send_live:
        RECENT_IB_ERRORS.clear()
        trade = ib.placeOrder(combo_contract, order)
        print(f"=== [已送出委託單至 IBKR] ===\n{summary_str}")

        # 等待 IBKR 接收與回報 (確認是否排入訂單簿或被退回)
        for _ in range(5):
            ib.sleep(1)
            if trade.orderStatus.status not in ('PendingSubmit', ''):
                break

        order_errors = [e for e in RECENT_IB_ERRORS if e[0] == trade.order.orderId or e[1] in (110, 201, 103, 321, 200)]
        if order_errors:
            err_code, err_msg = order_errors[-1][1], order_errors[-1][2]
            print(f"❌ [下單被拒絕] IBKR 回報錯誤 {err_code}: {err_msg}")
        elif trade.orderStatus.status in ('PreSubmitted', 'Submitted'):
            print(f"✅ [委託成功確認] IBKR 已成功接收並排入市場 (狀態: {trade.orderStatus.status}, 限價: {limit_price})")
        else:
            print(f"ℹ️ [委託狀態] 目前狀態: {trade.orderStatus.status} (限價: {limit_price})")

        # 等待成交 (最多 60 秒)
        end_time = time.time() + 55
        while time.time() < end_time:
            ib.sleep(1)
            if trade.orderStatus.status in ('Filled', 'Cancelled'):
                break

        if trade.orderStatus.status == 'Filled':
            fill_price = trade.orderStatus.avgFillPrice
            print(f"=== [成交確認] {symbol} Butterfly 已成交，平均價格: {fill_price} ===")
            note_str = (f"成交 Butterfly (BID LIMIT): 中心 {legs['center_strike']} / "
                        f"上翼 {legs['call_wing_strike']} / 下翼 {legs['put_wing_strike']} @ {fill_price}")
            send_webhook_notification('BUY_BUTTERFLY', symbol, TRADE_QTY, fill_price, note_str)
        elif trade.orderStatus.status != 'Filled':
            print(f"=== [委託終止] {symbol} 最終委託單狀態: {trade.orderStatus.status} (限價: {limit_price}) ===")
    else:
        print(f"=== [測試模式 - 僅列印不送單] (OP_SEND_WEBHOOK=false) ===\n{summary_str}")

    ib.sleep(2)


# ==============================================================================
# 6. 主程式入口 (定期即時查詢 trade/.env 並自動執行策略)
# ==============================================================================
def run_strategy_cycle():
    """執行單輪 Butterfly 策略掃描與下單。"""
    # 即時查詢 trade/.env 中的 OP_HEDGE_CONFIG_JSON
    hedge_config = load_op_hedge_config()
    if not hedge_config:
        print("[資訊] trade/.env 中的 OP_HEDGE_CONFIG_JSON 為空，跳過此輪。")
        return

    market_date = get_market_today()
    print(f"\n[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 開始執行 Butterfly 策略掃描 (美東市場基準日: {market_date}，共 {len(hedge_config)} 個商品群組)...")

    for g_name, g_info in hedge_config.items():
        print(f"\n================ 開始處理群組: {g_name} ================")
        symbols = g_info.get('symbols', [])
        if not symbols:
            print(f"[略過] 群組 {g_name} 未指定 symbols。")
            continue

        # 個別商品 DTE 開始日與結束日 (0 代表 0DTE 當天到期)
        raw_start = g_info.get('dte_start') if g_info.get('dte_start') is not None else g_info.get('min_dte')
        raw_end = g_info.get('dte_end') if g_info.get('dte_end') is not None else g_info.get('max_dte')
        min_dte = int(raw_start) if raw_start is not None else DEFAULT_MIN_DTE
        max_dte = int(raw_end) if raw_end is not None else DEFAULT_MAX_DTE
        if min_dte > max_dte:
            min_dte, max_dte = max_dte, min_dte

        # 個別商品動態翼寬 WING_WIDTH
        wing_width = g_info.get('wing_width')

        underlying, opt_class, chains = resolve_group_symbols(g_name, symbols)
        if not underlying:
            print(f"[略過] 無法解析 {g_name} 的期貨/指數與期權結構。")
            continue

        # 檢查是否已持有該商品期權部位 (包含 FOP 與 OPT)
        current_pos = [p for p in ib.portfolio() if p.contract.symbol == underlying.symbol and p.contract.secType in ['FOP', 'OPT']]
        if not current_pos:
            dte_label = "0DTE" if min_dte == 0 else f"{min_dte}~{max_dte}天"
            print(f"-> 準備為 {underlying.symbol} (Class: {opt_class}) 建立 Butterfly 部位 (DTE: {dte_label}, 翼寬: {wing_width or '預設'})...")
            legs = get_butterfly_legs(
                underlying=underlying,
                opt_class=opt_class,
                chains=chains,
                min_dte=min_dte,
                max_dte=max_dte,
                wing_width=wing_width
            )
            if legs:
                execute_butterfly(legs)
        else:
            print(f"-> {underlying.symbol} 已有期權部位 ({len(current_pos)} 筆)，跳過重複建倉。")


if __name__ == '__main__':
    import sys

    try:
        connect_ib()
        run_strategy_cycle()
    except Exception as e:
        print(f"\n[錯誤] 執行異常: {e}")
    finally:
        if ib.isConnected():
            try:
                ib.disconnect()
            except Exception:
                pass
            
    print("最後:", datetime.datetime.now().strftime('%H:%M:%S'))
    time.sleep(60*60*20)

