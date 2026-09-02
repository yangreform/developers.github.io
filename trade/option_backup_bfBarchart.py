import os
import json
import datetime
import time
import requests
import pandas as pd
from ib_insync import *

# ==============================================================================
# 0. 讀取 op.env 設定
# ==============================================================================
ENV_PATH = os.path.join(os.path.dirname(__file__), 'op.env')
env_config = {}
if os.path.exists(ENV_PATH):
    with open(ENV_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                k, v = line.split('=', 1)
                env_config[k.strip()] = v.strip().strip("'").strip('"')

# ==============================================================================
# 1. 實盤交易參數與連線設定
# ==============================================================================
IB_HOST = env_config.get('IB_HOST', '127.0.0.1')
IB_PORT = int(env_config.get('IB_PORT', 4001))
CLIENT_ID = int(env_config.get('IB_CLIENT_ID', 100)) + 6 

SEND_WEBHOOK = str(env_config.get('SEND_WEBHOOK', 'false')).lower() == 'true'
WEBHOOK_URL = env_config.get('WEBHOOK_URL', '')
WEBHOOK_PASSPHRASE = env_config.get('WEBHOOK_PASSPHRASE', '')

hedge_config = {}
try:
    hedge_config = json.loads(env_config.get('HEDGE_CONFIG_JSON', '{}'))
except Exception as e:
    print(f"[錯誤] 解析 HEDGE_CONFIG_JSON 失敗: {e}")

# 動態目標商品映射表 (Option Trading Class -> Underlying Future Symbol)
FUTURE_SYMBOL_MAP = {
    'ZC': 'ZC', 'OZC': 'ZC', 'OCD': 'ZC',
    'JP': 'JPY', 'JPU': 'JPY', '6J': 'JPY', 'JPY': 'JPY',
    'EU': 'EUR', 'EUU': 'EUR', '6E': 'EUR', 'EUR': 'EUR',
    'HG': 'HG', 'HXE': 'HG', 'H1W': 'HG', 'H2W': 'HG', 'H3W': 'HG', 'H4W': 'HG', 'H1T': 'HG',
    'ES': 'ES', 'EWN': 'ES', 'EW1': 'ES', 'EWQ': 'ES', 'ESU': 'ES', 'EW': 'ES',
    'NQ': 'NQ', 'QN': 'NQ',
    'GC': 'GC', 'OG': 'GC',
    'MNQ': 'MNQ', 'MES': 'MES', 'MGC': 'MGC', 'MCL': 'MCL',
    'RTY': 'RTY', 'RTO': 'RTY',
    'NG': 'NG', 'LN': 'NG'
}

TARGET_GROUPS = [] 

ENTRY_DTE = 45          
EXIT_DTE = 21           
DELTA_TARGET = 0.16     
TRADE_QTY = 1           

# 動態翼寬設定 (目標間距，最終會尋找最接近的合法履約價)
WING_WIDTH_MAP = {
    'ZC': 40,     
    'MNQ': 200,    
    'NQ': 200,
    'ES': 200,
    'JPY': 0.0020,
    'EUR': 0.04,
    'HG': 0.20,
    'GC': 80,
    'RTY': 50,
    'NG': 0.20
}

ib = IB()

def connect_ib():
    if not ib.isConnected():
        ib.connect(IB_HOST, IB_PORT, clientId=CLIENT_ID)
        print(f"=== [系統] 成功連接至 IBKR ({IB_HOST}:{IB_PORT}) | 實盤模式(送單): {SEND_WEBHOOK} ===")

def send_webhook_notification(action: str, symbol: str, qty: float, price: float, note: str = ""):
    if not WEBHOOK_URL or not WEBHOOK_PASSPHRASE:
        return
    
    payload = {
        "passphrase": WEBHOOK_PASSPHRASE,
        "symbol": symbol,
        "action": action,
        "quantity": str(qty),
        "price": str(price),
        "strategy_name": "iron_condor",
        "note": note
    }
    
    try:
        if SEND_WEBHOOK:
            response = requests.post(WEBHOOK_URL, json=payload, timeout=10)
            if response.status_code == 200:
                print(f"-> ✅ Webhook 傳送成功: {action} {qty} {symbol}")
            else:
                print(f"-> ❌ Webhook 傳送失敗: {response.status_code}")
    except Exception as e:
        print(f"-> ❌ Webhook 請求發生異常: {e}")

# ==============================================================================
# 2. 動態解析群組商品
# ==============================================================================
def resolve_group_symbols(group_name, symbols):
    for sym in symbols:
        underlying_sym = FUTURE_SYMBOL_MAP.get(sym, sym)
        details = ib.reqContractDetails(Future(symbol=underlying_sym))
        valid_details = [d for d in details if d.contract.exchange not in ['QBALGO', 'SMART']]
        
        if valid_details:
            underlying_fut = valid_details[0].contract
            
            trading_classes = set()
            chains = []
            for d in valid_details:
                res = ib.reqSecDefOptParams(d.contract.symbol, d.contract.exchange, d.contract.secType, d.contract.conId)
                if res:
                    chains.extend(res)
                    for c in res:
                        trading_classes.add(c.tradingClass)
            
            opt_class = next((s for s in symbols if s in trading_classes), None)
            if not opt_class and chains:
                opt_class = chains[0].tradingClass 
                
            return underlying_fut, opt_class, chains
    return None, None, []

# ==============================================================================
# 3. 獲取 16 Delta 四腿合約與當前市價
# ==============================================================================
def get_iron_condor_legs(underlying_fut, opt_class, chains):
    expirations = set()
    strikes = set()
    for c in chains:
        if c.tradingClass == opt_class:
            expirations.update(c.expirations)
            strikes.update(c.strikes)
    
    if not expirations:
        print(f"[錯誤] 找不到 {underlying_fut.symbol} (Class: {opt_class}) 的期權鏈資料")
        return None

    today = datetime.date.today()
    target_date = today + datetime.timedelta(days=ENTRY_DTE)
    closest_expiry = min(expirations, key=lambda x: abs(datetime.datetime.strptime(x, '%Y%m%d').date() - target_date))
    
    chosen_date = datetime.datetime.strptime(closest_expiry, '%Y%m%d').date()
    current_dte = (chosen_date - today).days
    if current_dte <= EXIT_DTE:
        print(f"=== [防禦] 當前最佳合約僅剩 {current_dte} 天，小於 {EXIT_DTE} 天，拒絕開倉 ===")
        return None

    print(f"-> 鎖定 {underlying_fut.symbol} 到期日: {closest_expiry} (剩餘 {current_dte} 天)")

    options = [FuturesOption(underlying_fut.symbol, closest_expiry, strike, right, underlying_fut.exchange, tradingClass=opt_class) 
               for strike in sorted(list(strikes)) for right in ['C', 'P']]
    
    qualified = ib.qualifyContracts(*options)
    
    # 建立過濾後的合法履約價清單 (排除 API 回傳的幽靈履約價)
    valid_strikes_set = set(c.strike for c in qualified)
    sorted_strikes = sorted(list(valid_strikes_set))

    print(f"-> 正在取得 {underlying_fut.symbol} 希臘字母 (Greeks)...")
    tickers = ib.reqTickers(*qualified)
    ib.sleep(4) # 等待數據 (增加等待時間確保取得合理的 Greeks)
    
    call_c, put_c = [], []
    for t in tickers:
        if t.modelGreeks and t.modelGreeks.delta is not None:
            # 排除完全沒有 Delta (0.0) 或深價內的選項，避免抓到無效報價的假合約
            if 0.02 <= abs(t.modelGreeks.delta) <= 0.40:
                if t.contract.right == 'C':
                    call_c.append((t.contract.strike, abs(t.modelGreeks.delta - DELTA_TARGET), t))
                else:
                    put_c.append((t.contract.strike, abs(abs(t.modelGreeks.delta) - DELTA_TARGET), t))
                
    if not call_c or not put_c:
        print(f"[錯誤] 無法取得 {underlying_fut.symbol} 合理區間的希臘字母 (Delta)，略過。")
        return None

    s_call_tick = min(call_c, key=lambda x: x[1])[2]
    s_put_tick = min(put_c, key=lambda x: x[1])[2]
    
    wing_width = WING_WIDTH_MAP.get(underlying_fut.symbol, 10)
    
    # 確保 Long 腿的履約價在合法的 strikes 列表中，避免遇到幽靈履約價
    valid_l_calls = [s for s in sorted_strikes if s >= s_call_tick.contract.strike + wing_width]
    l_call = valid_l_calls[0] if valid_l_calls else sorted_strikes[-1]
    
    valid_l_puts = [s for s in reversed(sorted_strikes) if s <= s_put_tick.contract.strike - wing_width]
    l_put = valid_l_puts[0] if valid_l_puts else sorted_strikes[0]
    
    # 直接從已驗證的 qualified 清單中抓出 Contract 物件，保證 conId 是有效的
    l_call_contract = next((c for c in qualified if c.strike == l_call and c.right == 'C'), None)
    l_put_contract = next((c for c in qualified if c.strike == l_put and c.right == 'P'), None)
    
    if not l_call_contract or not l_put_contract:
        print(f"[錯誤] 無法從已驗證清單中找到 LC 或 LP 的合約")
        return None
        
    print(f"-> {underlying_fut.symbol} 組合腿確認: LC {l_call} | SC {s_call_tick.contract.strike} | SP {s_put_tick.contract.strike} | LP {l_put}")
    return {
        'symbol': underlying_fut.symbol, 'exchange': underlying_fut.exchange,
        'sc': s_call_tick.contract, 'lc': l_call_contract,
        'sp': s_put_tick.contract, 'lp': l_put_contract
    }

# ==============================================================================
# 4. 建立並送出四腿組合單 (Combo Order)
# ==============================================================================
def execute_iron_condor(legs):
    combo_contract = Contract(symbol=legs['symbol'], secType='BAG', currency='USD', exchange=legs['exchange'])
    
    # 🌟 關鍵修正：IBKR 處理 Credit Spread 的最佳實踐
    # 將組合單定義為「買入跨式+賣出雙翼」(Debit Spread)，此時報價會是正數。
    # 然後我們送出 SELL 委託單，IBKR 就會自動反轉腿的方向，精準成交為 Credit Iron Condor！
    combo_contract.comboLegs = [
        ComboLeg(conId=legs['lp'].conId, ratio=1, action='SELL', exchange=legs['exchange']),
        ComboLeg(conId=legs['sp'].conId, ratio=1, action='BUY', exchange=legs['exchange']),
        ComboLeg(conId=legs['sc'].conId, ratio=1, action='BUY', exchange=legs['exchange']),
        ComboLeg(conId=legs['lc'].conId, ratio=1, action='SELL', exchange=legs['exchange'])
    ]
    
    ticker = ib.reqMktData(combo_contract, '', False, False)
    ib.sleep(2)
    
    bid, ask = (ticker.bid or 0), (ticker.ask or 0)
    limit_price = (bid + ask) / 2 if (bid > 0 and ask > 0) else "市價"
    
    if limit_price != "市價":
        MIN_TICK_MAP = {
            'ES': 0.25, 'NQ': 0.25, 'RTY': 0.1,
            'ZC': 0.125, 'HG': 0.0005, 'GC': 0.1, 
            'EUR': 0.00005, 'JPY': 0.0000005,
            'NG': 0.001, 'CL': 0.01
        }
        min_tick = MIN_TICK_MAP.get(legs['symbol'], 0.01)
        limit_price = round(limit_price / min_tick) * min_tick

    order = MarketOrder('SELL', TRADE_QTY)
    order.tif = 'DAY'  # 強制覆寫 TWS 的 GTC 預設值，避免被交易所拒絕
    order.algoStrategy = 'Adaptive'
    order.algoParams = [TagValue('adaptivePriority', 'Patient')]
    
    if SEND_WEBHOOK:
        trade = ib.placeOrder(combo_contract, order)
        print(f"=== [下單成功] 訂單已傳送，等待成交中... ({legs['symbol']}) ===")
        
        # 等待成交 (Adaptive Patient 可能需要幾十秒)
        end_time = time.time() + 60
        while time.time() < end_time:
            ib.sleep(1)
            if trade.orderStatus.status == 'Filled':
                break
                
        if trade.orderStatus.status == 'Filled':
            fill_price = trade.orderStatus.avgFillPrice
            print(f"=== [成交確認] {legs['symbol']} 已成交，平均價格: {fill_price} ===")
            send_webhook_notification('SELL_IRON_CONDOR', legs['symbol'], TRADE_QTY, fill_price, "建立 Iron Condor")
        else:
            print(f"=== [未完全成交] {legs['symbol']} 目前狀態: {trade.orderStatus.status} ===")
    else:
        print(f"=== [測試模式] 僅列印不送單 ===\n-> 建立 {legs['symbol']} Iron Condor | 動作: SELL")
    ib.sleep(2)

# ==============================================================================
# 5. 每日部位檢查與風控模組
# ==============================================================================
def check_and_manage_positions(underlying_symbol):
    portfolio_items = [p for p in ib.portfolio() if p.contract.symbol == underlying_symbol and p.contract.secType == 'FOP']
    
    if not portfolio_items:
        return False
        
    total_unrealized_pnl = sum(p.unrealizedPNL for p in portfolio_items if p.unrealizedPNL)
    total_cost_basis = sum(p.position * p.averageCost for p in portfolio_items if p.averageCost)
    
    net_credit_collected = -total_cost_basis
    target_profit = net_credit_collected * 0.5
    
    sample_contract = portfolio_items[0].contract
    expiry_date = datetime.datetime.strptime(sample_contract.lastTradeDateOrContractMonth, '%Y%m%d').date()
    dte = (expiry_date - datetime.date.today()).days
    
    print(f"[{underlying_symbol}] 剩餘 {dte} 天到期 | 收取: {net_credit_collected:.2f} | 目標 50%: {target_profit:.2f} | 當前損益: {total_unrealized_pnl:.2f}")
    
    should_close = False
    reason = ""
    if dte <= EXIT_DTE:
        should_close, reason = True, f"達 {dte} DTE 強制平倉"
    elif target_profit > 0 and total_unrealized_pnl >= target_profit:
        should_close, reason = True, f"達 50% 停利目標"
        
    if should_close:
        for p in portfolio_items:
            if p.position == 0: continue
            action = 'BUY' if p.position < 0 else 'SELL'
            order = MarketOrder(action, abs(p.position))
            order.tif = 'DAY'  # 強制覆寫預設設定，避免 Market Order 被強制轉為 GTC 報錯
            order.algoStrategy = 'Adaptive'
            order.algoParams = [TagValue('adaptivePriority', 'Patient')]
            
            closing_contract = p.contract
            if not closing_contract.exchange:
                closing_contract.exchange = closing_contract.primaryExchange or "SMART"
                
            if SEND_WEBHOOK:
                trade = ib.placeOrder(closing_contract, order)
                print(f"-> 已送出平倉單: {action} {abs(p.position)}口 {closing_contract.localSymbol} ({reason})，等待成交...")
                
                end_time = time.time() + 60
                while time.time() < end_time:
                    ib.sleep(1)
                    if trade.orderStatus.status == 'Filled':
                        break
                        
                if trade.orderStatus.status == 'Filled':
                    fill_price = trade.orderStatus.avgFillPrice
                    print(f"-> [成交確認] 平倉單已成交，平均價格: {fill_price}")
                    send_webhook_notification(action, closing_contract.localSymbol, abs(p.position), fill_price, reason)
                else:
                    print(f"-> [未完全成交] 平倉單目前狀態: {trade.orderStatus.status}")
            else:
                print(f"-> [測試模式] 假裝平倉: {action} {abs(p.position)}口 {closing_contract.localSymbol} ({reason})")
        return True
    return False

# ==============================================================================
# 6. Barchart Premium 進階篩選指標 (由外部提供 CSV 來源)
# ==============================================================================
def find_uoa_targets(file_path):
    """1. 異常選擇權活動 (UOA) 篩選"""
    df = pd.read_csv(file_path)
    uoa_df = df[df['Volume'] > (df['Open Interest'] * 5)].copy()
    uoa_df['UOA_Ratio'] = uoa_df['Volume'] / uoa_df['Open Interest']
    return uoa_df.sort_values(by='UOA_Ratio', ascending=False)

def find_momentum_stocks(file_path):
    """2. 100% Buy 強勢多頭波段"""
    df = pd.read_csv(file_path)
    trend_df = df[
        (df['Opinion'] == '100% Buy') & 
        (df['Opinion_Direction'] == 'Upgrading') & 
        (df['Last'] > df['200D_MA'])
    ]
    return trend_df

def find_iron_condor_candidates(file_path):
    """3. IV Rank 雙賣部位篩選"""
    df = pd.read_csv(file_path)
    ic_df = df[(df['IV Rank'] > 50) & (df['Options_Volume'] > 10000)]
    return ic_df.sort_values(by='IV Rank', ascending=False)

def analyze_volatility_skew(options_chain_df):
    """4. 波動率傾斜 (Skew) 套利"""
    # 此處輸入為 DataFrame 而非檔案路徑
    options_chain_df['IV_Skew'] = options_chain_df['Put_IV'] - options_chain_df['Call_IV']
    panic_puts = options_chain_df[options_chain_df['IV_Skew'] > 0.10]
    greedy_calls = options_chain_df[options_chain_df['IV_Skew'] < -0.10]
    return panic_puts, greedy_calls


# ==============================================================================
# 7. 主程式進入點
# ==============================================================================
if __name__ == '__main__':
    try:
        connect_ib()
        groups_to_process = {k: v for k, v in hedge_config.items() if not TARGET_GROUPS or k in TARGET_GROUPS}
        
        for g_name, g_info in groups_to_process.items():
            print(f"\n================ 開始處理群組: {g_name} ================")
            symbols = g_info.get('symbols', [])
            underlying_fut, opt_class, chains = resolve_group_symbols(g_name, symbols)
            
            if not underlying_fut:
                print(f"[略過] 無法解析 {g_name} 的期貨與期權結構。")
                continue
            
            positions_closed = check_and_manage_positions(underlying_fut.symbol)
            
            current_pos = [p for p in ib.portfolio() if p.contract.symbol == underlying_fut.symbol and p.contract.secType == 'FOP']
            if not current_pos:
                print(f"-> 準備為 {underlying_fut.symbol} (Class: {opt_class}) 建立新部位...")
                legs = get_iron_condor_legs(underlying_fut, opt_class, chains)
                if legs: execute_iron_condor(legs)
            else:
                print(f"-> {underlying_fut.symbol} 已有部位，跳過建倉。")
            
    except Exception as e:
        print(f"\n[錯誤] 執行異常: {e}")
    finally:
        if ib.isConnected(): ib.disconnect()
    
    time.sleep(60*60)
