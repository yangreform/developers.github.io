import os
import json
import datetime
import time
import math
import requests
from ib_insync import *

from notifier import send_push_message, send_trade_notification

# ==============================================================================
# 0. 動態讀取 .env 設定工具
# ==============================================================================
ENV_PATH = os.path.join(os.path.dirname(__file__), '.env')

def load_env_config():
    """
    即時讀取 trade/.env 設定，包含 OP_HEDGE_CONFIG_JSON, OP_SEND_WEBHOOK 等
    每次執行循環皆重新讀取，確保參數修改後立即生效
    """
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
            print(f"[警告] 讀取 .env 檔案失敗: {e}")
    return cfg

# ==============================================================================
# 1. 常數與商品映射表
# ==============================================================================
DEFAULT_MIN_DTE = 30
DEFAULT_MAX_DTE = 60
DEFAULT_WING_WIDTH = 50
TRADE_QTY = 1

# 指數選擇權清單與交易規格 (SPX: CBOE, NDX: NASDAQ)
INDEX_MAP = {
    'SPX': {'exchange': 'CBOE', 'currency': 'USD', 'preferred_class': 'SPX'},
    'NDX': {'exchange': 'NASDAQ', 'currency': 'USD', 'preferred_class': 'NDX'}
}

# 動態目標商品映射表 (Option Trading Class / Micro Symbol -> Standard Underlying Symbol)
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
    'SPX': 'SPX',
    'NDX': 'NDX'
}

# 優先/標準月度選擇權交易類別映射表
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
    'SPX': 'SPX',
    'NDX': 'NDX',
}

# 預設動態翼寬設定 (若 .env 中的 OP_HEDGE_CONFIG_JSON 未個別指定時的預設值)
WING_WIDTH_MAP = {
    'ZC': 40,
    'NQ': 200,
    'ES': 50,
    'JPY': 0.0020,
    'EUR': 0.04,
    'HG': 0.20,
    'GC': 80,
    'RTY': 50,
    'NG': 0.20,
    'CL': 5.0,
    'SPX': 50,
    'NDX': 200,
}

# 各商品最小跳動點 (Min Tick)
MIN_TICK_MAP = {
    'ES': 0.25, 'NQ': 0.25, 'RTY': 0.1,
    'ZC': 0.125, 'HG': 0.0005, 'GC': 0.1,
    'EUR': 0.00005, 'JPY': 0.0000005,
    'NG': 0.001, 'CL': 0.01,
    'SPX': 0.05, 'NDX': 0.05
}

ib = IB()

def connect_ib():
    env_config = load_env_config()
    ib_host = env_config.get('IB_HOST', '127.0.0.1')
    ib_port = int(env_config.get('IB_PORT', 4001))
    client_id = int(env_config.get('IB_CLIENT_ID', 100)) + 6
    raw_send_webhook = str(env_config.get('OP_SEND_WEBHOOK', 'false')).strip().lower()
    send_webhook = raw_send_webhook in ('true', '1')

    if not ib.isConnected():
        ib.connect(ib_host, ib_port, clientId=client_id)
        print(f"=== [系統] 成功連接至 IBKR ({ib_host}:{ib_port}) | 實盤送單模式: {send_webhook} ===")

def send_webhook_notification(action: str, symbol: str, qty: float, price: float, note: str = ""):
    env_config = load_env_config()
    raw_send = str(env_config.get('OP_SEND_WEBHOOK', 'false')).strip().lower()
    send_webhook = raw_send in ('true', '1')

    payload = {
        "symbol": symbol,
        "action": action,
        "quantity": str(qty),
        "price": str(price),
        "strategy_name": "butterfly",
        "note": note
    }
    msg = f"期權 Butterfly 交易: {action} {qty}口 @ {price}\n說明: {note}"
    try:
        if send_webhook:
            send_trade_notification(symbol, msg, payload)
        else:
            print(f"-> [測試模式通知] {msg}")
    except Exception as e:
        print(f"-> ❌ 推播通知發送異常: {e}")

