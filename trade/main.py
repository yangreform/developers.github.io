
import threading
import os
import time
import datetime
import calendar
import logging
import asyncio
import random
import json
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# 引入 IB
from ib_insync import *
from notifier import send_push_message, send_trade_notification

load_dotenv()

# ================= .env 讀取工具 =================
def env_str(name, default=None, required=False):
    val = os.getenv(name, default)
    if isinstance(val, str):
        val = val.strip()
    if required and (val is None or val == ""):
        raise RuntimeError(f".env 缺少必要參數: {name}")
    return val

def env_int(name, default=None):
    val = os.getenv(name)
    return int(val) if val not in (None, "") else default

def env_float(name, default=None):
    val = os.getenv(name)
    return float(val) if val not in (None, "") else default

def env_json(name, default):
    """讀取 .env 內的 JSON 字串（例如 alias_map/exchange_map），格式錯誤時退回預設值。"""
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"⚠️ .env 的 {name} JSON 格式錯誤，改用內建預設值: {e}")
        return default

# ================= 設定區（全部改由 .env 讀取） =================
IB_HOST = env_str("IB_HOST", "127.0.0.1")
IB_PORT = env_int("IB_PORT", 4001)
MAIN_IB_CLIENT_ID = env_int("MAIN_IB_CLIENT_ID", 100)  # 目前實際下單用 random client id，此值保留供未來使用
WEBHOOK_PASSPHRASE = env_str("WEBHOOK_PASSPHRASE", "")
TARGET_ACCOUNT = env_str("MAIN_TARGET_ACCOUNT", "")  # 若有多個帳戶，請在 .env 填寫目標帳戶代碼，留空則不過濾帳戶
MAIN_PORT = env_int("MAIN_PORT", 5500)
MAIN_TMF_ORDER_PRICE = env_float("MAIN_TMF_ORDER_PRICE", 14400)

KGI_USER = env_str("KGI_USER", "")
KGI_PASS = env_str("KGI_PASS", "")

# ==========================================
import shioaji as sj

SHIOAJI_API_KEY = env_str("SHIOAJI_API_KEY", required=True)
SHIOAJI_SECRET_KEY = env_str("SHIOAJI_SECRET_KEY", required=True)
SHIOAJI_CA_PATH = env_str("SHIOAJI_CA_PATH", "Sinopac.pfx")
SHIOAJI_CA_PASSWD = env_str("SHIOAJI_CA_PASSWD", required=True)

api = sj.Shioaji()
accounts = api.login(SHIOAJI_API_KEY, SHIOAJI_SECRET_KEY)
api.activate_ca(
    ca_path=SHIOAJI_CA_PATH,
    ca_passwd=SHIOAJI_CA_PASSWD,
)
# ==========================================

current_time = datetime.datetime.now().strftime("%m%d_%H%M") 
LOG_FILENAME = f"trading_{current_time}.log" 

print(f"📁 本次 Log 檔名: {LOG_FILENAME}")

# ==========================================
# 2. 日誌設定
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILENAME, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==============================================================================
# 🌟 全期貨商品動態尋找最近可用合約 (自動滾倉/到期檢測)
# ==============================================================================
FUTURE_EXCHANGE_MAP = {
    "MNQ": "CME", "NQ": "CME",
    "MES": "CME", "ES": "CME",
    "M2K": "CME", "RTY": "CME",
    "M6E": "CME", "EUR": "CME",
    "MJY": "CME", "JPY": "CME",
    "MBT": "CME", "BTC": "CME",
    "MCL": "NYMEX", "CL": "NYMEX",
    "MHNG": "NYMEX", "MNG": "NYMEX", "NG": "NYMEX", "LN": "NYMEX",
    "MHG": "COMEX", "HG": "COMEX",
    "MGC": "COMEX", "GC": "COMEX",
    "YC": "CBOT", "XC": "CBOT", "ZC": "CBOT",
    "VXM": "CFE",
}

