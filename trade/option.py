import os
import json
import datetime
import time
import requests
from ib_insync import *

import glob
import pandas as pd
from flask import Flask, render_template_string, jsonify, request
import threading
from waitress import serve

# ==============================================================================
# 0. 讀取 op.env / .env 設定
# ==============================================================================
ENV_PATH = os.path.join(os.path.dirname(__file__), '.env')
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

raw_send_webhook = str(env_config.get('OP_SEND_WEBHOOK', 'false')).strip().strip("'").strip('"').lower()
SEND_WEBHOOK = raw_send_webhook in ('true', '1')

hedge_config = {}
try:
    hedge_config = json.loads(env_config.get('OP_HEDGE_CONFIG_JSON', '{}'))
except Exception as e:
    print(f"[錯誤] 解析 HEDGE_CONFIG_JSON 失敗: {e}")

# 動態目標商品映射表 (Option Trading Class / Micro Symbol -> Standard Underlying Future Symbol)
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
    'CL': 'CL', 'LO': 'CL', 'MCL': 'CL'
}

# 優先/標準月度選擇權交易類別映射表 (Underlying Symbol -> Preferred Standard Option Trading Class)
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
}

TARGET_GROUPS = [] 

MIN_ENTRY_DTE = 30
MAX_ENTRY_DTE = 60
ENTRY_DTE = 45          
EXIT_DTE = 21           
DELTA_TARGET = 0.16     
TRADE_QTY = 1           

# 動態翼寬設定 (目標間距，最終會尋找最接近的合法履約價)
WING_WIDTH_MAP = {
    'ZC': 40,     
    'NQ': 200,
    'ES': 200,
    'JPY': 0.0020,
    'EUR': 0.04,
    'HG': 0.20,
    'GC': 80,
    'RTY': 50,
    'NG': 0.20,
    'CL': 5.0,
}

ib = IB()

from notifier import send_push_message, send_trade_notification

def connect_ib():
    if not ib.isConnected():
        ib.connect(IB_HOST, IB_PORT, clientId=CLIENT_ID)
        print(f"=== [系統] 成功連接至 IBKR ({IB_HOST}:{IB_PORT}) | 實盤模式(送單): {SEND_WEBHOOK} ===")

def send_webhook_notification(action: str, symbol: str, qty: float, price: float, note: str = ""):
    payload = {
        "symbol": symbol,
        "action": action,
        "quantity": str(qty),
        "price": str(price),
        "strategy_name": "iron_condor",
        "note": note
    }
    msg = f"選擇權交易成交: {action} {qty}口 @ {price} ({note})"
    try:
        if SEND_WEBHOOK:
            send_trade_notification(symbol, msg, payload)
        else:
            print(f"-> [測試模式通知] {msg}")
    except Exception as e:
        print(f"-> ❌ 推播通知發送異常: {e}")