# ==============================================================================
# 2. 解析標的與期權鏈 (支援期貨期權 FOP 與指數期權 SPX/NDX)
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

    # 1. 處理指數選擇權 (SPX, NDX)
    if underlying_sym in INDEX_MAP:
        idx_info = INDEX_MAP[underlying_sym]
        underlying_idx = Index(symbol=underlying_sym, exchange=idx_info['exchange'], currency=idx_info['currency'])
        qualified = ib.qualifyContracts(underlying_idx)
        if not qualified:
            underlying_idx = Index(symbol=underlying_sym, currency=idx_info['currency'])
            ib.qualifyContracts(underlying_idx)

        chains = ib.reqSecDefOptParams(underlying_idx.symbol, '', underlying_idx.secType, underlying_idx.conId)
        trading_classes = {c.tradingClass for c in chains} if chains else set()

        preferred = PREFERRED_TRADING_CLASS_MAP.get(underlying_sym)
        opt_class = None
        if preferred and preferred in trading_classes:
            opt_class = preferred
        elif chains:
            exact = [c.tradingClass for c in chains if c.tradingClass == underlying_sym]
            opt_class = exact[0] if exact else chains[0].tradingClass

        return underlying_idx, opt_class, chains

    # 2. 處理商品期貨選擇權 (ES, NQ, CL, GC, ZC, etc.)
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
# 3. 獲取 Butterfly 三腿合約（賣 1 下翼、買 2 中心、賣 1 上翼）
# ==============================================================================
def get_butterfly_legs(underlying_contract, opt_class, chains, min_dte, max_dte, wing_width):
    today = datetime.date.today()
    target_dte = int((min_dte + max_dte) / 2)
    
    class_candidates = []
    # 優先從指定的 opt_class 尋找符合 min_dte ~ max_dte 的到期日
    if opt_class:
        for c in chains:
            if c.tradingClass == opt_class:
                for exp in c.expirations:
                    try:
                        dte = (datetime.datetime.strptime(exp, '%Y%m%d').date() - today).days
                        if min_dte <= dte <= max_dte:
                            class_candidates.append((c.tradingClass, exp, c.strikes, dte))
                    except Exception:
                        pass
    
    # 若該 opt_class 無符合天數，從所有標準交易類別尋找
    if not class_candidates:
        for c in chains:
            if not c.tradingClass.startswith(('M', 'W', 'X')) and c.tradingClass not in ['XC', 'YC', 'MJY', 'M6E', 'MHG', 'M2K']:
                for exp in c.expirations:
                    try:
                        dte = (datetime.datetime.strptime(exp, '%Y%m%d').date() - today).days
                        if min_dte <= dte <= max_dte:
                            class_candidates.append((c.tradingClass, exp, c.strikes, dte))
                    except Exception:
                        pass
                        
    # 若仍無，放寬在 dte >= min_dte 的最近合約
    if not class_candidates:
        for c in chains:
            if not c.tradingClass.startswith(('M', 'W', 'X')) and c.tradingClass not in ['XC', 'YC', 'MJY', 'M6E', 'MHG', 'M2K']:
                for exp in c.expirations:
                    try:
                        dte = (datetime.datetime.strptime(exp, '%Y%m%d').date() - today).days
                        if dte >= min_dte:
                            class_candidates.append((c.tradingClass, exp, c.strikes, dte))
                    except Exception:
                        pass

    if not class_candidates:
        print(f"[錯誤] 找不到 {underlying_contract.symbol} (Class: {opt_class}) 符合 DTE 範圍 ({min_dte}~{max_dte} 天) 的期權合約到期日")
        return None

    # 選取 DTE 最接近 target_dte 的到期日
    best_candidate = min(class_candidates, key=lambda item: abs(item[3] - target_dte))
    chosen_opt_class, closest_expiry, strikes, current_dte = best_candidate

    print(f"-> 鎖定 {underlying_contract.symbol} (Class: {chosen_opt_class}) 到期日: {closest_expiry} (DTE: {current_dte} 天，設定範圍: {min_dte}~{max_dte} 天)")

    # 取得底層標的最新市價
    underlying_ticker = ib.reqTickers(underlying_contract)
    ib.sleep(1)
    ref_price = underlying_ticker[0].marketPrice() if underlying_ticker else 0
    if not ref_price or ref_price <= 0:
        ref_price = underlying_ticker[0].close if underlying_ticker and underlying_ticker[0].close else 0

    sorted_strikes = sorted(list(strikes))
    if not sorted_strikes:
        print(f"[錯誤] {underlying_contract.symbol} 無可用履約價列表")
        return None

    # 1. 決定中心履約價 (Center Strike / Body) -> ATM
    if ref_price and ref_price > 0:
        center_strike = min(sorted_strikes, key=lambda s: abs(s - ref_price))
    else:
        center_strike = sorted_strikes[len(sorted_strikes) // 2]
        print(f"[警告] 無法即時取得現價，使用履約價中位數作為中心: {center_strike}")

    # 2. 決定下翼與上翼 (Lower Wing & Upper Wing)
    target_lower = center_strike - wing_width
    target_upper = center_strike + wing_width

    lower_candidates = [s for s in sorted_strikes if s < center_strike]
    upper_candidates = [s for s in sorted_strikes if s > center_strike]

    if not lower_candidates or not upper_candidates:
        print(f"[錯誤] {underlying_contract.symbol} 中心履約價 {center_strike} 兩側缺乏可用履約價")
        return None

    lower_strike = min(lower_candidates, key=lambda s: abs(s - target_lower))
    upper_strike = min(upper_candidates, key=lambda s: abs(s - target_upper))

    actual_left_wing = round(center_strike - lower_strike, 4)
    actual_right_wing = round(upper_strike - center_strike, 4)

    print(
        f"-> Butterfly (蝶式) 履約價確認: "
        f"賣1下翼: {lower_strike} (翼寬 {actual_left_wing}) | "
        f"買2中心: {center_strike} | "
        f"賣1上翼: {upper_strike} (翼寬 {actual_right_wing}) | 目標翼寬: {wing_width}"
    )

    # 3. 建立 3 檔 Call 合約 (Index Option 或 FuturesOption)
    is_index = (underlying_contract.secType == 'IND')
    if is_index:
        lower_opt = Option(underlying_contract.symbol, closest_expiry, lower_strike, 'C', underlying_contract.exchange, currency='USD', tradingClass=chosen_opt_class)
        center_opt = Option(underlying_contract.symbol, closest_expiry, center_strike, 'C', underlying_contract.exchange, currency='USD', tradingClass=chosen_opt_class)
        upper_opt = Option(underlying_contract.symbol, closest_expiry, upper_strike, 'C', underlying_contract.exchange, currency='USD', tradingClass=chosen_opt_class)
    else:
        lower_opt = FuturesOption(underlying_contract.symbol, closest_expiry, lower_strike, 'C', underlying_contract.exchange, tradingClass=chosen_opt_class)
        center_opt = FuturesOption(underlying_contract.symbol, closest_expiry, center_strike, 'C', underlying_contract.exchange, tradingClass=chosen_opt_class)
        upper_opt = FuturesOption(underlying_contract.symbol, closest_expiry, upper_strike, 'C', underlying_contract.exchange, tradingClass=chosen_opt_class)

    qualified = ib.qualifyContracts(lower_opt, center_opt, upper_opt)
    if len(qualified) < 3:
        print(f"[錯誤] {underlying_contract.symbol} 合約驗證失敗 (qualified {len(qualified)}/3)")
        return None

    print(f"-> 正在取得 {underlying_contract.symbol} 蝶式三腿報價與 Greeks...")
    tickers = ib.reqTickers(lower_opt, center_opt, upper_opt)
    ib.sleep(3)

    t_lower, t_center, t_upper = tickers[0], tickers[1], tickers[2]

    def get_greek(t, attr):
        if t.modelGreeks and getattr(t.modelGreeks, attr, None) is not None:
            return getattr(t.modelGreeks, attr)
        return 0.0

    d_l, t_l, g_l = get_greek(t_lower, 'delta'), get_greek(t_lower, 'theta'), get_greek(t_lower, 'gamma')
    d_c, t_c, g_c = get_greek(t_center, 'delta'), get_greek(t_center, 'theta'), get_greek(t_center, 'gamma')
    d_u, t_u, g_u = get_greek(t_upper, 'delta'), get_greek(t_upper, 'theta'), get_greek(t_upper, 'gamma')

    # 組合損益與 Greeks (賣1下翼、買2中心、賣1上翼)
    total_delta = (2 * d_c) - d_l - d_u
    total_theta = (2 * t_c) - t_l - t_u
    total_gamma = (2 * g_c) - g_l - g_u

    print(
        f"-> {underlying_contract.symbol} Butterfly 組合計算完成:\n"
        f"   - 賣1下翼 C {lower_strike}: Δ={d_l:+.3f}, θ={t_l:.2f}, γ={g_l:.4f}\n"
        f"   - 買2中心 C {center_strike}: Δ={d_c:+.3f}, θ={t_c:.2f}, γ={g_c:.4f}\n"
        f"   - 賣1上翼 C {upper_strike}: Δ={d_u:+.3f}, θ={t_u:.2f}, γ={g_u:.4f}\n"
        f"   -> 組合淨值: DTE={current_dte}天 | 淨Δ={total_delta:+.3f} | 總θ={total_theta:.2f} | 總γ={total_gamma:.4f}"
    )

    return {
        'symbol': underlying_contract.symbol,
        'exchange': underlying_contract.exchange,
        'is_index': is_index,
        'lower': lower_opt,
        'center': center_opt,
        'upper': upper_opt,
        'lower_ticker': t_lower,
        'center_ticker': t_center,
        'upper_ticker': t_upper,
        'dte': current_dte,
        'total_delta': total_delta,
        'total_theta': total_theta,
        'total_gamma': total_gamma
    }

# ==============================================================================
# 4. 建立並送出 Butterfly 組合單 (全部下 BID LIMIT)
# ==============================================================================
def execute_butterfly(legs):
    combo_contract = Contract(
        symbol=legs['symbol'],
        secType='BAG',
        currency='USD',
        exchange=legs['exchange']
    )
    
    # 定義 Butterfly 三腿：賣 1 下翼、買 2 中心、賣 1 上翼
    combo_contract.comboLegs = [
        ComboLeg(conId=legs['lower'].conId, ratio=1, action='SELL', exchange=legs['exchange']),
        ComboLeg(conId=legs['center'].conId, ratio=2, action='BUY', exchange=legs['exchange']),
        ComboLeg(conId=legs['upper'].conId, ratio=1, action='SELL', exchange=legs['exchange'])
    ]
    
    ticker = ib.reqMktData(combo_contract, '', False, False)
    ib.sleep(2.5)
    
    # 取得 BID 價格 (全部下 BID LIMIT)
    combo_bid = None
    if ticker.bid is not None and not math.isnan(ticker.bid) and ticker.bid != 0:
        combo_bid = ticker.bid
        print(f"-> 取得 IBKR 組合即時 BID: {combo_bid}")
    else:
        # 合成 BID: 買 2 中心 @ Bid, 賣下翼 @ Ask, 賣上翼 @ Ask -> 淨買入出價 (Bid)
        c_bid = legs['center_ticker'].bid or legs['center_ticker'].close or 0
        l_ask = legs['lower_ticker'].ask or legs['lower_ticker'].close or 0
        u_ask = legs['upper_ticker'].ask or legs['upper_ticker'].close or 0
        if c_bid or l_ask or u_ask:
            combo_bid = round((2 * c_bid) - l_ask - u_ask, 4)
            print(f"-> IBKR 組合無直接報價，計算合成 BID: {combo_bid} (中心Bid={c_bid}, 下翼Ask={l_ask}, 上翼Ask={u_ask})")

    if combo_bid is None:
        print(f"[錯誤] {legs['symbol']} 無法取得有效 BID 價格，依安全原則不送單 (嚴禁市價單)！")
        return

    min_tick = MIN_TICK_MAP.get(legs['symbol'], 0.01)
    limit_price = round(combo_bid / min_tick) * min_tick
    limit_price = round(limit_price, 6)

    # 送出 BUY 委託單以成交 (賣1下翼、買2中心、賣1上翼)，限價掛在 BID
    order = LimitOrder('BUY', TRADE_QTY, limit_price)
    order.tif = 'DAY'
    
    log_title = (
        f"Butterfly (賣1 C{legs['lower'].strike} / 買2 C{legs['center'].strike} / 賣1 C{legs['upper'].strike}) "
        f"| DTE: {legs['dte']}天 | 委託價: BID LIMIT ${limit_price} | 淨Δ: {legs['total_delta']:+.3f}, θ: {legs['total_theta']:.2f}"
    )

    env_config = load_env_config()
    raw_send = str(env_config.get('OP_SEND_WEBHOOK', 'false')).strip().lower()
    send_webhook = raw_send in ('true', '1')

    if send_webhook:
        trade = ib.placeOrder(combo_contract, order)
        print(f"=== [下單成功] 已送出 BID LIMIT 訂單: {legs['symbol']} {log_title} ===")
        
        # 等待成交 (60秒)
        end_time = time.time() + 60
        while time.time() < end_time:
            ib.sleep(1)
            if trade.orderStatus.status == 'Filled':
                break
                
        if trade.orderStatus.status == 'Filled':
            fill_price = trade.orderStatus.avgFillPrice
            print(f"=== [成交確認] {legs['symbol']} Butterfly 已成交，平均價格: {fill_price} ===")
            note_str = f"成交 Butterfly (賣1 C{legs['lower'].strike}/買2 C{legs['center'].strike}/賣1 C{legs['upper'].strike}, DTE: {legs['dte']}天, 均價: {fill_price})"
            send_webhook_notification('BUY_BUTTERFLY', legs['symbol'], TRADE_QTY, fill_price, note_str)
        else:
            print(f"=== [掛單中/未成交] {legs['symbol']} 目前狀態: {trade.orderStatus.status} (限價: {limit_price}) ===")
    else:
        print(
            f"=== [測試模式] 僅列印不送單 ===\n"
            f"-> 建立 {legs['symbol']} {log_title}"
        )
    ib.sleep(2)

# ==============================================================================
# 5. 掃描與執行主流程 (即時自 trade/.env 讀取 OP_HEDGE_CONFIG_JSON)
# ==============================================================================
def run_option_scanner():
    """
    即時查詢 trade/.env 中的 OP_HEDGE_CONFIG_JSON，
    支援每個商品分別自訂: min_dte(開始日), max_dte(結束日), wing_width(動態翼寬)
    """
    env_config = load_env_config()
    raw_config = env_config.get('OP_HEDGE_CONFIG_JSON', '{}')
    try:
        hedge_config = json.loads(raw_config)
    except Exception as e:
        print(f"[錯誤] 即時解析 OP_HEDGE_CONFIG_JSON 失敗: {e}")
        return

    print(f"\n================ 開始掃描期權對沖清單 (共 {len(hedge_config)} 個商品) ================")
    
    for g_name, g_info in hedge_config.items():
        symbols = g_info.get('symbols', [])
        
        # 1. 支援每個商品分別自訂 DTE 開始日與結束日
        min_dte = int(g_info.get('min_dte') or g_info.get('dte_start') or g_info.get('start_dte') or g_info.get('DTE開始日') or DEFAULT_MIN_DTE)
        max_dte = int(g_info.get('max_dte') or g_info.get('dte_end') or g_info.get('end_dte') or g_info.get('DTE結束日') or DEFAULT_MAX_DTE)

        # 2. 支援每個商品分別自訂動態翼寬 WING_WIDTH
        underlying_sym = symbols[0] if symbols else g_name
        wing_width = float(g_info.get('wing_width') or g_info.get('WING_WIDTH') or g_info.get('wing') or WING_WIDTH_MAP.get(underlying_sym, DEFAULT_WING_WIDTH))

        print(f"\n[商品掃描: {g_name}] symbols={symbols} | DTE範圍: {min_dte}~{max_dte} 天 | 動態翼寬: {wing_width}")
        
        underlying_contract, opt_class, chains = resolve_group_symbols(g_name, symbols)
        if not underlying_contract:
            print(f"[略過] 無法解析 {g_name} 的期貨/指數與期權結構。")
            continue

        # 檢查是否已有該標的之期權部位 (FOP 或 OPT)
        current_pos = [p for p in ib.portfolio() if p.contract.symbol == underlying_contract.symbol and p.contract.secType in ('FOP', 'OPT') and p.position != 0]
        if not current_pos:
            print(f"-> 準備為 {underlying_contract.symbol} (Class: {opt_class}) 建立 Butterfly 部位...")
            legs = get_butterfly_legs(underlying_contract, opt_class, chains, min_dte, max_dte, wing_width)
            if legs:
                execute_butterfly(legs)
        else:
            print(f"-> {underlying_contract.symbol} 已有部位 ({len(current_pos)} 筆)，跳過建倉。")

# ==============================================================================
# 6. 主程式入口
# ==============================================================================
if __name__ == '__main__':
    try:
        connect_ib()
        run_option_scanner()
    except Exception as e:
        print(f"\n[錯誤] 執行異常: {e}")
    finally:
        if ib.isConnected():
            ib.disconnect()