FUTURE_SYMBOL_ALIAS = {
    "XC": "YC",
    "MNG": "MHNG",
}

FUTURE_CONTRACT_CACHE = {}


def is_unexpired(exp_str: str, today_str: str) -> bool:
    if not exp_str:
        return False
    clean_exp = str(exp_str).strip()
    if len(clean_exp) >= 8:
        return clean_exp[:8] >= today_str
    if len(clean_exp) == 6:
        return clean_exp >= today_str[:6]
    return clean_exp >= today_str


def get_target_future_contract(ib_instance, symbol: str):
    """
    自動搜尋並回傳該商品最近可以下單的正確合法期貨合約。
    支援自動過濾已到期合約、按到期日排序取最近月，並自動進行合約資格化 (qualifyContracts)。
    """
    if not ib_instance or not ib_instance.isConnected():
        return None

    actual_symbol = FUTURE_SYMBOL_ALIAS.get(symbol.upper(), symbol.upper())
    exchange = FUTURE_EXCHANGE_MAP.get(actual_symbol, "CME")
    today_str = datetime.date.today().strftime('%Y%m%d')
    now_ts = time.time()

    cache_key = f"{actual_symbol}_{exchange}"
    cached = FUTURE_CONTRACT_CACHE.get(cache_key)
    if cached:
        contract, cached_time = cached
        if (now_ts - cached_time < 1800) and is_unexpired(contract.lastTradeDateOrContractMonth, today_str):
            return contract

    details = []
    try:
        details = ib_instance.reqContractDetails(Future(symbol=actual_symbol, exchange=exchange, currency='USD'))
    except Exception:
        pass

    if not details:
        try:
            details = ib_instance.reqContractDetails(Future(symbol=actual_symbol, exchange=exchange))
        except Exception:
            pass

    if not details:
        try:
            details = ib_instance.reqContractDetails(Future(symbol=actual_symbol))
        except Exception:
            pass

    valid_details = [
        d for d in details
        if d.contract.exchange not in ['QBALGO', 'SMART']
        and is_unexpired(d.contract.lastTradeDateOrContractMonth, today_str)
    ]

    if not valid_details:
        logger.warning(f"⚠️ [期貨合約搜尋] 找不到 {symbol} ({actual_symbol} @ {exchange}) 的可用近月合約！")
        return None

    # 按到期月份升冪排序，取得最接近可下單的有效合約
    valid_details = sorted(valid_details, key=lambda d: d.contract.lastTradeDateOrContractMonth)
    target_contract = valid_details[0].contract
    try:
        ib_instance.qualifyContracts(target_contract)
    except Exception as q_err:
        logger.warning(f"⚠️ [期貨合約資格化異常] {target_contract.symbol}: {q_err}")

    FUTURE_CONTRACT_CACHE[cache_key] = (target_contract, now_ts)
    return target_contract

app = Flask(__name__)

# --- 新增的部位檢查函式 ---
def get_current_position(ib, symbol):
    """ 取得指定商品的目前部位 (口數) """
    ib.reqPositions()
    ib.sleep(0.5) 
    positions = ib.positions()
    
    # 如果有指定帳戶就過濾
    if TARGET_ACCOUNT:
        positions = [p for p in positions if p.account == TARGET_ACCOUNT]
        
    for p in positions:
        if p.contract.symbol == symbol:
            return p.position
    return 0
# ------------------------

