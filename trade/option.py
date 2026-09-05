import os
import json
import datetime
import time
import requests
from ib_insync import *

from notifier import send_push_message, send_trade_notification

# ==============================================================================
# 0. 環境設定動態載入工具
# ==============================================================================
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')

def load_env_config(env_path: str = ENV_PATH):
    """
    即時查詢 trade/.env 中的最新設定，包含 OP_HEDGE_CONFIG_JSON
    """
    env_config = {}
    if os.path.exists(env_path):
        try:
            with open(env_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '=' in line:
                        k, v = line.split('=', 1)
                        env_config[k.strip()] = v.strip().strip("'").strip('"')
        except Exception as e:
            print(f"[警告] 讀取 .env 檔案失敗: {e}")

    hedge_config = {}
    raw_json = env_config.get('OP_HEDGE_CONFIG_JSON', '{}')
    try:
        hedge_config = json.loads(raw_json)
    except Exception as e:
        print(f"[錯誤] 即時解析 OP_HEDGE_CONFIG_JSON 失敗: {e}")

    return env_config, hedge_config


# ==============================================================================
# 1. 交易標的與商品映射設定 (支援指數期權 SPX, NDX 與各類商品期貨期權)
# ==============================================================================
INDEX_SYMBOLS = {'SPX', 'NDX'}

INDEX_CONFIG_MAP = {
    'SPX': {'exchange': 'CBOE', 'currency': 'USD', 'preferred_class': 'SPX'},
    'NDX': {'exchange': 'NASDAQ', 'currency': 'USD', 'preferred_class': 'NDX'}
}

# 動態目標商品映射表 (Option Trading Class / Micro Symbol -> Standard Underlying Symbol)
FUTURE_SYMBOL_MAP = {
    'SPX': 'SPX', 'SPXW': 'SPX',
    'NDX': 'NDX', 'NDXP': 'NDX',
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

# 優先/標準月度選擇權交易類別映射表
PREFERRED_TRADING_CLASS_MAP = {
    'SPX': 'SPX',
    'NDX': 'NDX',
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

# 預設動態翼寬設定 (若 OP_HEDGE_CONFIG_JSON 未個別指定時的預設值)
WING_WIDTH_MAP = {
    'SPX': 100,
    'NDX': 250,
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

# 最小跳動點 (Tick Size) 映射表
MIN_TICK_MAP = {
    'SPX': 0.05,
    'NDX': 0.10,
    'ES': 0.25, 'NQ': 0.25, 'RTY': 0.1,
    'ZC': 0.125, 'HG': 0.0005, 'GC': 0.1,
    'EUR': 0.00005, 'JPY': 0.0000005,
    'NG': 0.001, 'CL': 0.01
}

# 全域預設參數 (可由 OP_HEDGE_CONFIG_JSON 中的個別商品覆蓋)
DEFAULT_MIN_DTE = 30
DEFAULT_MAX_DTE = 60
EXIT_DTE = 21
TRADE_QTY = 1

ib = IB()


def connect_ib():
    """連接 IBKR TWS / Gateway"""
    env_config, _ = load_env_config()
    ib_host = env_config.get('IB_HOST', '127.0.0.1')
    ib_port = int(env_config.get('IB_PORT', 4001))
    client_id = int(env_config.get('IB_CLIENT_ID', 100)) + 6

    if not ib.isConnected():
        ib.connect(ib_host, ib_port, clientId=client_id)
        raw_send = str(env_config.get('OP_SEND_WEBHOOK', 'false')).strip().lower()
        send_order = raw_send in ('true', '1')
        print(f"=== [系統] 成功連接至 IBKR ({ib_host}:{ib_port}) | 實盤送單模式: {send_order} ===")


def send_webhook_notification(action: str, symbol: str, qty: float, price: float, note: str = ""):
    """發送 LINE 推播通知"""
    payload = {
        "symbol": symbol,
        "action": action,
        "quantity": str(qty),
        "price": str(price),
        "strategy_name": "short_butterfly_spread",
        "note": note
    }
    msg = f"選擇權交易委託/成交: {action} {qty}口 @ {price}\n策略: Short Butterfly Spread\n備註: {note}"
    try:
        send_trade_notification(symbol, msg, payload)
    except Exception as e:
        print(f"-> ❌ 推播通知發送異常: {e}")


# ==============================================================================
# 2. 動態解析群組商品 (支援 SPX / NDX 指數與期貨商品)
# ==============================================================================
def resolve_group_symbols(group_name, symbols):
    """
    動態解析群組中的標的。
    回傳: (underlying_contract, opt_class, chains, sec_type)
    sec_type: 'IND' (指數) 或 'FUT' (期貨)
    """
    underlying_sym = None
    for sym in symbols:
        if sym in FUTURE_SYMBOL_MAP:
            underlying_sym = FUTURE_SYMBOL_MAP[sym]
            break
    if not underlying_sym and symbols:
        underlying_sym = FUTURE_SYMBOL_MAP.get(symbols[0], symbols[0])

    if not underlying_sym:
        return None, None, [], None

    # --- 處理現金指數期權 (SPX, NDX) ---
    if underlying_sym in INDEX_SYMBOLS:
        idx_cfg = INDEX_CONFIG_MAP[underlying_sym]
        ind_contract = Index(symbol=underlying_sym, exchange=idx_cfg['exchange'], currency=idx_cfg['currency'])
        details = ib.reqContractDetails(ind_contract)
        if not details:
            # 嘗試 SMART 交易所
            ind_contract = Index(symbol=underlying_sym, exchange='SMART', currency=idx_cfg['currency'])
            details = ib.reqContractDetails(ind_contract)

        if details:
            underlying_contract = details[0].contract
            chains = ib.reqSecDefOptParams(underlying_contract.symbol, '', underlying_contract.secType, underlying_contract.conId)
            opt_class = idx_cfg['preferred_class']
            trading_classes = [c.tradingClass for c in chains] if chains else []
            if opt_class not in trading_classes and chains:
                opt_class = chains[0].tradingClass
            return underlying_contract, opt_class, chains, 'IND'
        else:
            print(f"[錯誤] 無法取得指數合約詳情: {underlying_sym}")
            return None, None, [], None

    # --- 處理期貨期權 (ES, NQ, CL, GC, ZC, HG, NG, RTY, EUR, JPY) ---
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

        return underlying_fut, opt_class, chains, 'FUT'

    return None, None, [], None


# ==============================================================================
# 3. 尋找對稱翼寬 Short Butterfly Spread 履約價
# ==============================================================================
def select_butterfly_strikes(sorted_strikes, ref_price, target_wing_width):
    """
    選取對稱 Short Butterfly Spread 的三個履約價:
    - Center Strike (K_center): 距離現價最近的 ATM 履約價
    - Lower Strike (K_lower): K_center - W
    - Upper Strike (K_upper): K_center + W
    優先尋找完全對稱之翼寬；若無，則選取最接近目標翼寬者。
    """
    if not sorted_strikes:
        return None, None, None, target_wing_width

    # 1. 尋找最接近現價的中心履約價 (ATM)
    center = min(sorted_strikes, key=lambda s: abs(s - ref_price))

    lowers = [s for s in sorted_strikes if s < center]
    uppers = [s for s in sorted_strikes if s > center]

    if not lowers or not uppers:
        return None, None, None, target_wing_width

    # 2. 尋找完全對稱的翼寬 (K_center - W, K_center + W)
    symmetric_pairs = []
    upper_set = set(uppers)
    for l in lowers:
        w = round(center - l, 6)
        u = round(center + w, 6)
        # 比對浮點精度
        matched_u = next((up for up in uppers if abs(up - u) < 1e-5), None)
        if matched_u is not None:
            diff = abs(w - target_wing_width)
            symmetric_pairs.append((diff, l, center, matched_u, w))

    if symmetric_pairs:
        # 選取與 target_wing_width 差距最小的對稱組合
        symmetric_pairs.sort(key=lambda x: x[0])
        _, best_l, best_c, best_u, actual_w = symmetric_pairs[0]
        return best_l, best_c, best_u, actual_w
    else:
        # 若無完全對稱履約價，選取兩側最接近的履約價
        best_l = min(lowers, key=lambda s: abs(s - (center - target_wing_width)))
        best_u = min(uppers, key=lambda s: abs(s - (center + target_wing_width)))
        actual_w = round(min(center - best_l, best_u - center), 4)
        return best_l, center, best_u, actual_w


# ==============================================================================
# 4. 獲取 Short Butterfly Spread 腿部合約與即時數據
# ==============================================================================
def get_short_butterfly_legs(underlying, opt_class, chains, sec_type, dte_start, dte_end, wing_width):
    """
    根據商品設定的 [dte_start, dte_end] 與 wing_width，篩選並鎖定 Short Butterfly Spread 合約
    Short Butterfly:
      - 賣出 1 口 Lower Call (K - W)
      - 買入 2 口 Center Call (K, ATM)
      - 賣出 1 口 Upper Call (K + W)
    """
    today = datetime.date.today()
    class_candidates = []

    # 1. 優先從指定交易類別 (opt_class) 尋找符合 [dte_start, dte_end] 的到期日
    if opt_class:
        for c in chains:
            if c.tradingClass == opt_class:
                for exp in c.expirations:
                    try:
                        dte = (datetime.datetime.strptime(exp, '%Y%m%d').date() - today).days
                        if dte_start <= dte <= dte_end:
                            class_candidates.append((c.tradingClass, exp, c.strikes, dte))
                    except Exception:
                        pass

    # 2. 若該 opt_class 找不到，擴大至標準非微型類別
    if not class_candidates:
        for c in chains:
            if not c.tradingClass.startswith(('M', 'W', 'X')) and c.tradingClass not in ['XC', 'YC', 'MJY', 'M6E', 'MHG', 'M2K']:
                for exp in c.expirations:
                    try:
                        dte = (datetime.datetime.strptime(exp, '%Y%m%d').date() - today).days
                        if dte_start <= dte <= dte_end:
                            class_candidates.append((c.tradingClass, exp, c.strikes, dte))
                    except Exception:
                        pass

    # 3. 備用方案: 若特定範圍內無合約，取大於 EXIT_DTE 的最近到期合約
    if not class_candidates:
        for c in chains:
            for exp in c.expirations:
                try:
                    dte = (datetime.datetime.strptime(exp, '%Y%m%d').date() - today).days
                    if dte > EXIT_DTE:
                        class_candidates.append((c.tradingClass, exp, c.strikes, dte))
                except Exception:
                    pass

    if not class_candidates:
        print(f"[錯誤] 找不到 {underlying.symbol} (Class: {opt_class}) 符合 DTE 範圍 ({dte_start}~{dte_end}天) 的期權合約到期日")
        return None

    # 選取 DTE 最接近目標區間中位數的到期日
    target_dte = (dte_start + dte_end) / 2.0
    best_candidate = min(class_candidates, key=lambda item: abs(item[3] - target_dte))
    chosen_opt_class, closest_expiry, strikes, current_dte = best_candidate

    print(f"-> 鎖定 {underlying.symbol} (Class: {chosen_opt_class}) 到期日: {closest_expiry} (DTE: {current_dte} 天，範圍 {dte_start}~{dte_end} 天)")

    # 取得標的最新市價以決定履約價範圍
    underlying_ticker = ib.reqTickers(underlying)
    ib.sleep(1)
    ref_price = underlying_ticker[0].marketPrice() if underlying_ticker else 0
    if not ref_price or ref_price <= 0:
        ref_price = underlying_ticker[0].close if underlying_ticker and underlying_ticker[0].close else 0

    if not ref_price or ref_price <= 0:
        print(f"[錯誤] 無法取得 {underlying.symbol} 最新市價，無法計算 ATM 履約價。")
        return None

    # 過濾履約價範圍 (現價 ±25%)，避免請求過量合約
    filtered_strikes = [s for s in strikes if ref_price * 0.75 <= s <= ref_price * 1.25]
    if not filtered_strikes:
        filtered_strikes = list(strikes)

    # 建立 Call 合約清單 (Butterfly Spread 使用同月份 Call 組合)
    if sec_type == 'IND':
        options = [Option(underlying.symbol, closest_expiry, strike, 'C', 'SMART', tradingClass=chosen_opt_class)
                   for strike in sorted(filtered_strikes)]
    else:
        options = [FuturesOption(underlying.symbol, closest_expiry, strike, 'C', underlying.exchange, tradingClass=chosen_opt_class)
                   for strike in sorted(filtered_strikes)]

    qualified = ib.qualifyContracts(*options)
    valid_strikes_set = set(c.strike for c in qualified)
    sorted_strikes = sorted(list(valid_strikes_set))

    if not sorted_strikes:
        print(f"[錯誤] {underlying.symbol} 無有效可驗證履約價。")
        return None

    # 選取 Short Butterfly 三條腿的履約價
    k_lower, k_center, k_upper, actual_wing = select_butterfly_strikes(sorted_strikes, ref_price, wing_width)

    if k_lower is None or k_center is None or k_upper is None:
        print(f"[錯誤] 無法為 {underlying.symbol} 配對合適的蝶式價差履約價 (現價: {ref_price}, 目標翼寬: {wing_width})。")
        return None

    c_lower = next(c for c in qualified if c.strike == k_lower)
    c_center = next(c for c in qualified if c.strike == k_center)
    c_upper = next(c for c in qualified if c.strike == k_upper)

    # 取得這三檔合約的 Greeks 與報價
    print(f"-> 正在取得 {underlying.symbol} 蝶式三腿報價與 Greeks... (下翼: {k_lower}, 中心: {k_center}, 上翼: {k_upper})")
    leg_tickers = ib.reqTickers(c_lower, c_center, c_upper)
    ib.sleep(2)

    t_lower = next((t for t in leg_tickers if t.contract.strike == k_lower), None)
    t_center = next((t for t in leg_tickers if t.contract.strike == k_center), None)
    t_upper = next((t for t in leg_tickers if t.contract.strike == k_upper), None)

    # 計算組合總 Greeks (Short Butterfly = -1*Lower + 2*Center - 1*Upper)
    def _get_greeks(t):
        if t and t.modelGreeks:
            d = t.modelGreeks.delta or 0.0
            th = t.modelGreeks.theta or 0.0
            g = t.modelGreeks.gamma or 0.0
            return d, th, g
        return 0.0, 0.0, 0.0

    d_l, th_l, g_l = _get_greeks(t_lower)
    d_c, th_c, g_c = _get_greeks(t_center)
    d_u, th_u, g_u = _get_greeks(t_upper)

    total_delta = -1.0 * d_l + 2.0 * d_c - 1.0 * d_u
    total_theta = -1.0 * th_l + 2.0 * th_c - 1.0 * th_u
    total_gamma = -1.0 * g_l + 2.0 * g_c - 1.0 * g_u

    print(
        f"-> {underlying.symbol} Short Butterfly 組合確認: "
        f"賣出 Call {k_lower} (Δ {d_l:+.3f}) | "
        f"買入 2x Call {k_center} (Δ {d_c:+.3f}) | "
        f"賣出 Call {k_upper} (Δ {d_u:+.3f}) | "
        f"翼寬: {actual_wing} | DTE: {current_dte} 天 | 總淨 DELTA: {total_delta:+.3f} | 總 Theta: {total_theta:.2f}"
    )

    exchange_name = 'SMART' if sec_type == 'IND' else underlying.exchange

    return {
        'symbol': underlying.symbol,
        'exchange': exchange_name,
        'sec_type': sec_type,
        'c_lower': c_lower,
        'c_center': c_center,
        'c_upper': c_upper,
        't_lower': t_lower,
        't_center': t_center,
        't_upper': t_upper,
        'k_lower': k_lower,
        'k_center': k_center,
        'k_upper': k_upper,
        'wing_width': actual_wing,
        'dte': current_dte,
        'total_delta': total_delta,
        'total_theta': total_theta,
        'total_gamma': total_gamma
    }


# ==============================================================================
# 5. 建立並送出 Short Butterfly Spread 組合單 (全部下 BID LIMIT)
# ==============================================================================
def execute_short_butterfly(legs):
    """
    透過 IBKR BAG 組合單執行 Short Butterfly Spread:
    標準蝶式定義 (買1下翼 + 賣2中心 + 買1上翼)，透過 SELL 委託單送出，精準成交為:
      - 賣出 1 口 Lower Call
      - 買入 2 口 Center Call
      - 賣出 1 口 Upper Call
    ★ 下單限制: 全部以 BID LIMIT (買方出價限價單) 掛單！
    """
    env_config, _ = load_env_config()
    raw_send = str(env_config.get('OP_SEND_WEBHOOK', 'false')).strip().lower()
    send_order = raw_send in ('true', '1')

    exchange = legs['exchange']
    combo_contract = Contract(symbol=legs['symbol'], secType='BAG', currency='USD', exchange=exchange)

    # 組合腿定義:
    # Action BUY (ratio 1) on Lower, Action SELL (ratio 2) on Center, Action BUY (ratio 1) on Upper
    # 當對此 Combo 執行 SELL 訂單時，Lower 與 Upper 被 SELL，Center 被 BUY，完美構成 Short Butterfly
    leg_ex_lower = legs['c_lower'].exchange or exchange
    leg_ex_center = legs['c_center'].exchange or exchange
    leg_ex_upper = legs['c_upper'].exchange or exchange

    combo_contract.comboLegs = [
        ComboLeg(conId=legs['c_lower'].conId, ratio=1, action='BUY', exchange=leg_ex_lower),
        ComboLeg(conId=legs['c_center'].conId, ratio=2, action='SELL', exchange=leg_ex_center),
        ComboLeg(conId=legs['c_upper'].conId, ratio=1, action='BUY', exchange=leg_ex_upper)
    ]

    ticker = ib.reqMktData(combo_contract, '', False, False)
    ib.sleep(2)

    # 取得當前市場 BID 價格
    combo_bid = ticker.bid if ticker.bid and ticker.bid > 0 else 0.0

    # 若組合行情無即時 Bid，使用各腿報價合成 Bid (賣Lower取Bid - 2*買Center取Ask + 賣Upper取Bid)
    if combo_bid <= 0:
        bid_l = (legs['t_lower'].bid or 0.0) if legs['t_lower'] else 0.0
        ask_c = (legs['t_center'].ask or 0.0) if legs['t_center'] else 0.0
        bid_u = (legs['t_upper'].bid or 0.0) if legs['t_upper'] else 0.0
        synthetic_bid = bid_l - 2.0 * ask_c + bid_u
        if synthetic_bid > 0:
            combo_bid = synthetic_bid
            print(f"-> 使用各腿合成 BID 計算價格: {combo_bid:.4f} (Leg L Bid: {bid_l}, C Ask: {ask_c}, U Bid: {bid_u})")

    min_tick = MIN_TICK_MAP.get(legs['symbol'], 0.01)

    if combo_bid > 0:
        limit_price = round(round(combo_bid / min_tick) * min_tick, 6)
    else:
        # 若報價完全缺漏，以最近成交價或收盤價為基準
        fallback_p = ticker.close or ticker.last or (min_tick * 10)
        limit_price = round(round(fallback_p / min_tick) * min_tick, 6)
        print(f"-> ⚠️ 未取得即時 BID 報價，退回參考價: {limit_price}")

    # ★ 嚴格下單指令: 全部下 BID LIMIT
    order = LimitOrder('SELL', TRADE_QTY, limit_price)
    order.tif = 'DAY'

    order_desc = (
        f"Short Butterfly Spread (SC {legs['k_lower']} / 2x BC {legs['k_center']} / SC {legs['k_upper']}) | "
        f"下單型態: BID LIMIT | 限價: ${limit_price} | 數量: {TRADE_QTY}口 | DTE: {legs['dte']}天 | "
        f"淨Δ: {legs['total_delta']:+.3f} | θ: {legs['total_theta']:.2f}"
    )

    if send_order:
        trade = ib.placeOrder(combo_contract, order)
        print(f"=== [下單成功] 已送出 BID LIMIT 訂單: {legs['symbol']} | {order_desc} ===")

        # 等待成交監控 (最多等待 60 秒)
        end_time = time.time() + 60
        while time.time() < end_time:
            ib.sleep(1)
            if trade.orderStatus.status in ['Filled', 'Cancelled']:
                break

        if trade.orderStatus.status == 'Filled':
            fill_price = trade.orderStatus.avgFillPrice
            print(f"=== [成交確認] {legs['symbol']} Short Butterfly 已成交，成交均價: {fill_price} ===")
            note_str = f"Short Butterfly (SC {legs['k_lower']}/BC 2x{legs['k_center']}/SC {legs['k_upper']}, 成交: {fill_price}, DTE: {legs['dte']}天)"
            send_webhook_notification('SELL_BUTTERFLY', legs['symbol'], TRADE_QTY, fill_price, note_str)
        else:
            print(f"=== [委託掛單中] {legs['symbol']} 目前委託狀態: {trade.orderStatus.status} (BID LIMIT: {limit_price}) ===")
    else:
        print(
            f"=== [測試模式 (不送單)] ===\n"
            f"-> 標的: {legs['symbol']} | {order_desc}\n"
            f"-> (若開啟 OP_SEND_WEBHOOK=true，將自動掛出 BID LIMIT 限價單: ${limit_price})"
        )

    ib.sleep(2)


# ==============================================================================
# 6. 主執行程序 (即時查詢 .env 中的 OP_HEDGE_CONFIG_JSON)
# ==============================================================================
def run_option_cycle():
    """
    每次執行時動態從 trade/.env 讀取最新配置並進行期權對沖掃描
    """
    connect_ib()

    # 即時查詢 trade/.env 中的最新配置
    env_config, op_hedge_config = load_env_config()

    if not op_hedge_config:
        print("[警告] trade/.env 內未設定 OP_HEDGE_CONFIG_JSON，或內容為空。")
        return

    print(f"\n================ 開始進行期權掃描 (共 {len(op_hedge_config)} 個商品群組) ================")

    for g_name, g_info in op_hedge_config.items():
        symbols = g_info.get('symbols', [])
        # 1. 取得該商品的專屬 DTE 設定 (開始日與結束日)
        dte_start = int(g_info.get('dte_start') or g_info.get('min_dte') or g_info.get('start_dte') or DEFAULT_MIN_DTE)
        dte_end = int(g_info.get('dte_end') or g_info.get('max_dte') or g_info.get('end_dte') or DEFAULT_MAX_DTE)

        print(f"\n>> 處理群組: {g_name} | 代號: {symbols} | 設定 DTE 區間: {dte_start}~{dte_end} 天")

        # 2. 解析標的與期權結構 (支援指數 SPX / NDX 與商品期貨)
        underlying, opt_class, chains, sec_type = resolve_group_symbols(g_name, symbols)
        if not underlying:
            print(f"[略過] 無法解析 {g_name} 的期權合約結構。")
            continue

        # 3. 取得該商品的專屬動態翼寬 WING_WIDTH (優先取個別商品設定，次取全域映射表)
        underlying_symbol = underlying.symbol
        custom_wing = g_info.get('wing_width') or g_info.get('wing')
        if custom_wing is not None:
            wing_width = float(custom_wing)
        else:
            wing_width = float(WING_WIDTH_MAP.get(underlying_symbol, 50.0))

        print(f"-> 標的合約: {underlying_symbol} ({sec_type}) | 選擇權類別: {opt_class} | 動態翼寬 WING_WIDTH: {wing_width}")

        # 4. 檢查目前是否已有該標的的期權部位
        portfolio_positions = ib.portfolio()
        current_pos = [
            p for p in portfolio_positions
            if p.contract.symbol == underlying_symbol and p.contract.secType in ['FOP', 'OPT'] and p.position != 0
        ]

        if not current_pos:
            print(f"-> {underlying_symbol} 無持倉，開始建立 Short Butterfly Spread...")
            legs = get_short_butterfly_legs(
                underlying=underlying,
                opt_class=opt_class,
                chains=chains,
                sec_type=sec_type,
                dte_start=dte_start,
                dte_end=dte_end,
                wing_width=wing_width
            )
            if legs:
                execute_short_butterfly(legs)
        else:
            print(f"-> {underlying_symbol} 已有選擇權持倉 ({len(current_pos)} 檔合約)，跳過建倉。")


if __name__ == '__main__':
    try:
        run_option_cycle()
    except KeyboardInterrupt:
        print("\n[中斷] 使用者中斷執行。")
    except Exception as e:
        print(f"\n[錯誤] 執行異常: {e}")
    finally:
        if ib.isConnected():
            ib.disconnect()
            print("=== [系統] 已中斷與 IBKR 連線 ===")