# ==============================================================================
# 2. 動態解析群組商品 (鎖定大合約 NQ, ES, GC, CL... 與標準選擇權類別)
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

    details = ib.reqContractDetails(Future(symbol=underlying_sym))
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
# 3. 獲取 16 Delta 雙賣合約與當前市價
# ==============================================================================
def get_iron_condor_legs(underlying_fut, opt_class, chains):
    today = datetime.date.today()
    
    class_candidates = []
    # 優先從指定的標準 opt_class 尋找 30~60 天合約
    if opt_class:
        for c in chains:
            if c.tradingClass == opt_class:
                for exp in c.expirations:
                    dte = (datetime.datetime.strptime(exp, '%Y%m%d').date() - today).days
                    if MIN_ENTRY_DTE <= dte <= MAX_ENTRY_DTE:
                        class_candidates.append((c.tradingClass, exp, c.strikes, dte))
    
    # 若該 opt_class 在 30~60 天無到期日，從所有標準交易類別 (排除微型與非月/週五) 尋找 30~60 天合約
    if not class_candidates:
        for c in chains:
            if not c.tradingClass.startswith(('M', 'W', 'X')) and c.tradingClass not in ['XC', 'YC', 'MJY', 'M6E', 'MHG', 'M2K']:
                for exp in c.expirations:
                    dte = (datetime.datetime.strptime(exp, '%Y%m%d').date() - today).days
                    if MIN_ENTRY_DTE <= dte <= MAX_ENTRY_DTE:
                        class_candidates.append((c.tradingClass, exp, c.strikes, dte))
                        
    # 若 30~60 天仍無合約，則備用 > EXIT_DTE (21天) 的合約
    if not class_candidates:
        for c in chains:
            if not c.tradingClass.startswith(('M', 'W', 'X')) and c.tradingClass not in ['XC', 'YC', 'MJY', 'M6E', 'MHG', 'M2K']:
                for exp in c.expirations:
                    dte = (datetime.datetime.strptime(exp, '%Y%m%d').date() - today).days
                    if dte > EXIT_DTE:
                        class_candidates.append((c.tradingClass, exp, c.strikes, dte))

    if not class_candidates:
        print(f"[錯誤] 找不到 {underlying_fut.symbol} (Class: {opt_class}) 符合條件的期權合約到期日")
        return None

    # 選取 DTE 最接近 ENTRY_DTE (45天) 的合約
    best_candidate = min(class_candidates, key=lambda item: abs(item[3] - ENTRY_DTE))
    chosen_opt_class, closest_expiry, strikes, current_dte = best_candidate

    print(f"-> 鎖定 {underlying_fut.symbol} (Class: {chosen_opt_class}) 到期日: {closest_expiry} (DTE: {current_dte} 天，範圍 30~60 天)")

    # 取得底層期貨最新市價以過濾履約價範圍（±35%），大幅提升合約驗證與 Greeks 獲取速度
    underlying_ticker = ib.reqTickers(underlying_fut)
    ib.sleep(1)
    ref_price = underlying_ticker[0].marketPrice() if underlying_ticker else 0
    if not ref_price or ref_price <= 0:
        ref_price = underlying_ticker[0].close if underlying_ticker and underlying_ticker[0].close else 0

    if ref_price and ref_price > 0:
        filtered_strikes = [s for s in strikes if ref_price * 0.65 <= s <= ref_price * 1.35]
    else:
        filtered_strikes = list(strikes)

    options = [FuturesOption(underlying_fut.symbol, closest_expiry, strike, right, underlying_fut.exchange, tradingClass=opt_class) 
               for strike in sorted(filtered_strikes) for right in ['C', 'P']]
    
    qualified = ib.qualifyContracts(*options)
    
    # 建立過濾後的合法履約價清單 (排除 API 回傳的幽靈履約價)
    valid_strikes_set = set(c.strike for c in qualified)
    sorted_strikes = sorted(list(valid_strikes_set))

    print(f"-> 正在取得 {underlying_fut.symbol} 希臘字母 (Greeks)... (共 {len(qualified)} 檔合約)")
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
    
    sc_delta = s_call_tick.modelGreeks.delta if (s_call_tick.modelGreeks and s_call_tick.modelGreeks.delta is not None) else 0.0
    sc_theta = s_call_tick.modelGreeks.theta if (s_call_tick.modelGreeks and s_call_tick.modelGreeks.theta is not None) else 0.0
    sp_delta = s_put_tick.modelGreeks.delta if (s_put_tick.modelGreeks and s_put_tick.modelGreeks.delta is not None) else 0.0
    sp_theta = s_put_tick.modelGreeks.theta if (s_put_tick.modelGreeks and s_put_tick.modelGreeks.theta is not None) else 0.0

    total_delta = -(sc_delta + sp_delta)
    total_theta = -(sc_theta + sp_theta)

    print(
        f"-> {underlying_fut.symbol} 雙賣組合確認: "
        f"SC {s_call_tick.contract.strike} (DELTA: {sc_delta:+.3f}, Theta: {sc_theta:.2f}) | "
        f"SP {s_put_tick.contract.strike} (DELTA: {sp_delta:+.3f}, Theta: {sp_theta:.2f}) | "
        f"DTE: {current_dte} 天 | 總淨 DELTA: {total_delta:+.3f} | 總 Theta: {total_theta:.2f}"
    )

    return {
        'symbol': underlying_fut.symbol, 'exchange': underlying_fut.exchange,
        'sc': s_call_tick.contract,
        'sp': s_put_tick.contract,
        'dte': current_dte,
        'sc_delta': sc_delta,
        'sc_theta': sc_theta,
        'sp_delta': sp_delta,
        'sp_theta': sp_theta,
        'total_delta': total_delta,
        'total_theta': total_theta
    }