def get_futures_code(prefix="TMF"):
    """ 自動生成月份合約代碼 (結算日當天 11:00 AM 精準換月) """
    now = datetime.datetime.now()
    month_map = "ABCDEFGHIJKL"

    # 找出該月第三個星期三的日期
    c = calendar.monthcalendar(now.year, now.month)
    wed_dates = [week[calendar.WEDNESDAY] for week in c if week[calendar.WEDNESDAY] != 0]
    third_wednesday = wed_dates[2]
    
    # 執行精準換月邏輯：
    # 條件 1: 日期已經超過結算日 (大於第三個星期三)
    # 條件 2: 今天剛好是結算日，且時間已經過了 11:00 AM (含)
    if now.day > third_wednesday or (now.day == third_wednesday and now.hour >= 11):
        # 換到下個月
        target_month = now.month + 1
        target_year = now.year
        
        # 跨年防呆：如果現在是 12 月結算日之後，下個月是明年的 1 月
        if target_month > 12:
            target_month = 1
            target_year += 1
            
        month_char = month_map[target_month - 1]
        year_char = str(target_year)[-1]
    else:
        # 維持本月
        month_char = month_map[now.month - 1]
        year_char = str(now.year)[-1]

    return f"{prefix}{month_char}{year_char}"

import zoneinfo # Python 3.9+ 內建，專門處理標準時區
def is_regular_trading_hours():
    """ 判斷目前是否為美股正規交易時段 (自動處理夏令/冬令時間) """
    # 1. 取得絕對的 UTC 時間
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    
    # 2. 轉換為美東時間 (紐約) - 會自動判定現在是 EDT (UTC-4) 還是 EST (UTC-5)
    ny_tz = zoneinfo.ZoneInfo("America/New_York")
    now_ny = now_utc.astimezone(ny_tz)
    
    # 3. 週六(5)週日(6)不開市
    if now_ny.weekday() >= 5:
        return False
        
    # 4. 設定當天的開盤與收盤時間界線
    start_time = now_ny.replace(hour=9, minute=30, second=0, microsecond=0)
    end_time = now_ny.replace(hour=16, minute=0, second=0, microsecond=0)
    
    # 5. 判斷現在是否在開盤區間內
    return start_time <= now_ny <= end_time

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    if not data or data.get('passphrase') != WEBHOOK_PASSPHRASE:
        return "Unauthorized", 401
    
    logger.info(f"=== 收到 Webhook 訊號: {data} ===")

    symbol = data.get('symbol')
    action = data.get('action', '').upper()
    quantity = int(float(data.get('quantity', 1)))
    tv_price = data.get('price')
    strategy_name = data.get('strategy_name', '')

    if strategy_name == 'iron_condor':
        logger.info(f"[{symbol}] Iron Condor 訊號已由 option.py 獨立執行，此處僅作記錄不重複下單。")
        return jsonify({'status': 'success', 'message': f'{symbol} 已下單完畢，單純通知。 (Iron Condor {action})'}), 200

    if symbol == 'TMF': # 假設 I 代表國內期貨
        if not api:
            return jsonify({'status': 'error', 'message': 'API_NOT_CONNECTED'}), 200

        try:
            target_code = get_futures_code("TMF")
            contract = api.Contracts.Futures.TMF[target_code]

            if contract is None:
                return jsonify({'status': 'error', 'message': '合約找不到'}), 200
            
            # ======== 1. 檢查特定商品與庫存狀況 ========
            current_pos = 0
            positions = api.list_positions(api.futopt_account)
            for p in positions:
                if p.code == target_code:
                    if p.direction == sj.constant.Action.Buy:
                        current_pos += p.quantity
                    elif p.direction == sj.constant.Action.Sell:
                        current_pos -= p.quantity
            logger.info(f"{target_code} 目前國內庫存口數: {current_pos}")
            if action.upper() == 'SELL' and current_pos <= 1:
                #return jsonify({'status': 'skip', 'message': '庫存為+1，這次不下單'}), 200
                pass

            # ============================================

            sj_action = sj.constant.Action.Buy if action.upper() == 'BUY' else sj.constant.Action.Sell
            
            order = api.Order(
                action=sj_action,
                price=MAIN_TMF_ORDER_PRICE, 
                quantity=quantity,
                price_type=sj.constant.FuturesPriceType.MKT,
                order_type=sj.constant.OrderType.IOC,
                octype=sj.constant.FuturesOCType.Auto,
                account=api.futopt_account
            )
            
            trade = api.place_order(contract, order)
            logger.info(f"送信國內委託: {target_code} {action} {quantity}口")
            
            # 等待 Shioaji 委託狀態更新 (IOC 單通常很快)
            end_time = time.time() + 3
            while trade.status.status.name in ['PendingSubmit', 'Submitted', 'PreSubmitted'] and time.time() < end_time:
                time.sleep(0.1)

            sj_status = trade.status.status.name
            
            # 從 deals 中計算成交量與均價
            filled_qty = sum(d.quantity for d in trade.status.deals)
            remain_qty = quantity - filled_qty
            
            if filled_qty > 0:
                avg_price = sum(d.price * d.quantity for d in trade.status.deals) / filled_qty
            else:
                avg_price = 0.0

            # ======== IOC 三種結果判斷 ========
            if sj_status == 'Filled' or (filled_qty == quantity):
                msg = f'全數成交 {filled_qty}口 @ {avg_price:.2f}'
                result_status = 'success'
            elif sj_status in ('Cancelled', 'Inactive', 'Failed') and filled_qty > 0:
                msg = f'部分成交 {filled_qty}/{quantity}口 @ {avg_price:.2f}，剩餘{remain_qty}口因 IOC 取消'
                result_status = 'partial'
            else:
                msg = f'完全未成交，狀態: {sj_status}'
                result_status = 'cancelled'

            logger.info(f"Shioaji 國內下單結果: {msg}")

            res_msg = f'這次有下單，{msg}'
            try:
                send_trade_notification(symbol, res_msg, data)
            except Exception as notify_err:
                logger.error(f"LINE 推播發送失敗: {notify_err}")

            return jsonify({
                'status': result_status,
                'shioaji': {
                    'result': sj_status,
                    'filled': filled_qty,
                    'remaining': remain_qty,
                    'avg_price': avg_price,
                },
                'message': res_msg
            }), 200
            
        except Exception as e:
            logger.error(f"國內下單發生異常: {e}")
            err_msg = '這次有下單，但下單失敗'
            try:
                send_trade_notification(symbol, f"{err_msg}: {e}", data)
            except Exception:
                pass
            return jsonify({'status': 'error', 'message': err_msg, 'error': str(e)}), 200

    else:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        ib = IB()
        try:
            client_id = random.randint(100, 999) 
            ib.connect(IB_HOST, IB_PORT, clientId=client_id, timeout=10)

            # 2. 建立合約別名轉換表 (Symbol Alias Mapping)
            # 確保 Webhook 傳來的代號 (Key) 能對應到 IBKR 正確的 Symbol (Value)
            alias_map = {
                "MNG": "MHNG",   # 天然氣代碼為 NG
                "XC": "YC"       # 玉米 mini 在 IBKR 的 symbol 為 YC
            }
            # 轉換代碼（後續查庫存、建合約都使用 actual_symbol）
            actual_symbol = alias_map.get(symbol.upper(), symbol.upper())

            # ======== 1. 檢查特定商品與庫存狀況 ========
            # ============================================
            # 2. 建立合約 (優先支援傳入 conId 或 expiry，若無則自動動態查詢最近可用期貨合約)
            exchange_map = {
                "MBT": "CME", "MES": "CME", "MNQ": "CME", "M6E": "CME", "MJY": "CME",
                "MHG": "COMEX", "MGC": "COMEX",
                "MHNG": "NYMEX", "MCL": "NYMEX",
                "YC": "CBOT", "VXM": "CFE"
            }

            contract = None
            if 'conId' in data and int(data['conId']) > 0:
                contract = Contract(conId=int(data['conId']))
                ib.qualifyContracts(contract)
                logger.info(f"使用傳入 conId 建立合約: conId={data['conId']}, localSymbol={getattr(contract, 'localSymbol', '')}")
            elif 'expiry' in data and data['expiry']:
                target_exchange = FUTURE_EXCHANGE_MAP.get(actual_symbol, exchange_map.get(actual_symbol, "CME"))
                contract = Future(actual_symbol, str(data['expiry']), target_exchange, currency='USD')
                ib.qualifyContracts(contract)
                logger.info(f"使用傳入 expiry 建立合約: {actual_symbol} @ {target_exchange} (expiry={data['expiry']})")
            elif actual_symbol in exchange_map or actual_symbol in FUTURE_EXCHANGE_MAP:
                # 動態搜尋最近可以下單的正確期貨合約
                contract = get_target_future_contract(ib, actual_symbol)
                if not contract:
                    raise Exception(f"無法自動取得 {actual_symbol} 的有效近月期貨合約")
                logger.info(f"動態鎖定最近期貨合約: {contract.symbol} {contract.localSymbol} (expiry={contract.lastTradeDateOrContractMonth}, conId={contract.conId})")
            else:
                contract = Stock(symbol, 'SMART', 'USD')
                ib.qualifyContracts(contract)

            # 獲取合約的最小跳動點 (minTick) 以避免報價不符規範 (Warning 110)
            details = ib.reqContractDetails(contract)
            min_tick = details[0].minTick if details and getattr(details[0], 'minTick', 0) > 0 else 0.01

            # 針對部分 CBOT/COMEX 商品，IB 回報的 minTick (可能因為單位問題) 與實際報價小數點位數不同，這裡進行覆寫
            TICK_OVERRIDES = {
                'YC': 0.125,     # 玉米 Mini (跳動點 1/8 = 0.125)
                'ZC': 0.25,      # 玉米 (跳動點 1/4 = 0.25)
                'MGC': 0.1,      # 微型黃金
                'MES': 0.25,     # 微型 SP500
                'MNQ': 0.25,     # 微型 Nasdaq
                'MCL': 0.01,     # 微型原油
                'ZC': 0.25,      # 玉米 (跳動點 1/4 = 0.25)
            }
            if actual_symbol in TICK_OVERRIDES:
                min_tick = TICK_OVERRIDES[actual_symbol]

            # 3. 獲取市價
            mkt_price = float(tv_price)

            # 使用 isinstance 檢查 contract 是 Future 還是 Stock，這比依賴變數更安全
            is_future = isinstance(contract, Future)

            # 4. 訂單邏輯
            is_rth = is_regular_trading_hours()

            if not mkt_price or mkt_price <= 0:
                logger.info(f"[{symbol}] Webhook 傳入市價為 0，嘗試透過 IB 即時取得報價...")
                ticker = ib.reqMktData(contract, "", False, False)
                ib.sleep(1.5)
                
                if ticker.last and ticker.last > 0:
                    mkt_price = ticker.last
                elif ticker.ask and ticker.ask > 0 and action == 'BUY':
                    mkt_price = ticker.ask
                elif ticker.bid and ticker.bid > 0 and action == 'SELL':
                    mkt_price = ticker.bid
                elif ticker.close and ticker.close > 0:
                    mkt_price = ticker.close
                
                ib.cancelMktData(contract)
                
                if not mkt_price or mkt_price <= 0:
                    raise Exception("無法獲取市價，無法計算 Adaptive Algo 的限價天花板")
                logger.info(f"[{symbol}] 成功取得 IB 即時市價: {mkt_price}")

            # 統一計算限價天花板 (買單 +1%，賣單 -1%)，並根據 minTick 四捨五入
            raw_limit_price = mkt_price * (1.01 if action == 'BUY' else 0.99)
            limit_price = round(round(raw_limit_price / min_tick) * min_tick, 5)

            # 建立市價單
            order = MarketOrder(action, quantity)
            order.outsideRth = True

            # ==========================================
            # 核心 2. 套用 IBKR Adaptive Algo 演算法
            # ==========================================
            # 盤前盤後不支援 Adaptive Algo 時退回一般市價單
            if not is_future and not is_rth:
                logger.info(f"[{symbol}] 美股盤外時段不支援 Adaptive Algo，使用一般市價單")
            else:
                order.algoStrategy = 'Adaptive'
                order.algoParams = [TagValue('adaptivePriority', 'Patient')]

            CBOT_FUTURES = {'YC', 'ZC'}
            if is_future:
                if not is_rth and actual_symbol in CBOT_FUTURES:
                    order.tif = 'GTC'
                    logger.info(f"[CBOT盤外] 使用 GTC MarketOrder")
                else:
                    # Adaptive Algo 不適合 IOC，改用 GTC
                    order.tif = 'GTC'
            else:
                # 股票/其他邏輯
                if is_rth:
                    order.tif = 'DAY'
                else:
                    order.tif = 'GTC'

            # 5. 執行與確認
            trade = ib.placeOrder(contract, order)
            
            end_time = time.time() + 5  # Adaptive Algo 需要較多時間，延長至 5 秒
            while not trade.isDone() and time.time() < end_time:
                ib.sleep(0.5)
            
            ib_status   = trade.orderStatus.status
            filled_qty  = trade.orderStatus.filled      # 實際成交口數
            remain_qty  = trade.orderStatus.remaining   # 未成交口數
            avg_price   = trade.orderStatus.avgFillPrice

            # ======== 委託結果判斷 ========
            error_msg = ""
            for log_entry in trade.log:
                if getattr(log_entry, 'errorCode', 0) != 0 or 'Error' in getattr(log_entry, 'message', '') or 'rejected' in getattr(log_entry, 'message', '').lower():
                    error_msg = getattr(log_entry, 'message', '').replace('<br>', ' ')
                    break

            if ib_status == 'Filled':
                msg = f'全數成交 {filled_qty}口 @ {avg_price}'
                result_status = 'success'
            elif ib_status in ('Submitted', 'PreSubmitted'):
                msg = f'委託運作中 (Adaptive Algo)，目前狀態: {ib_status}，已成交 {filled_qty}口'
                result_status = 'submitted'
            elif ib_status in ('Cancelled', 'Inactive') and filled_qty > 0:
                msg = f'部分成交 {filled_qty}/{int(filled_qty + remain_qty)}口 @ {avg_price}，剩餘{remain_qty}口因故取消'
                result_status = 'partial'
            else:
                if error_msg:
                    msg = f'完全未成交，發生錯誤: {error_msg}'
                else:
                    msg = f'完全未成交，狀態: {ib_status}（市場未開盤或流動性不足）'
                result_status = 'cancelled'

            logger.info(f"IB 下單結果: {msg}")

            res_msg = f'這次有下單，{msg}'
            try:
                send_trade_notification(symbol, res_msg, data)
            except Exception as notify_err:
                logger.error(f"LINE 推播發送失敗: {notify_err}")

            return jsonify({
                'status': result_status,
                'ib': {
                    'result': ib_status,
                    'filled': filled_qty,
                    'remaining': remain_qty,
                    'avg_price': avg_price,
                },
                'message': res_msg,
            }), 200

        except Exception as e:
            logger.error(f"IB 下單發生異常: {e}")
            err_msg = '這次有下單，但下單失敗'
            try:
                send_trade_notification(symbol, f"{err_msg}: {e}", data)
            except Exception:
                pass
            return jsonify({'status': 'error', 'message': err_msg, 'error': str(e)}), 200
        finally:
            if ib.isConnected():
                ib.sleep(0.1)
                ib.disconnect()


if __name__ == '__main__':
    from waitress import serve
    print(f"📈 IBKR 交易核心已啟動於獨立 Port {MAIN_PORT}")
    # 獨立運行，不再依賴 router.py 的啟動
    serve(app, host='0.0.0.0', port=MAIN_PORT, threads=4)
