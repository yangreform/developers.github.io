
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
    quantity = int(data.get('quantity', 1))
    tv_price = data.get('price')

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

            # ======== 加入 message 欄位供 GAS 擷取 ========
            return jsonify({
                'status': result_status,
                'shioaji': {
                    'result': sj_status,
                    'filled': filled_qty,
                    'remaining': remain_qty,
                    'avg_price': avg_price,
                },
                'message': f'這次有下單，{msg}'
            }), 200
            
        except Exception as e:
            logger.error(f"國內下單發生異常: {e}")
            return jsonify({'status': 'error', 'message': '這次有下單，但下單失敗', 'error': str(e)}), 200

    else:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        ib = IB()
        try:
            client_id = random.randint(100, 999) 
            ib.connect(IB_HOST, IB_PORT, clientId=client_id, timeout=10)

            # 2. 建立合約別名轉換表 (Symbol Alias Mapping)
            # 確保 Webhook 傳來的代號 (Key) 能對應到 IBKR 正確的 Symbol (Value)
            # ⚠️ 必須在庫存檢查前先做轉換，否則 XC→YC 後查不到部位
            alias_map = {
                "MNG": "MHNG",   # 天然氣代碼為 NG
                "XC": "YC"       # 玉米 mini 在 IBKR 的 symbol 為 YC
            }
            # 轉換代碼（後續查庫存、建合約都使用 actual_symbol）
            actual_symbol = alias_map.get(symbol.upper(), symbol.upper())

            # ======== 1. 檢查特定商品與庫存狀況 ========
            CHECK_SYMBOLS = ['TMF','1OZ','MBT','MJY','M6E','MHNG','MHG','M2K','MES','MNQ']    #long only
            #CHECK_SYMBOLS = ['VXM']    #add
            if symbol in CHECK_SYMBOLS:
                current_pos = get_current_position(ib, actual_symbol)
                logger.info(f"{symbol}({actual_symbol}) 目前庫存口數: {current_pos}")
                if action == 'BUY' and current_pos >= -1:
                    ib.disconnect()
                    return jsonify({'status': 'skip', 'message': '庫存為-1，這次不下單'}), 200
            CHECK_SYMBOLS = ['XC','MCL']    #long only
            if symbol in CHECK_SYMBOLS:
                # XC 在 IBKR 實際 symbol 為 YC，需用 actual_symbol 查詢
                current_pos = get_current_position(ib, actual_symbol)
                logger.info(f"{symbol}({actual_symbol}) 目前庫存口數: {current_pos}")
                if action == 'SELL' and current_pos <= 1:
                    ib.disconnect()
                    return jsonify({'status': 'skip', 'message': '庫存為+1，這次不下單'}), 200
            # ============================================
            # 2. 建立合約
            # 定義各期貨商品的交易所對應表
            exchange_map = {
                "MBT": "CME", "M2K": "CME", "MES": "CME", "MNQ": "CME", "M6E": "CME", "MJY": "CME",
                "MHG": "COMEX", "1OZ": "COMEX", "MGC": "COMEX",
                "MHNG": "NYMEX", "MCL": "NYMEX",
                "MYM": "CBOT", "YC": "CBOT",
                "VXM": "CFE"
            }

            # 3. 修正：將 expiry_map 改為支援條件邏輯 (支援 BUY/SELL 分別指定不同月份)
            # 將邏輯封裝在一個函數中，或者直接在這裡判斷
            def get_target_expiry(sym, act):
                # 預設對照表
                mapping = {
                    "MBT": "202608", "VXM": "202608",
                    "1OZ": "202608", "MGC": "202608", "MHNG": "202609", "MNG": "202609", "MCL": "202609",
                    "MNQ": "202609", "MES": "202609", "M2K": "202609", "M6E": "202609", "MJY": "202609", "MYM": "202609", "MHG": "202609", "YC": "202609"
                }
                
                # 特殊邏輯：MNQ 轉倉規則
                #if sym == "MNQ":
                #    return "202606" if act == "BUY" else "202609"
                #if sym == "M2K":
                #    return "202606" if act == "BUY" else "202609"
                #if sym == "M6E":
                #    return "202609" if act == "BUY" else "202606"
                #if sym == "MJY":
                #    return "202609" if act == "BUY" else "202606"

                #if sym == "MHG":
                #    return "202607" if act == "BUY" else "202609"

                #if sym == "MBT":
                #    return "202607" if act == "BUY" else "202608"
                #if sym == "VXM":
                #    return "202608" if act == "BUY" else "202607"
                #if sym == "MHNG":
                #    return "202609" if act == "BUY" else "202608"
                
                return mapping.get(sym, "202609") # 若無對應則預設 202609

            if actual_symbol in exchange_map:
                target_exchange = exchange_map.get(actual_symbol, "CME")
                target_expiry = get_target_expiry(actual_symbol, action.upper())
                
                # 使用轉換後的 actual_symbol 與動態邏輯計算出的 target_expiry
                contract = Future(actual_symbol, target_expiry, target_exchange, currency='USD')
            else:
                contract = Stock(symbol, 'SMART', 'USD')
            
            ib.qualifyContracts(contract)

            # 獲取合約的最小跳動點 (minTick) 以避免報價不符規範 (Warning 110)
            details = ib.reqContractDetails(contract)
            min_tick = details[0].minTick if details and getattr(details[0], 'minTick', 0) > 0 else 0.01

            # 3. 獲取市價
            mkt_price = float(tv_price)

            # 使用 isinstance 檢查 contract 是 Future 還是 Stock，這比依賴變數更安全
            is_future = isinstance(contract, Future)

            # 4. 訂單邏輯
            is_rth = is_regular_trading_hours()

            if not mkt_price or mkt_price <= 0:
                raise Exception("無法獲取市價，無法計算 Adaptive Algo 的限價天花板")

            # 統一計算限價天花板 (買單 +1%，賣單 -1%)，並根據 minTick 四捨五入
            raw_limit_price = mkt_price * (1.01 if action == 'BUY' else 0.99)
            limit_price = round(round(raw_limit_price / min_tick) * min_tick, 5)

            # 建立限價單
            order = LimitOrder(action, quantity, limit_price)
            order.outsideRth = True

            # 🌟 2. 掛上 IBKR Adaptive Algo 外掛 (魔法在這裡)
            # ==========================================
            order.algoStrategy = 'Adaptive'
            order.algoParams = [TagValue('adaptivePriority', 'Normal')]

            # CBOT 農產品期貨（YC=玉米）在美股正規時段外
            CBOT_FUTURES = {'YC', 'MYM'}  # CBOT 交易所的期貨品種
            
            if is_future:
                if not is_rth and actual_symbol in CBOT_FUTURES:
                    order.tif = 'GTC'
                    logger.info(f"[CBOT盤外] 使用 GTC LimitOrder @ {limit_price}")
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
            
            end_time = time.time() + 15  # Adaptive Algo 需要較多時間，延長至 5 秒
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

            # ======== 加入 message 欄位供 GAS 擷取 ========
            return jsonify({
                'status': result_status,
                'ib': {
                    'result': ib_status,
                    'filled': filled_qty,
                    'remaining': remain_qty,
                    'avg_price': avg_price,
                },
                'message': f'這次有下單，{msg}',
            }), 200

        except Exception as e:
            logger.error(f"IB 下單發生異常: {e}")
            # ======== 下單失敗回傳指定訊息 ========
            return jsonify({'status': 'error', 'message': '這次有下單，但下單失敗', 'error': str(e)}), 200
        finally:
            if ib.isConnected():
                ib.sleep(0.1)
                ib.disconnect()


if __name__ == '__main__':
    from waitress import serve
    print(f"📈 IBKR 交易核心已啟動於獨立 Port {MAIN_PORT}")
    # 獨立運行，不再依賴 router.py 的啟動
    serve(app, host='0.0.0.0', port=MAIN_PORT, threads=4)