# ==============================================================================
# 4. 建立並送出雙賣組合單 (Short Strangle Combo Order)
# ==============================================================================
def execute_iron_condor(legs):
    combo_contract = Contract(symbol=legs['symbol'], secType='BAG', currency='USD', exchange=legs['exchange'])
    
    # 將組合單定義為買入兩腿，送出 SELL 委託單，精準成交為雙賣 (Short Strangle: 賣出 Put + 賣出 Call)
    combo_contract.comboLegs = [
        ComboLeg(conId=legs['sp'].conId, ratio=1, action='BUY', exchange=legs['exchange']),
        ComboLeg(conId=legs['sc'].conId, ratio=1, action='BUY', exchange=legs['exchange'])
    ]
    
    ticker = ib.reqMktData(combo_contract, '', False, False)
    ib.sleep(2)
    
    bid, ask = (ticker.bid or 0), (ticker.ask or 0)
    limit_price = ask if ask > 0 else "市價"
    
    if limit_price != "市價":
        MIN_TICK_MAP = {
            'ES': 0.25, 'NQ': 0.25, 'RTY': 0.1,
            'ZC': 0.125, 'HG': 0.0005, 'GC': 0.1, 
            'EUR': 0.00005, 'JPY': 0.0000005,
            'NG': 0.001, 'CL': 0.01
        }
        min_tick = MIN_TICK_MAP.get(legs['symbol'], 0.01)
        limit_price = round(limit_price / min_tick) * min_tick

        order = LimitOrder('SELL', TRADE_QTY, limit_price)
    else:
        order = MarketOrder('SELL', TRADE_QTY)
        
    order.tif = 'DAY'
    
    if SEND_WEBHOOK:
        trade = ib.placeOrder(combo_contract, order)
        print(
            f"=== [下單成功] 雙賣訂單已傳送: {legs['symbol']} "
            f"SC {legs['sc'].strike} (DELTA {legs['sc_delta']:+.3f}) + SP {legs['sp'].strike} (DELTA {legs['sp_delta']:+.3f}) | "
            f"DTE: {legs['dte']} 天 | 總淨 DELTA: {legs['total_delta']:+.3f} | 總 Theta: {legs['total_theta']:.2f} ==="
        )
        
        # 等待成交
        end_time = time.time() + 60
        while time.time() < end_time:
            ib.sleep(1)
            if trade.orderStatus.status == 'Filled':
                break
                
        if trade.orderStatus.status == 'Filled':
            fill_price = trade.orderStatus.avgFillPrice
            print(f"=== [成交確認] {legs['symbol']} 雙賣已成交，平均價格: {fill_price} ===")
            note_str = f"建立雙賣 (Short Strangle: SC {legs['sc'].strike}/SP {legs['sp'].strike}, DTE: {legs['dte']}天, 淨Δ: {legs['total_delta']:+.3f}, θ: {legs['total_theta']:.2f})"
            send_webhook_notification('SELL_STRANGLE', legs['symbol'], TRADE_QTY, fill_price, note_str)
        else:
            print(f"=== [未完全成交] {legs['symbol']} 目前狀態: {trade.orderStatus.status} ===")
    else:
        print(
            f"=== [測試模式] 僅列印不送單 ===\n"
            f"-> 建立 {legs['symbol']} 雙賣 (Short Strangle) | 動作: SELL | "
            f"SC {legs['sc'].strike} (DELTA: {legs['sc_delta']:+.3f}, Theta: {legs['sc_theta']:.2f}) + "
            f"SP {legs['sp'].strike} (DELTA: {legs['sp_delta']:+.3f}, Theta: {legs['sp_theta']:.2f}) | "
            f"DTE: {legs['dte']} 天 | 總淨 DELTA: {legs['total_delta']:+.3f} | 總 Theta: {legs['total_theta']:.2f}"
        )
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
            order.tif = 'DAY'
            
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
# 6. 主程式入口
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
