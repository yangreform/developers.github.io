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

# 預設買入限價上限 (若 .env 中的個別商品未指定 bid_up 則採用此表)
DEFAULT_BID_UP_MAP = {
    'ZC': 20.0,
    'NQ': 600.0,
    'ES': 180.0,
    'SPX': 10.0,
    'NDX': 40.0,
    'JPY': 0.0010,
    'EUR': 0.02,
    'HG': 0.10,
    'GC': 100.0,
    'RTY': 25.0,
    'NG': 0.10,
    'CL': 2.5,
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
def get_butterfly_legs(underlying, opt_class, chains, min_dte=DEFAULT_MIN_DTE, max_dte=DEFAULT_MAX_DTE, wing_width=None, bid_up=None, iron='long'):
    """
    根據商品設定的 DTE 範圍篩選到期日（支援 0DTE 當天到期末日期權），並以市價最近之 ATM 履約價建立 Butterfly 蝶式：
    - iron='long': 中心 ATM 買入 (BUY Call & Put)，價外上下翼賣出 (SELL Call & Put)
    - iron='short': 中心 ATM 賣出 (SELL Call & Put)，價外上下翼買入 (BUY Call & Put)
    - 上翼（Upper Wing）：Call 履約價為 中心 + wing_width
    - 下翼（Lower Wing）：Put  履約價為 中心 - wing_width
    - 限價上限（bid_up）：鎖定委託限價之最高上限 min(即時bid, bid_up)
    """
    iron = str(iron).strip().lower() if iron else 'long'
    is_long = (iron != 'short')
    center_action_desc = "買入 (BUY)" if is_long else "賣出 (SELL)"
    wing_action_desc = "賣出 (SELL)" if is_long else "買入 (BUY)"
    strategy_name = "Long Butterfly (蝶式買方)" if is_long else "Short Butterfly (蝶式賣方/鐵蝶)"

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

    # 1. 取得該期權合約到期日對應之「真實合約細節」與「真實底層標的」
    # （期貨期權 FOP 的不同到期月份可能掛鉤不同季度的期貨，例如 10月期權掛鉤 12月期貨 NQZ6 而非 9月期貨 NQU6）
    actual_underlying = underlying
    is_idx = (underlying.secType == 'IND')
    opt_exchange = 'SMART' if is_idx else underlying.exchange

    query_contract = (
        Option(underlying.symbol, lastTradeDateOrContractMonth=closest_expiry, exchange=opt_exchange, currency='USD', tradingClass=chosen_opt_class)
        if is_idx else
        FuturesOption(underlying.symbol, lastTradeDateOrContractMonth=closest_expiry, exchange=opt_exchange, tradingClass=chosen_opt_class)
    )
    details = ib.reqContractDetails(query_contract)

    # 若帶有 tradingClass 查無資料，放寬不帶 tradingClass 查詢該到期日
    if not details:
        fallback_contract = (
            Option(underlying.symbol, lastTradeDateOrContractMonth=closest_expiry, exchange=opt_exchange, currency='USD')
            if is_idx else
            FuturesOption(underlying.symbol, lastTradeDateOrContractMonth=closest_expiry, exchange=opt_exchange)
        )
        details = ib.reqContractDetails(fallback_contract)

    exact_contract_map = {}
    if details:
        # 1.1 精準取得「該特定到期日實際存在」的合法履約價清單，杜絕使用到近月才有的 10/25 點履約價
        exact_strikes = sorted(list(set(d.contract.strike for d in details)))
        valid_strikes = exact_strikes if exact_strikes else sorted(list(set(strikes)))

        # 1.2 校正 tradingClass 為真實合約的 tradingClass
        if details[0].contract.tradingClass:
            chosen_opt_class = details[0].contract.tradingClass

        # 1.3 取得底層真實掛鉤期貨合約 (例如 10月期權掛鉤 12月期貨 NQZ6 而非 9月期貨 NQU6)
        if not is_idx and getattr(details[0], 'underConId', None):
            try:
                target_fut = Contract(conId=details[0].underConId)
                ib.qualifyContracts(target_fut)
                if target_fut.conId:
                    actual_underlying = target_fut
            except Exception as e:
                print(f"[提示] 查詢期權具體掛鉤期貨異常 ({e})，使用預設期貨標的")

        # 1.4 快取該到期日的合法合約物件 (可直接使用已具備 conId 之合約)
        for d in details:
            exact_contract_map[(d.contract.strike, d.contract.right)] = d.contract
    else:
        valid_strikes = sorted(list(set(strikes)))

    # 2. 取得底層標的最新市價 (優先以該期權真實掛鉤合約報價為準，杜絕現價與到期月份不對齊)
    ref_price = get_underlying_price(actual_underlying)
    if not is_valid_price(ref_price) and actual_underlying != underlying:
        ref_price = get_underlying_price(underlying)

    if not is_valid_price(ref_price):
        print(f"[錯誤] 無法取得 {actual_underlying.symbol} 的有效標的市價，無法定位 ATM 平價履約價。")
        return None

    under_desc = getattr(actual_underlying, 'localSymbol', actual_underlying.symbol)
    print(f"-> {underlying.symbol} 期權真實掛鉤標的: {under_desc} (當前標的市價: {ref_price:.2f})")

    # 決定動態翼寬 WING_WIDTH (優先採用群組個別設定，無設定則退回預設對應表)
    if wing_width is None or float(wing_width) <= 0:
        wing_width = WING_WIDTH_MAP.get(underlying.symbol, DEFAULT_WING_WIDTH)
    wing_width = float(wing_width)

    # 決定限價上限 BID_UP (優先採用群組設定，若無設定則退回預設對應表)
    if bid_up is None or float(bid_up) <= 0:
        bid_up = DEFAULT_BID_UP_MAP.get(underlying.symbol, None)
    try:
        bid_up = float(bid_up) if bid_up is not None else None
    except (ValueError, TypeError):
        bid_up = None

    # 1. 鎖定中心 ATM 履約價
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
        f"-> 蝶式結構 ({strategy_name}) 履約價確認:\n"
        f"   ~ 策略方向 (iron): {iron.upper()} (中心 ATM: {center_action_desc} / 價外上下翼: {wing_action_desc})\n"
        f"   ~ 中心 (Center Body) ATM: {center_strike} (市價: {ref_price:.2f}) [{center_action_desc} Call + {center_action_desc} Put]\n"
        f"   ~ 上翼 (Call Wing) {wing_action_desc} Call: {call_wing_strike} (設定翼寬: {wing_width}, 實際間距: {actual_upper_width})\n"
        f"   ~ 下翼 (Put Wing)  {wing_action_desc} Put : {put_wing_strike} (設定翼寬: {wing_width}, 實際間距: {actual_lower_width})\n"
        f"   ~ 到期日: {closest_expiry} ({dte_desc})"
    )

    # 建立 4 條腿合約物件並驗證
    c_center = exact_contract_map.get((center_strike, 'C'))
    p_center = exact_contract_map.get((center_strike, 'P'))
    c_wing = exact_contract_map.get((call_wing_strike, 'C'))
    p_wing = exact_contract_map.get((put_wing_strike, 'P'))

    if not all([c_center, p_center, c_wing, p_wing]):
        if is_idx:
            c_center = c_center or Option(underlying.symbol, closest_expiry, center_strike, 'C', opt_exchange, currency='USD', tradingClass=chosen_opt_class)
            p_center = p_center or Option(underlying.symbol, closest_expiry, center_strike, 'P', opt_exchange, currency='USD', tradingClass=chosen_opt_class)
            c_wing = c_wing or Option(underlying.symbol, closest_expiry, call_wing_strike, 'C', opt_exchange, currency='USD', tradingClass=chosen_opt_class)
            p_wing = p_wing or Option(underlying.symbol, closest_expiry, put_wing_strike, 'P', opt_exchange, currency='USD', tradingClass=chosen_opt_class)
        else:
            c_center = c_center or FuturesOption(underlying.symbol, closest_expiry, center_strike, 'C', opt_exchange, tradingClass=chosen_opt_class)
            p_center = p_center or FuturesOption(underlying.symbol, closest_expiry, center_strike, 'P', opt_exchange, tradingClass=chosen_opt_class)
            c_wing = c_wing or FuturesOption(underlying.symbol, closest_expiry, call_wing_strike, 'C', opt_exchange, tradingClass=chosen_opt_class)
            p_wing = p_wing or FuturesOption(underlying.symbol, closest_expiry, put_wing_strike, 'P', opt_exchange, tradingClass=chosen_opt_class)

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

    def format_leg_quote(label, strike, right, action_text, t):
        if not t:
            return f"   ~ {label} [{action_text} {right}{strike}]: (無即時報價)"
        b = f"{t.bid:.4f}".rstrip('0').rstrip('.') if (t.bid is not None and not math.isnan(t.bid) and t.bid > 0) else "N/A"
        a = f"{t.ask:.4f}".rstrip('0').rstrip('.') if (t.ask is not None and not math.isnan(t.ask) and t.ask > 0) else "N/A"
        try:
            mp = t.marketPrice()
            p = f"{mp:.4f}".rstrip('0').rstrip('.') if (mp is not None and not math.isnan(mp) and mp > 0) else "N/A"
        except Exception:
            p = "N/A"
        
        g_parts = []
        if t.modelGreeks:
            if t.modelGreeks.delta is not None and not math.isnan(t.modelGreeks.delta):
                g_parts.append(f"Delta: {t.modelGreeks.delta:+.3f}")
            if t.modelGreeks.theta is not None and not math.isnan(t.modelGreeks.theta):
                g_parts.append(f"Theta: {t.modelGreeks.theta:.2f}")
            if t.modelGreeks.impliedVol is not None and not math.isnan(t.modelGreeks.impliedVol):
                g_parts.append(f"IV: {t.modelGreeks.impliedVol*100:.1f}%")
        g_str = f" | {', '.join(g_parts)}" if g_parts else ""
        return f"   ~ {label:<8} [{action_text} {right}{strike}]: Bid={b:<8} Ask={a:<8} 市價={p:<8}{g_str}"

    def format_leg_compact(label, strike, right, action_text, t):
        if not t:
            return f"{label} [{action_text} {right}{strike}]: (無報價)"
        b = f"{t.bid:.4f}".rstrip('0').rstrip('.') if (t.bid is not None and not math.isnan(t.bid) and t.bid > 0) else "-"
        a = f"{t.ask:.4f}".rstrip('0').rstrip('.') if (t.ask is not None and not math.isnan(t.ask) and t.ask > 0) else "-"
        g_d = f" Δ{t.modelGreeks.delta:+.2f}" if (t.modelGreeks and t.modelGreeks.delta is not None and not math.isnan(t.modelGreeks.delta)) else ""
        return f"{label} [{action_text} {right}{strike}]: B:{b} / A:{a}{g_d}"

    legs_quote_str = (
        f"{format_leg_quote('中心 Call', center_strike, 'C', center_action_desc, tc_center)}\n"
        f"{format_leg_quote('中心 Put ', center_strike, 'P', center_action_desc, tp_center)}\n"
        f"{format_leg_quote('上翼 Call', call_wing_strike, 'C', wing_action_desc, tc_wing)}\n"
        f"{format_leg_quote('下翼 Put ', put_wing_strike, 'P', wing_action_desc, tp_wing)}"
    )

    line_legs_quote_str = (
        f"  • {format_leg_compact('中心 Call', center_strike, 'C', center_action_desc, tc_center)}\n"
        f"  • {format_leg_compact('中心 Put ', center_strike, 'P', center_action_desc, tp_center)}\n"
        f"  • {format_leg_compact('上翼 Call', call_wing_strike, 'C', wing_action_desc, tc_wing)}\n"
        f"  • {format_leg_compact('下翼 Put ', put_wing_strike, 'P', wing_action_desc, tp_wing)}"
    )

    print(
        f"-> 4 腿期權合約即時報價與 Greeks 詳情:\n"
        f"{legs_quote_str}"
    )

    def get_greek(t, name):
        if t and t.modelGreeks and getattr(t.modelGreeks, name, None) is not None:
            return getattr(t.modelGreeks, name)
        return 0.0

    # 組合總淨 Delta 與 Theta (依 long 或 short 方向計算淨部位暴露)
    if is_long:
        total_delta = (+ get_greek(tc_center, 'delta') + get_greek(tp_center, 'delta')
                       - get_greek(tc_wing, 'delta') - get_greek(tp_wing, 'delta'))
        total_theta = (+ get_greek(tc_center, 'theta') + get_greek(tp_center, 'theta')
                       - get_greek(tc_wing, 'theta') - get_greek(tp_wing, 'theta'))
    else:
        total_delta = (- get_greek(tc_center, 'delta') - get_greek(tp_center, 'delta')
                       + get_greek(tc_wing, 'delta') + get_greek(tp_wing, 'delta'))
        total_theta = (- get_greek(tc_center, 'theta') - get_greek(tp_center, 'theta')
                       + get_greek(tc_wing, 'theta') + get_greek(tp_wing, 'theta'))

    return {
        'symbol': underlying.symbol,
        'actual_underlying': actual_underlying,
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
        'bid_up': bid_up,
        'iron': iron,
        'is_long': is_long,
        'strategy_name': strategy_name,
        'center_action_desc': center_action_desc,
        'wing_action_desc': wing_action_desc,
        'legs_quote_str': legs_quote_str,
        'line_legs_quote_str': line_legs_quote_str,
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
    iron = legs.get('iron', 'long')
    is_long = legs.get('is_long', True)
    strategy_name = legs.get('strategy_name', 'Long Butterfly' if is_long else 'Short Butterfly')
    center_desc = legs.get('center_action_desc', '買入 (BUY)' if is_long else '賣出 (SELL)')
    wing_desc = legs.get('wing_action_desc', '賣出 (SELL)' if is_long else '買入 (BUY)')
    legs_quote_str = legs.get('legs_quote_str', '')
    line_legs_quote_str = legs.get('line_legs_quote_str', '')

    # 建立 BAG 組合單合約
    combo_contract = Contract(symbol=symbol, secType='BAG', currency='USD', exchange=exchange)

    # 4 腿定義：
    # 中心 ATM 2 腿為 BUY，價外上下翼為 SELL
    # 若 iron == 'long': 送出 BUY 訂單 (BUY*BUY=買中心, BUY*SELL=賣雙翼)
    # 若 iron == 'short': 送出 SELL 訂單 (SELL*BUY=賣中心, SELL*SELL=買雙翼)
    # 保持組合單為標準正限價，完全符合 CME/CBOE 交易所的正值限價規範 (避免 Error 201 負價被拒)
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
    bid_up = legs.get('bid_up')

    # 執行 bid_up 限價上限保護: min(即時查詢到的bid價格, bid_up)
    chosen_bid = raw_bid
    if bid_up is not None and float(bid_up) > 0:
        bid_up_val = float(bid_up)
        if raw_bid > 0:
            if raw_bid > bid_up_val:
                print(f"-> 🛡️ [價格上限保護] 即時查詢 Bid (${raw_bid}) 超過設定上限 bid_up (${bid_up_val})，依規則只下單上限價: ${bid_up_val}")
                chosen_bid = bid_up_val
            else:
                print(f"-> ℹ️ [價格檢驗正常] 即時查詢 Bid (${raw_bid}) <= bid_up (${bid_up_val})，採用即時價: ${raw_bid}")
                chosen_bid = raw_bid
        else:
            chosen_bid = min(0.05, bid_up_val)

    if chosen_bid > 0:
        limit_price = round_to_valid_tick(symbol, chosen_bid, ref_leg)
    else:
        # 若休市無買盤報價，依最小跳動點建立
        default_p = 0.05
        if bid_up is not None and float(bid_up) > 0:
            default_p = min(default_p, float(bid_up))
        limit_price = round_to_valid_tick(symbol, default_p, ref_leg)

    # 確保跳動點修整後不超過 bid_up (若有設定 bid_up)
    if bid_up is not None and float(bid_up) > 0 and limit_price > float(bid_up):
        limit_price = round_to_valid_tick(symbol, float(bid_up), ref_leg)

    limit_price = round(limit_price, 6)

    order_action = 'BUY' if is_long else 'SELL'
    action_chinese = '限價買入' if is_long else '限價賣出'

    # 建立 BID LIMIT 訂單 (iron='long' -> BUY 組合單; iron='short' -> SELL 組合單)
    order = LimitOrder(order_action, TRADE_QTY, limit_price)
    order.tif = 'DAY'

    # 即時查詢 trade/.env 中的 OP_SEND_WEBHOOK 開關與目標帳號
    env_cfg = load_env_config()
    send_live = str(env_cfg.get('OP_SEND_WEBHOOK', 'false')).strip().lower() in ('true', '1')
    if "--dry-run" in sys.argv:
        send_live = False
    target_acct = env_cfg.get('IB_TARGET_ACCOUNT', '').strip()
    if target_acct:
        order.account = target_acct

    actual_und = legs.get('actual_underlying')
    und_name = getattr(actual_und, 'localSymbol', symbol) if actual_und else symbol
    summary_str = (
        f"Butterfly 蝶式組合單 ({strategy_name}):\n"
        f"  標的代號: {symbol} (真實掛鉤: {und_name}, 市價: {legs['underlying_price']:.2f})\n"
        f"  策略方向 (iron): {iron.upper()} (中心 ATM: {center_desc} / 價外上下翼: {wing_desc})\n"
        f"  中心 ATM ({center_desc} Call & Put): {legs['center_strike']}\n"
        f"  上翼 ({wing_desc} Call): {legs['call_wing_strike']} (+{legs['wing_width']})\n"
        f"  下翼 ({wing_desc} Put) : {legs['put_wing_strike']} (-{legs['wing_width']})\n"
        f"  到期日: {legs['expiry']} (DTE: {legs['dte']} 天)\n"
        f"  四腿即時報價與 Greeks:\n"
        f"{legs_quote_str}\n"
        f"  下單方式: BID LIMIT ({action_chinese} {order_action} {TRADE_QTY} 口)\n"
        f"  委託限價: ${limit_price} (即時Bid: {raw_bid}, 上限bid_up: {bid_up if bid_up is not None else '無'})\n"
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
            reject_msg = (
                f"【IBKR 下單被拒絕警示】\n"
                f"商品: {symbol} (掛鉤: {und_name})\n"
                f"策略: {strategy_name} [{iron.upper()}]\n"
                f"錯誤代碼: {err_code}\n"
                f"錯誤訊息: {err_msg}\n"
                f"委託動作: {action_chinese} {order_action} (限價: ${limit_price})"
            )
            try:
                send_trade_notification(symbol, reject_msg, {"error_code": err_code, "error_msg": err_msg})
                print(f"-> 📲 已發送下單被拒絕警示至 LINE")
            except Exception as e:
                print(f"❌ [LINE 推播異常] {e}")
        elif trade.orderStatus.status in ('PreSubmitted', 'Submitted'):
            print(f"✅ [委託成功確認] IBKR 已成功接收並排入市場 (狀態: {trade.orderStatus.status}, 限價: {limit_price})")
            submit_msg = (
                f"【IBKR 下單成功通知】\n"
                f"商品: {symbol} (掛鉤: {und_name})\n"
                f"策略: {strategy_name} [{iron.upper()}]\n"
                f"方向: 中心 ATM {center_desc} / 價外上下翼 {wing_desc}\n"
                f"到期日: {legs['expiry']} (DTE: {legs['dte']} 天)\n"
                f"中心 ATM: {legs['center_strike']}\n"
                f"上翼 Call: {legs['call_wing_strike']} (+{legs['wing_width']})\n"
                f"下翼 Put : {legs['put_wing_strike']} (-{legs['wing_width']})\n"
                f"四腿報價:\n"
                f"{line_legs_quote_str}\n"
                f"委託方式: BID LIMIT ({action_chinese} {order_action} {TRADE_QTY} 口)\n"
                f"委託限價: ${limit_price} (即時Bid: {raw_bid}, 上限: {bid_up if bid_up is not None else '無'})\n"
                f"排單狀態: {trade.orderStatus.status}\n"
                f"淨 Delta: {legs['total_delta']:+.3f} | 淨 Theta: {legs['total_theta']:.2f}"
            )
            payload = {
                "symbol": symbol,
                "iron": iron,
                "action": f"{order_action}_BUTTERFLY_SUBMITTED",
                "quantity": TRADE_QTY,
                "price": limit_price,
                "bid_up": bid_up,
                "raw_bid": raw_bid,
                "status": trade.orderStatus.status,
                "order_id": trade.order.orderId,
                "center_strike": legs['center_strike'],
                "call_wing": legs['call_wing_strike'],
                "put_wing": legs['put_wing_strike'],
                "expiry": legs['expiry'],
                "dte": legs['dte'],
                "net_delta": round(legs['total_delta'], 3),
                "net_theta": round(legs['total_theta'], 2),
            }
            try:
                send_trade_notification(symbol, submit_msg, payload)
                print(f"-> 📲 已發送 IBKR 下單成功推播至 LINE")
            except Exception as e:
                print(f"❌ [LINE 推播異常] {e}")
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
            print(f"=== [成交確認] {symbol} {strategy_name} 已成交，平均價格: {fill_price} ===")
            fill_msg = (
                f"【IBKR 成交確認通知】\n"
                f"商品: {symbol} (掛鉤: {und_name})\n"
                f"策略: {strategy_name} [{iron.upper()}]\n"
                f"狀態: 完全成交 (Filled)\n"
                f"成交均價: ${fill_price}\n"
                f"成交動作: {action_chinese} {order_action} {TRADE_QTY} 口\n"
                f"中心 ATM: {legs['center_strike']} / 上翼: {legs['call_wing_strike']} / 下翼: {legs['put_wing_strike']}"
            )
            fill_payload = {
                "symbol": symbol,
                "iron": iron,
                "action": f"{order_action}_BUTTERFLY_FILLED",
                "quantity": TRADE_QTY,
                "price": fill_price,
                "status": "Filled",
                "order_id": trade.order.orderId,
            }
            try:
                send_trade_notification(symbol, fill_msg, fill_payload)
                print(f"-> 📲 已發送 IBKR 成交確認推播至 LINE")
            except Exception as e:
                print(f"❌ [LINE 推播異常] {e}")
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

        # 個別商品買入限價上限 BID_UP
        bid_up = g_info.get('bid_up')

        # 個別商品策略方向 IRON (long vs short)
        iron = str(g_info.get('iron', 'long')).strip().lower()

        underlying, opt_class, chains = resolve_group_symbols(g_name, symbols)
        if not underlying:
            print(f"[略過] 無法解析 {g_name} 的期貨/指數與期權結構。")
            continue

        # 檢查是否已持有該商品期權部位 (包含 FOP 與 OPT)
        current_pos = [p for p in ib.portfolio() if p.contract.symbol == underlying.symbol and p.contract.secType in ['FOP', 'OPT']]
        if not current_pos:
            dte_label = "0DTE" if min_dte == 0 else f"{min_dte}~{max_dte}天"
            print(f"-> 準備為 {underlying.symbol} (Class: {opt_class}) 建立 Butterfly 部位 (方向: {iron.upper()}, DTE: {dte_label}, 翼寬: {wing_width or '預設'}, bid_up: {bid_up or '預設'})...")
            legs = get_butterfly_legs(
                underlying=underlying,
                opt_class=opt_class,
                chains=chains,
                min_dte=min_dte,
                max_dte=max_dte,
                wing_width=wing_width,
                bid_up=bid_up,
                iron=iron
            )
            if legs:
                execute_butterfly(legs)
        else:
            print(f"-> {underlying.symbol} 已有期權部位 ({len(current_pos)} 筆)，跳過重複建倉。")


if __name__ == '__main__':
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Butterfly 跨期權自動建倉與對沖執行腳本")
    parser.add_argument("--dry-run", action="store_true", help="以模擬模式執行下單（不實際送單至 IBKR）")
    parser.add_argument("--no-line", action="store_true", help="不發送 LINE 通知")
    args = parser.parse_args()

    if args.dry_run:
        print("💡 [DRY-RUN 模擬模式啟動] OP_SEND_WEBHOOK=False，不實際向 IBKR 送單。")

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
    if not args.dry_run and sys.stdin and hasattr(sys.stdin, 'isatty') and sys.stdin.isatty():
        try:
            time.sleep(60 * 60 * 20)
        except KeyboardInterrupt:
            pass

