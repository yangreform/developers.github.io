#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
open_Shioaji.py
==============================================================================
永豐 Shioaji 選擇權雙向價差開倉程式 (Bear Call Spread + Bull Put Spread)

策略架構 (共 4 口):
  Center ATM:       {legs['center_strike']}
  Upper Wing Call:  {legs['call_wing_strike']} (+{legs['wing_width']}) -> 買方一口
  Lower Wing Put:   {legs['put_wing_strike']} (-{legs['wing_width']})  -> 買方一口
  
  1. 買權空頭價差 (Bear Call Spread):
     - 賣出 (Sell) ATM Call ({legs['center_strike']}) 1口
     - 買入 (Buy)  Upper Wing Call ({legs['call_wing_strike']}) 1口
  
  2. 賣權多頭價差 (Bull Put Spread):
     - 賣出 (Sell) ATM Put ({legs['center_strike']}) 1口
     - 買入 (Buy)  Lower Wing Put ({legs['put_wing_strike']}) 1口

安全機制:
  - 開倉時優先買進外側翅膀 (Long Call & Long Put)，確保保證金充裕並限制單邊最大風險，隨後賣出中心 ATM。
  - 支援 --dry-run 模擬試算模式，不進行實際下單。
  - 下單後自動推播詳細報告至 LINE。
==============================================================================
"""

import os
import sys
import time
import math
import argparse
import datetime
from typing import Dict, List, Optional, Tuple

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
        sys.stderr.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
    except Exception:
        pass

import json
import shioaji as sj
from shioaji.contracts import Option, ComboBase, ComboContract
from shioaji.order import Order, ComboOrder
from dotenv import load_dotenv, find_dotenv

# 引入本機通知模組
try:
    from notifier import send_push_message
except ImportError:
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from notifier import send_push_message


# ==============================================================================
# 🔐 環境變數與登入
# ==============================================================================
def env_int(name: str, default: int = 400) -> int:
    """
    即時讀取 trade/.env 中的整數設定 (純讀取，不寫回 .env)。
    優先順序:
      1. OP_HEDGE_CONFIG_JSON 內台指/TXO 區塊的 wing_width
      2. trade/.env 中同名環境變數 (例如: wing_width 或 WING_WIDTH)
      3. 預設值 default (400)
    """
    dotenv_path = find_dotenv()
    if not dotenv_path:
        dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(dotenv_path):
        load_dotenv(dotenv_path, override=True)

    raw_json = os.getenv("OP_HEDGE_CONFIG_JSON", "")
    if raw_json:
        try:
            op_cfg = json.loads(raw_json)
            # 優先搜尋台指/TXO/TMF/MXF 相關商品區塊
            for key in ["台指(TXO)", "台指", "TXO", "小台(TXO)", "小台", "TMF", "MXF"]:
                if key in op_cfg and isinstance(op_cfg[key], dict) and name in op_cfg[key]:
                    return int(op_cfg[key][name])
            # 或遍歷 symbols 含有 TXO/TX/TMF/MXF 的群組
            for k, val in op_cfg.items():
                if isinstance(val, dict):
                    syms = val.get("symbols", [])
                    if any(s in syms for s in ["TXO", "TX", "TMF", "MXF"]):
                        if name in val:
                            return int(val[name])
            # 若最外層有該欄位
            if name in op_cfg:
                return int(op_cfg[name])
        except Exception:
            pass

    val = os.getenv(name) or os.getenv(name.upper()) or os.getenv(name.lower())
    if val is not None and str(val).strip():
        try:
            return int(float(str(val).strip()))
        except (ValueError, TypeError):
            pass

    return default


def env_float(name: str, default: float = 0.0) -> float:
    """
    即時讀取 trade/.env 中的浮點數設定 (純讀取，不寫回 .env)。
    優先順序:
      1. OP_HEDGE_CONFIG_JSON 內台指/TXO 區塊的欄位 (例如 price_diff)
      2. trade/.env 中同名環境變數 (例如: price_diff 或 PRICE_DIFF)
      3. 預設值 default (0.0)
    """
    dotenv_path = find_dotenv()
    if not dotenv_path:
        dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(dotenv_path):
        load_dotenv(dotenv_path, override=True)

    raw_json = os.getenv("OP_HEDGE_CONFIG_JSON", "")
    if raw_json:
        try:
            op_cfg = json.loads(raw_json)
            # 優先搜尋台指/TXO/TMF/MXF 相關商品區塊
            for key in ["台指(TXO)", "台指(TMF)", "台指", "TXO", "小台(TXO)", "小台", "TMF", "MXF"]:
                if key in op_cfg and isinstance(op_cfg[key], dict) and name in op_cfg[key]:
                    return float(op_cfg[key][name])
            # 或遍歷 symbols 含有 TXO/TX/TMF/MXF 的群組
            for k, val in op_cfg.items():
                if isinstance(val, dict):
                    syms = val.get("symbols", [])
                    if any(s in syms for s in ["TXO", "TX", "TMF", "MXF"]):
                        if name in val:
                            return float(val[name])
            # 若最外層有該欄位
            if name in op_cfg:
                return float(op_cfg[name])
        except Exception:
            pass

    val = os.getenv(name) or os.getenv(name.upper()) or os.getenv(name.lower())
    if val is not None and str(val).strip():
        try:
            return float(str(val).strip())
        except (ValueError, TypeError):
            pass

    return default


# ==============================================================================
# 📐 Black-76 選擇權 Greeks 計算 (支援 DTE 3 週選擇權與基差點位修正)
# ==============================================================================
def calculate_option_delta(
    f: float,
    k: float,
    t: float,
    r: float = 0.01,
    sigma: float = 0.20,
    option_type: str = "C"
) -> float:
    """
    計算選擇權 Black-76 模型 Delta (純標準庫 math，無需 scipy)。
    f: 標的期貨參考價 (含 price_diff 修正)
    k: 履約價
    t: 年化到期時間 (dte / 365.0)
    sigma: 隱含波動率
    option_type: 'C' 或 'P'
    """
    if t <= 0 or f <= 0 or k <= 0:
        return 0.0
    sigma = max(0.01, sigma)
    d1 = (math.log(f / k) + 0.5 * sigma**2 * t) / (sigma * math.sqrt(t))
    norm_cdf_d1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
    df = math.exp(-r * t)
    if option_type.upper() == "C":
        return float(df * norm_cdf_d1)
    else:
        return float(df * (norm_cdf_d1 - 1.0))


def implied_volatility_black76(
    f: float,
    k: float,
    t: float,
    option_price: float,
    option_type: str = "C",
    r: float = 0.01,
    default_iv: float = 0.20
) -> float:
    """
    使用二分逼近法求 Black-76 隱含波動率。
    """
    if t <= 0 or f <= 0 or k <= 0 or option_price <= 0:
        return default_iv

    def b76_price(sig: float) -> float:
        d1 = (math.log(f / k) + 0.5 * sig**2 * t) / (sig * math.sqrt(t))
        d2 = d1 - sig * math.sqrt(t)
        cdf1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
        cdf2 = 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))
        if option_type.upper() == "C":
            return math.exp(-r * t) * (f * cdf1 - k * cdf2)
        else:
            return math.exp(-r * t) * (k * (1.0 - cdf2) - f * (1.0 - cdf1))

    low, high = 0.01, 5.0
    p_low = b76_price(low)
    p_high = b76_price(high)
    if option_price <= p_low:
        return low
    if option_price >= p_high:
        return high

    for _ in range(32):
        mid = (low + high) / 2.0
        p = b76_price(mid)
        if abs(p - option_price) < 0.05:
            return mid
        if p < option_price:
            low = mid
        else:
            high = mid
    return mid


def load_config() -> dict:
    """載入 trade/.env 設定檔"""
    dotenv_path = find_dotenv()
    if not dotenv_path:
        dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(dotenv_path):
        load_dotenv(dotenv_path, override=True)

    config = {
        "api_key": os.getenv("SHIOAJI_API_KEY", "").strip(),
        "secret_key": os.getenv("SHIOAJI_SECRET_KEY", "").strip(),
        "ca_path": os.getenv("SHIOAJI_CA_PATH", "").strip(),
        "ca_passwd": os.getenv("SHIOAJI_CA_PASSWD", "").strip(),
    }
    return config


def init_shioaji(config: dict) -> Tuple[sj.Shioaji, Dict[str, List[Option]], Dict[str, Option]]:
    """初始化並登入 Shioaji，同時快取選擇權合約"""
    if not config["api_key"] or not config["secret_key"]:
        raise RuntimeError("❌ 缺少 SHIOAJI_API_KEY 或 SHIOAJI_SECRET_KEY，請檢查 trade/.env 設定")

    api = sj.Shioaji()
    api.login(config["api_key"], config["secret_key"])
    print(f"✅ Shioaji 登入成功 (帳號: {api.futopt_account.account_id if api.futopt_account else '未綁定期貨帳號'})")

    # 啟用憑證 CA
    ca_path = config["ca_path"]
    if ca_path:
        if not os.path.isabs(ca_path):
            ca_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ca_path)
        if os.path.exists(ca_path):
            ca_ok = api.activate_ca(ca_path=ca_path, ca_passwd=config["ca_passwd"])
            print(f"🔑 憑證啟用狀態: {'成功' if ca_ok else '失敗'}")
        else:
            print(f"⚠️ 找不到憑證檔案: {ca_path}")
    else:
        print("⚠️ 未指定 SHIOAJI_CA_PATH，若進行實盤下單可能會被拒絕")

    # 快取台指選擇權合約 (涵蓋週三到期: TX1, TX2, TX3, TX4, TX5, TXO; 週五到期: TXU, TXV, TXX, TXY, TXZ 及所有 TX 開頭類別)
    txo_by_date: Dict[str, List[Option]] = {}
    txo_by_code: Dict[str, Option] = {}
    target_cats = ['TX1', 'TX2', 'TX3', 'TX4', 'TX5', 'TXO', 'TXU', 'TXV', 'TXX', 'TXY', 'TXZ']
    all_tx_cats = sorted(list(set(target_cats + [attr for attr in dir(api.Contracts.Options) if attr.startswith("TX")])))
    for cat in all_tx_cats:
        if hasattr(api.Contracts.Options, cat):
            for c in getattr(api.Contracts.Options, cat):
                d = c.delivery_date
                if d not in txo_by_date:
                    txo_by_date[d] = []
                txo_by_date[d].append(c)
                txo_by_code[c.code] = c

    return api, txo_by_date, txo_by_code


# ==============================================================================
# 📈 取得標的價格與計算履約價
# ==============================================================================
def get_underlying_price(api: sj.Shioaji, manual_price: Optional[float] = None) -> Tuple[str, float]:
    """
    取得最新台指期貨價格作為標的參考價。
    優先順序: 手動指定 > 微台 (TMF) > 小台 (MXF) > 大台 (TXF)
    若今天為期貨到期結算日且已過 13:30，自動選取次近月活躍期貨。
    """
    if manual_price and manual_price > 0:
        return "手動指定", float(manual_price)

    today_str = datetime.date.today().strftime("%Y/%m/%d")
    now_time = datetime.datetime.now().time()
    is_after_settlement = (now_time >= datetime.time(13, 30))
    fut_candidates = []

    # 搜尋近月期貨 (排除今日已過 13:30 結算之合約)
    for cat in ['TMF', 'MXF', 'TXF']:
        if hasattr(api.Contracts.Futures, cat):
            for c in getattr(api.Contracts.Futures, cat):
                if c.delivery_date < today_str or (c.delivery_date == today_str and is_after_settlement):
                    continue
                fut_candidates.append(c)

    fut_candidates.sort(key=lambda x: (x.delivery_date, x.category != 'TMF'))
    if not fut_candidates:
        raise RuntimeError("❌ 找不到可用的台指期貨合約")

    target_fut = fut_candidates[0]
    snaps = api.snapshots([target_fut])
    if not snaps:
        raise RuntimeError(f"❌ 無法取得期貨合約 {target_fut.code} 快照")

    snap = snaps[0]
    price = snap.close if snap.close > 0 else (snap.reference if snap.reference > 0 else (snap.buy_price + snap.sell_price) / 2.0)
    if price <= 0:
        price = snap.reference

    return f"{target_fut.code} ({target_fut.symbol})", float(price)


def select_delivery_date(txo_by_date: Dict[str, List[Option]], target_date: Optional[str] = None) -> Tuple[str, int]:
    """
    選擇目標選擇權到期日與計算 DTE。
    【自動選擇規則】：排除今天已結算/到期之合約 (要求 DTE > 0)，並選取 DTE 最小 (最鄰近未來交易日) 的到期日。
    涵蓋週三到期 (TX1, TX2, TX4, TX5, TXO) 與週五到期 (TXU, TXV, TXX, TXY, TXZ)。
    """
    today = datetime.date.today()

    if target_date:
        cleaned = target_date.replace("-", "/").replace(".", "/")
        if len(cleaned) == 8 and cleaned.isdigit():
            cleaned = f"{cleaned[:4]}/{cleaned[4:6]}/{cleaned[6:]}"
        if cleaned in txo_by_date:
            selected = cleaned
            try:
                exp_dt = datetime.datetime.strptime(selected, "%Y/%m/%d").date()
                dte = (exp_dt - today).days
            except Exception:
                dte = 0
            return selected, dte
        else:
            raise ValueError(f"❌ 指定的到期日 {target_date} 不在可用列表: {sorted(list(txo_by_date.keys()))}")

    # 自動篩選 DTE > 0 且 DTE 最小的到期日 (包含星期五到期的 TXU, TXV, TXX, TXY, TXZ 與週三 TX1, TX2, TX4 等)
    candidates = []
    for d in txo_by_date.keys():
        try:
            exp_dt = datetime.datetime.strptime(d, "%Y/%m/%d").date()
            dte = (exp_dt - today).days
            if dte > 0:
                candidates.append((d, dte))
        except Exception:
            continue

    if not candidates:
        raise RuntimeError("❌ 沒有找到 DTE > 0 的未到期選擇權合約")

    # 排序取 DTE 最小者
    candidates.sort(key=lambda x: (x[1], x[0]))
    selected, dte = candidates[0]
    return selected, dte


def find_option_contract(
    contracts: List[Option],
    strike: float,
    right: str
) -> Optional[Option]:
    """在指定到期日的合約列表中尋找符合履約價與買賣權的合約"""
    target_right = right.upper()
    for c in contracts:
        c_right = 'C' if (c.option_right == sj.constant.OptionRight.Call or getattr(c.option_right, 'value', '') == 'C') else 'P'
        if c_right == target_right and abs(c.strike_price - strike) < 0.01:
            return c
    return None


# ==============================================================================
# 🎯 雙向價差架構建置
# ==============================================================================
def calculate_spread_legs(
    api: sj.Shioaji,
    txo_by_date: Dict[str, List[Option]],
    ref_price: float,
    raw_ref_price: Optional[float] = None,
    price_diff: float = 0.0,
    wing_width: float = 200.0,
    strike_step: float = 100.0,
    manual_center: Optional[float] = None,
    delivery_date: Optional[str] = None
) -> dict:
    """
    計算中心 ATM、上翼 Call、下翼 Put，並取得 4 口合約及即時報價與 Black-76 Greeks
    """
    # 1. 決定中心 ATM 與翅膀 (使用含 price_diff 修正後之 ref_price)
    if manual_center and manual_center > 0:
        center_strike = float(manual_center)
    else:
        center_strike = round(ref_price / strike_step) * strike_step

    call_wing_strike = center_strike + wing_width
    put_wing_strike = center_strike - wing_width

    # 2. 決定到期日
    selected_date, dte = select_delivery_date(txo_by_date, delivery_date)
    contracts_for_date = txo_by_date[selected_date]

    # 3. 搜尋 4 口合約
    c_center = find_option_contract(contracts_for_date, center_strike, 'C')
    c_wing   = find_option_contract(contracts_for_date, call_wing_strike, 'C')
    p_center = find_option_contract(contracts_for_date, center_strike, 'P')
    p_wing   = find_option_contract(contracts_for_date, put_wing_strike, 'P')

    missing = []
    if not c_center: missing.append(f"Call ATM ({center_strike})")
    if not c_wing:   missing.append(f"Call Upper Wing ({call_wing_strike})")
    if not p_center: missing.append(f"Put ATM ({center_strike})")
    if not p_wing:   missing.append(f"Put Lower Wing ({put_wing_strike})")
    if missing:
        raise RuntimeError(f"❌ 於到期日 {selected_date} 找不到以下合約: {', '.join(missing)}")

    # 4. 取得報價快照
    target_contracts = [c_wing, c_center, p_center, p_wing]
    snapshots = {s.code: s for s in api.snapshots(target_contracts)}

    def get_order_price(contract: Option, action: sj.constant.Action) -> float:
        """
        以確保即時撮合之報價:
        - 買入 (Buy): 使用賣出價 (Ask)，若無則用收盤價或參考價
        - 賣出 (Sell): 使用買進價 (Bid)，若無則用收盤價或參考價
        """
        s = snapshots.get(contract.code)
        if not s:
            return contract.reference

        if action == sj.constant.Action.Buy:
            if s.sell_price and s.sell_price > 0:
                return float(s.sell_price)
        else:
            if s.buy_price and s.buy_price > 0:
                return float(s.buy_price)

        if s.close and s.close > 0:
            return float(s.close)
        return float(s.reference)

    c_wing_price   = get_order_price(c_wing, sj.constant.Action.Buy)
    c_center_price = get_order_price(c_center, sj.constant.Action.Sell)
    p_center_price = get_order_price(p_center, sj.constant.Action.Sell)
    p_wing_price   = get_order_price(p_wing, sj.constant.Action.Buy)

    # 5. 權利金試算 (台指選擇權一點 = NT$ 50)
    bear_call_credit = c_center_price - c_wing_price
    bull_put_credit  = p_center_price - p_wing_price
    total_credit     = bear_call_credit + bull_put_credit
    max_risk_points  = max(0.0, wing_width - total_credit)

    # 6. 計算各腿 Black-76 Greeks (以修正後參考價 ref_price 為 F，年化到期時間 T = max(dte, 0.5) / 365.0)
    T = max(float(dte), 0.5) / 365.0
    c_wing_iv = implied_volatility_black76(f=ref_price, k=call_wing_strike, t=T, option_price=c_wing_price, option_type="C")
    c_wing_d  = calculate_option_delta(f=ref_price, k=call_wing_strike, t=T, r=0.01, sigma=c_wing_iv, option_type="C")

    c_center_iv = implied_volatility_black76(f=ref_price, k=center_strike, t=T, option_price=c_center_price, option_type="C")
    c_center_d  = calculate_option_delta(f=ref_price, k=center_strike, t=T, r=0.01, sigma=c_center_iv, option_type="C")

    p_center_iv = implied_volatility_black76(f=ref_price, k=center_strike, t=T, option_price=p_center_price, option_type="P")
    p_center_d  = calculate_option_delta(f=ref_price, k=center_strike, t=T, r=0.01, sigma=p_center_iv, option_type="P")

    p_wing_iv = implied_volatility_black76(f=ref_price, k=put_wing_strike, t=T, option_price=p_wing_price, option_type="P")
    p_wing_d  = calculate_option_delta(f=ref_price, k=put_wing_strike, t=T, r=0.01, sigma=p_wing_iv, option_type="P")

    # 價差 Net Delta (賣方部位 Delta 變號)
    # Bear Call = Sell ATM Call + Buy Upper Wing Call
    bear_call_delta = -c_center_d + c_wing_d
    # Bull Put = Sell ATM Put + Buy Lower Wing Put (Put Delta 本身為負，賣出 -(-Delta) 為正)
    bull_put_delta  = -p_center_d + p_wing_d
    total_net_delta = bear_call_delta + bull_put_delta

    legs_info = {
        "center_strike": center_strike,
        "call_wing_strike": call_wing_strike,
        "put_wing_strike": put_wing_strike,
        "wing_width": wing_width,
        "delivery_date": selected_date,
        "dte": dte,
        "ref_price": ref_price,
        "raw_ref_price": raw_ref_price if raw_ref_price is not None else ref_price,
        "price_diff": price_diff,
        "contracts": {
            "c_wing": {
                "contract": c_wing,
                "action": sj.constant.Action.Buy,
                "action_desc": "買入 (BUY)",
                "strike": call_wing_strike,
                "right": "Call",
                "role": "Upper Wing Call",
                "price": c_wing_price,
                "iv": c_wing_iv,
                "delta": c_wing_d,
                "snap": snapshots.get(c_wing.code)
            },
            "c_center": {
                "contract": c_center,
                "action": sj.constant.Action.Sell,
                "action_desc": "賣出 (SELL)",
                "strike": center_strike,
                "right": "Call",
                "role": "Center Call",
                "price": c_center_price,
                "iv": c_center_iv,
                "delta": c_center_d,
                "snap": snapshots.get(c_center.code)
            },
            "p_center": {
                "contract": p_center,
                "action": sj.constant.Action.Sell,
                "action_desc": "賣出 (SELL)",
                "strike": center_strike,
                "right": "Put",
                "role": "Center Put",
                "price": p_center_price,
                "iv": p_center_iv,
                "delta": p_center_d,
                "snap": snapshots.get(p_center.code)
            },
            "p_wing": {
                "contract": p_wing,
                "action": sj.constant.Action.Buy,
                "action_desc": "買入 (BUY)",
                "strike": put_wing_strike,
                "right": "Put",
                "role": "Lower Wing Put",
                "price": p_wing_price,
                "iv": p_wing_iv,
                "delta": p_wing_d,
                "snap": snapshots.get(p_wing.code)
            },
        },
        "bear_call_credit": bear_call_credit,
        "bull_put_credit": bull_put_credit,
        "total_credit": total_credit,
        "total_credit_twd": total_credit * 50.0,
        "max_risk_points": max_risk_points,
        "max_risk_twd": max_risk_points * 50.0,
        "bear_call_delta": bear_call_delta,
        "bull_put_delta": bull_put_delta,
        "total_net_delta": total_net_delta,
    }
    return legs_info


# ==============================================================================
# 🚀 執行下單
# ==============================================================================
def make_combo_base(contract: Option, action: sj.constant.Action) -> ComboBase:
    """將 Option 合約轉為 ComboBase 物件"""
    fields = list(ComboBase.model_fields.keys())
    d = {k: getattr(contract, k) for k in fields if hasattr(contract, k)}
    d['action'] = action
    return ComboBase(**d)


def check_spread_held(
    api: sj.Shioaji,
    sell_code: str,
    buy_code: str,
    target_qty: int = 1
) -> bool:
    """檢查即時帳戶是否已持有指定之價差組合 (一口 Sell + 一口 Buy)"""
    try:
        positions = api.list_positions(account=api.futopt_account)
    except Exception as e:
        print(f"⚠️ 查詢帳戶持倉異常: {e}")
        return False

    has_sell = False
    has_buy = False
    for p in positions:
        is_sell = (p.direction == sj.constant.Action.Sell) or str(p.direction).lower().endswith("sell")
        is_buy  = (p.direction == sj.constant.Action.Buy) or str(p.direction).lower().endswith("buy")
        if p.code == sell_code and is_sell and p.quantity >= target_qty:
            has_sell = True
        elif p.code == buy_code and is_buy and p.quantity >= target_qty:
            has_buy = True
    return has_sell and has_buy


def get_live_combo_credit(api: sj.Shioaji, sell_contract: Option, buy_contract: Option) -> float:
    """取得即時撮合淨價差: 賣方 Bid - 買方 Ask"""
    try:
        snaps = {s.code: s for s in api.snapshots([sell_contract, buy_contract])}
        s_sell = snaps.get(sell_contract.code)
        s_buy = snaps.get(buy_contract.code)
        sell_p = (s_sell.buy_price or s_sell.close or sell_contract.reference) if s_sell else sell_contract.reference
        buy_p = (s_buy.sell_price or s_buy.close or buy_contract.reference) if s_buy else buy_contract.reference
        return float(sell_p - buy_p)
    except Exception as e:
        print(f"⚠️ 取得即時價差行情異常: {e}")
        return 0.0


def execute_spread_orders(
    api: sj.Shioaji,
    legs: dict,
    quantity: int = 1,
    dry_run: bool = False,
    mode: str = "combo",
    max_retries: int = 10
) -> List[dict]:
    """
    執行下單:
    【預設】複式單模式 (mode="combo"):
      1. 檢查即時帳戶是否已持有相符的價差部位:
         - 若已持有 Bear Call: 跳過不下單
         - 若已持有 Bull Put:  跳過不下單
      2. 若未持有，則送出易於撮合之 IOC 限價複式單，送單後檢查未平倉部位，若未成交則連續重試直到成交為止！
    """
    execution_results = []
    c_map = legs["contracts"]

    c_center = c_map["c_center"]["contract"]
    c_wing   = c_map["c_wing"]["contract"]
    p_center = c_map["p_center"]["contract"]
    p_wing   = c_map["p_wing"]["contract"]

    # 檢查現有持倉
    has_bear_call = check_spread_held(api, c_center.code, c_wing.code, quantity)
    has_bull_put  = check_spread_held(api, p_center.code, p_wing.code, quantity)

    print("\n------------------- [ 即時帳戶部位檢核 ] -------------------")
    print(f"📉 Bear Call Spread ({c_center.code} + {c_wing.code}): {'✅ 已持有相符部位 (將略過不下單)' if has_bear_call else '❌ 尚未持有 (將送單至成交為止)'}")
    print(f"📈 Bull Put Spread  ({p_center.code} + {p_wing.code}): {'✅ 已持有相符部位 (將略過不下單)' if has_bull_put else '❌ 尚未持有 (將送單至成交為止)'}")
    print("-------------------------------------------------------------")

    if dry_run:
        print("\n=======================================================")
        print(f"  🔍 【DRY-RUN 模擬開倉】模式: {'COMBO 複式單' if mode == 'combo' else '單腿單'} (不送出真實委託)")
        print("=======================================================")
        if mode == "combo":
            call_credit = max(0.1, round(legs["bear_call_credit"], 1))
            put_credit = max(0.1, round(legs["bull_put_credit"], 1))
            if has_bear_call:
                print(f"ℹ️ [Bear Call Spread]: 帳戶已持有相符部位，模擬略過不下單。")
            else:
                print(f"👉 模擬送出 [Bear Call 複式單]: 賣出 {c_center.code} + 買入 {c_wing.code} | 數量: {quantity} 限價: {call_credit} (IOC)")
            if has_bull_put:
                print(f"ℹ️ [Bull Put Spread]: 帳戶已持有相符部位，模擬略過不下單。")
            else:
                print(f"👉 模擬送出 [Bull Put 複式單] : 賣出 {p_center.code} + 買入 {p_wing.code} | 數量: {quantity} 限價: {put_credit} (IOC)")
            execution_results.append({
                "strategy": "Bear Call Spread (複式單)",
                "legs": f"Sell {c_center.code} + Buy {c_wing.code}",
                "quantity": quantity,
                "price": call_credit,
                "status": "ALREADY_HELD" if has_bear_call else "SIMULATED",
                "trade_id": "DRY_COMBO_CALL"
            })
            execution_results.append({
                "strategy": "Bull Put Spread (複式單)",
                "legs": f"Sell {p_center.code} + Buy {p_wing.code}",
                "quantity": quantity,
                "price": put_credit,
                "status": "ALREADY_HELD" if has_bull_put else "SIMULATED",
                "trade_id": "DRY_COMBO_PUT"
            })
        return execution_results

    # ================= 實盤 COMBO 複式單下單流程 =================
    if mode == "combo":
        # 1. 處理 Bear Call Spread
        if has_bear_call:
            print(f"\n✅ [Bear Call Spread] 帳戶已持有相符部位 (Sell {c_center.code} + Buy {c_wing.code})，略過不下單！")
            execution_results.append({
                "strategy": "Bear Call Spread (複式單)",
                "legs": f"Sell {c_center.code} + Buy {c_wing.code}",
                "quantity": quantity,
                "price": round(legs["bear_call_credit"], 1),
                "status": "ALREADY_HELD",
                "trade_id": "EXISTING"
            })
        else:
            print(f"\n🚀 [Bear Call Spread] 開始送單至成交為止 (目標: 賣出 {c_center.code} + 買進 {c_wing.code})...")
            leg_call_sell = make_combo_base(c_center, sj.constant.Action.Sell)
            leg_call_buy  = make_combo_base(c_wing, sj.constant.Action.Buy)
            combo_call = ComboContract(legs=[leg_call_sell, leg_call_buy])

            filled_call = False
            for attempt in range(1, max_retries + 1):
                # 重新抓取即時撮合價差
                live_credit = get_live_combo_credit(api, c_center, c_wing)
                # 若重試多次，每次讓價 0.5~1.0 點以確保順利撮合成交
                concession = min(5.0, (attempt - 1) * 0.5)
                order_price = max(0.1, round(live_credit - concession, 1))

                order_call = api.ComboOrder(
                    price=order_price,
                    quantity=quantity,
                    action=sj.constant.Action.Sell,
                    price_type=sj.constant.FuturesPriceType.LMT,
                    order_type=sj.constant.OrderType.IOC,
                    octype=sj.constant.FuturesOCType.New
                )

                print(f"👉 [Bear Call 嘗試 {attempt}/{max_retries}] 送出複式委託: 賣出 {c_center.code} + 買進 {c_wing.code} | 限價: {order_price} (IOC)...")
                try:
                    trade_call = api.place_comboorder(combo_call, order_call)
                except Exception as e:
                    print(f"⚠️ 送單異常: {e}")

                time.sleep(1.2)

                # 檢查帳戶持倉是否已成交
                if check_spread_held(api, c_center.code, c_wing.code, quantity):
                    print(f"🎉 [Bear Call Spread] 委託已順利成交！(成交限價: {order_price})")
                    filled_call = True
                    execution_results.append({
                        "strategy": "Bear Call Spread (複式單)",
                        "legs": f"Sell {c_center.code} + Buy {c_wing.code}",
                        "quantity": quantity,
                        "price": order_price,
                        "status": "FILLED",
                        "trade_id": str(getattr(trade_call.status, "id", ""))
                    })
                    break
                else:
                    print(f"⏳ [Bear Call] 第 {attempt} 次 IOC 委託未撮合，準備更新行情重試...")
                    time.sleep(0.5)

            if not filled_call:
                print(f"❌ [Bear Call Spread] 已重試 {max_retries} 次仍未成交，請確認盤面流動性！")
                execution_results.append({
                    "strategy": "Bear Call Spread (複式單)",
                    "legs": f"Sell {c_center.code} + Buy {c_wing.code}",
                    "quantity": quantity,
                    "price": 0.0,
                    "status": "FAILED_TIMEOUT",
                    "trade_id": ""
                })

        time.sleep(0.5)

        # 2. 處理 Bull Put Spread
        if has_bull_put:
            print(f"\n✅ [Bull Put Spread] 帳戶已持有相符部位 (Sell {p_center.code} + Buy {p_wing.code})，略過不下單！")
            execution_results.append({
                "strategy": "Bull Put Spread (複式單)",
                "legs": f"Sell {p_center.code} + Buy {p_wing.code}",
                "quantity": quantity,
                "price": round(legs["bull_put_credit"], 1),
                "status": "ALREADY_HELD",
                "trade_id": "EXISTING"
            })
        else:
            print(f"\n🚀 [Bull Put Spread] 開始送單至成交為止 (目標: 賣出 {p_center.code} + 買進 {p_wing.code})...")
            leg_put_sell = make_combo_base(p_center, sj.constant.Action.Sell)
            leg_put_buy  = make_combo_base(p_wing, sj.constant.Action.Buy)
            combo_put = ComboContract(legs=[leg_put_sell, leg_put_buy])

            filled_put = False
            for attempt in range(1, max_retries + 1):
                # 重新抓取即時撮合價差
                live_credit = get_live_combo_credit(api, p_center, p_wing)
                # 若重試多次，每次讓價 0.5~1.0 點以確保順利撮合成交
                concession = min(5.0, (attempt - 1) * 0.5)
                order_price = max(0.1, round(live_credit - concession, 1))

                order_put = api.ComboOrder(
                    price=order_price,
                    quantity=quantity,
                    action=sj.constant.Action.Sell,
                    price_type=sj.constant.FuturesPriceType.LMT,
                    order_type=sj.constant.OrderType.IOC,
                    octype=sj.constant.FuturesOCType.New
                )

                print(f"👉 [Bull Put 嘗試 {attempt}/{max_retries}] 送出複式委託: 賣出 {p_center.code} + 買進 {p_wing.code} | 限價: {order_price} (IOC)...")
                try:
                    trade_put = api.place_comboorder(combo_put, order_put)
                except Exception as e:
                    print(f"⚠️ 送單異常: {e}")

                time.sleep(1.2)

                # 檢查帳戶持倉是否已成交
                if check_spread_held(api, p_center.code, p_wing.code, quantity):
                    print(f"🎉 [Bull Put Spread] 委託已順利成交！(成交限價: {order_price})")
                    filled_put = True
                    execution_results.append({
                        "strategy": "Bull Put Spread (複式單)",
                        "legs": f"Sell {p_center.code} + Buy {p_wing.code}",
                        "quantity": quantity,
                        "price": order_price,
                        "status": "FILLED",
                        "trade_id": str(getattr(trade_put.status, "id", ""))
                    })
                    break
                else:
                    print(f"⏳ [Bull Put] 第 {attempt} 次 IOC 委託未撮合，準備更新行情重試...")
                    time.sleep(0.5)

            if not filled_put:
                print(f"❌ [Bull Put Spread] 已重試 {max_retries} 次仍未成交，請確認盤面流動性！")
                execution_results.append({
                    "strategy": "Bull Put Spread (複式單)",
                    "legs": f"Sell {p_center.code} + Buy {p_wing.code}",
                    "quantity": quantity,
                    "price": 0.0,
                    "status": "FAILED_TIMEOUT",
                    "trade_id": ""
                })

        return execution_results

    # 單腿依序下單 (若使用者指定 --mode single)
    print("\n=======================================================")
    print("  🚀 【實盤單腿開倉中】依序送出 4 口單腿委託...")
    print("  ⚠️ 注意: 單腿模式可能會因賣方被視為裸賣而要求較高保證金！")
    print("=======================================================")

    execution_order = ["c_wing", "p_wing", "c_center", "p_center"]

    for key in execution_order:
        info = c_map[key]
        c = info["contract"]
        action = info["action"]
        price = info["price"]

        order = api.Order(
            price=price,
            quantity=quantity,
            action=action,
            price_type=sj.constant.FuturesPriceType.LMT,
            order_type=sj.constant.OrderType.ROD,
            octype=sj.constant.FuturesOCType.New
        )

        try:
            trade = api.place_order(c, order)
            print(f"✅ [{info['role']}] {info['action_desc']} {c.code} 數量: {quantity} 限價: {price} -> 委託已送出 (狀態: {trade.status.status})")
            execution_results.append({
                "strategy": info["role"],
                "legs": f"{info['action_desc']} {c.code}",
                "quantity": quantity,
                "price": price,
                "status": str(trade.status.status),
                "trade_id": getattr(trade.status, "id", "")
            })
        except Exception as e:
            print(f"❌ [{info['role']}] 下單失敗 ({c.code}): {e}")
            execution_results.append({
                "strategy": info["role"],
                "legs": f"{info['action_desc']} {c.code}",
                "quantity": quantity,
                "price": price,
                "status": f"ERROR: {e}",
                "trade_id": ""
            })
        time.sleep(0.3)

    return execution_results

    # 單腿依序下單 (若使用者指定 --mode single)
    print("\n=======================================================")
    print("  🚀 【實盤單腿開倉中】依序送出 4 口單腿委託...")
    print("  ⚠️ 注意: 單腿模式可能會因賣方被視為裸賣而要求較高保證金！")
    print("=======================================================")

    execution_order = ["c_wing", "p_wing", "c_center", "p_center"]

    for key in execution_order:
        info = c_map[key]
        c = info["contract"]
        action = info["action"]
        price = info["price"]

        order = api.Order(
            price=price,
            quantity=quantity,
            action=action,
            price_type=sj.constant.FuturesPriceType.LMT,
            order_type=sj.constant.OrderType.ROD,
            octype=sj.constant.FuturesOCType.New
        )

        try:
            trade = api.place_order(c, order)
            print(f"✅ [{info['role']}] {info['action_desc']} {c.code} 數量: {quantity} 限價: {price} -> 委託已送出 (狀態: {trade.status.status})")
            execution_results.append({
                "strategy": info["role"],
                "legs": f"{info['action_desc']} {c.code}",
                "quantity": quantity,
                "price": price,
                "status": str(trade.status.status),
                "trade_id": getattr(trade.status, "id", "")
            })
        except Exception as e:
            print(f"❌ [{info['role']}] 下單失敗 ({c.code}): {e}")
            execution_results.append({
                "strategy": info["role"],
                "legs": f"{info['action_desc']} {c.code}",
                "quantity": quantity,
                "price": price,
                "status": f"ERROR: {e}",
                "trade_id": ""
            })
        time.sleep(0.3)

    return execution_results


# ==============================================================================
# 📱 LINE 推播訊息格式化
# ==============================================================================
def build_notification_message(
    legs: dict,
    und_name: str,
    quantity: int,
    results: List[dict],
    dry_run: bool = False
) -> str:
    """組裝 LINE 推播訊息"""
    tag = "【DRY-RUN 模擬開倉】" if dry_run else "【永豐 Shioaji 開倉通知】"
    c_map = legs["contracts"]

    msg = (
        f"{tag} 選擇權雙向價差建倉\n"
        f"-----------------------------------------\n"
        f"🎯 標的: {und_name}\n"
        f"   期貨現價: {legs.get('raw_ref_price', legs['ref_price']):.1f} | 點位修正: {legs.get('price_diff', 0.0):+.1f} 點\n"
        f"   修正後參考價: {legs['ref_price']:.1f}\n"
        f"📅 到期日: {legs['delivery_date']} (DTE: {legs['dte']} 天)\n"
        f"📊 口數: 每腿 {quantity} 口 (共 4 口)\n"
        f"\n"
        f"Center ATM: {legs['center_strike']:.0f}\n"
        f"Upper Wing Call: {legs['call_wing_strike']:.0f} (+{legs['wing_width']:.0f})\n"
        f"Lower Wing Put: {legs['put_wing_strike']:.0f} (-{legs['wing_width']:.0f})\n"
        f"-----------------------------------------\n"
        f"📉 1. 買權空頭價差 (Bear Call Spread):\n"
        f"  - 賣出 (Sell) Call {legs['center_strike']:.0f} ({c_map['c_center']['contract'].code}) @ {c_map['c_center']['price']:.1f} (Δ:{c_map['c_center'].get('delta', 0.0):+.2f})\n"
        f"  - 買入 (Buy)  Call {legs['call_wing_strike']:.0f} ({c_map['c_wing']['contract'].code}) @ {c_map['c_wing']['price']:.1f} (Δ:{c_map['c_wing'].get('delta', 0.0):+.2f})\n"
        f"  預估淨收入: {legs['bear_call_credit']:+.1f} 點 | Net Delta: {legs.get('bear_call_delta', 0.0):+.2f}\n"
        f"\n"
        f"📈 2. 賣權多頭價差 (Bull Put Spread):\n"
        f"  - 賣出 (Sell) Put {legs['center_strike']:.0f} ({c_map['p_center']['contract'].code}) @ {c_map['p_center']['price']:.1f} (Δ:{c_map['p_center'].get('delta', 0.0):+.2f})\n"
        f"  - 買入 (Buy)  Put {legs['put_wing_strike']:.0f} ({c_map['p_wing']['contract'].code}) @ {c_map['p_wing']['price']:.1f} (Δ:{c_map['p_wing'].get('delta', 0.0):+.2f})\n"
        f"  預估淨收入: {legs['bull_put_credit']:+.1f} 點 | Net Delta: {legs.get('bull_put_delta', 0.0):+.2f}\n"
        f"-----------------------------------------\n"
        f"💰 總淨權利金: {legs['total_credit']:+.1f} 點 (約 NT$ {legs['total_credit_twd'] * quantity:,.0f})\n"
        f"📐 總 Net Delta: {legs.get('total_net_delta', 0.0):+.2f}\n"
        f"🛡️ 單邊最大風險: {legs['max_risk_points']:.1f} 點 (約 NT$ {legs['max_risk_twd'] * quantity:,.0f})\n"
        f"⚡ 委託狀態: {'全部模擬完成' if dry_run else '實盤委託已送出'}"
    )
    return msg


# ==============================================================================
# 🏁 主流程
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="永豐 Shioaji 選擇權雙向價差開倉程式")
    parser.add_argument("--wing-width", type=float, default=None, help="翅膀寬度 (若未指定則自 trade/.env 的 OP_HEDGE_CONFIG_JSON 即時讀取)")
    parser.add_argument("--price-diff", type=float, default=None, help="點位修正 (若未指定則自 trade/.env 的 OP_HEDGE_CONFIG_JSON 即時讀取)")
    parser.add_argument("--strike-step", type=float, default=100.0, help="中心 ATM 履約價檔位取整 (預設: 100 點)")
    parser.add_argument("--center", type=float, default=None, help="手動指定中心 ATM 履約價 (例如: 45600)")
    parser.add_argument("--price", type=float, default=None, help="手動指定標的期貨參考價")
    parser.add_argument("--delivery-date", type=str, default=None, help="指定到期日 (YYYY/MM/DD 或 YYYYMMDD，預設為最近到期日)")
    parser.add_argument("-q", "--quantity", type=int, default=1, help="每腿口數 (預設: 1 口)")
    parser.add_argument("--dry-run", action="store_true", help="模擬試算模式 (不實際送出委託)")
    parser.add_argument("--no-line", action="store_true", help="不發送 LINE 推播訊息")
    parser.add_argument("--max-retries", type=int, default=10, help="複式單 IOC 未成交時最大重試次數 (預設: 10)")
    parser.add_argument("--mode", type=str, default="combo", choices=["combo", "single"], help="下單模式: combo (預設，期交所標準複式單，僅需價差保證金且兩腿必同時成交) 或 single (單腿單)")

    args = parser.parse_args()

    print("=================================================================")
    print("  🚀 啟動 永豐 Shioaji 選擇權雙向價差開倉程式 (Bear Call + Bull Put)")
    print("=================================================================")

    # 即時讀取 price_diff (DTE 25 TMF 期貨 與 DTE 3 週選擇權之基差修正)
    if args.price_diff is not None:
        price_diff = float(args.price_diff)
        diff_source = "指令參數 --price-diff"
    else:
        price_diff = float(env_float("price_diff", 0.0))
        diff_source = "trade/.env 的 OP_HEDGE_CONFIG_JSON 即時讀取"
    print(f"📊 點位修正 (price_diff): {price_diff:+.1f} 點 (來源: {diff_source})")

    # 即時讀取 wing_width (不用寫入 .env)
    if args.wing_width is not None and args.wing_width > 0:
        wing_width = float(args.wing_width)
        wing_source = "指令參數 --wing-width"
    else:
        wing_width = float(env_int("wing_width", 400))
        wing_source = "trade/.env 的 OP_HEDGE_CONFIG_JSON 即時讀取"
    print(f"📐 翅膀寬度 (wing_width): {wing_width:.0f} 點 (來源: {wing_source})")

    config = load_config()
    api, txo_by_date, txo_by_code = init_shioaji(config)

    # 取得標的市價
    und_name, ref_price = get_underlying_price(api, manual_price=args.price)
    adj_ref_price = ref_price + price_diff
    print(f"📊 標的參考: {und_name} | 期貨現價: {ref_price:.2f} | 點位修正: {price_diff:+.1f} | 修正後參考價: {adj_ref_price:.2f}")

    # 計算價差架構 (以修正後參考價 adj_ref_price 計算價平 center_strike 與 Black-76 Greeks)
    legs = calculate_spread_legs(
        api=api,
        txo_by_date=txo_by_date,
        ref_price=adj_ref_price,
        raw_ref_price=ref_price,
        price_diff=price_diff,
        wing_width=wing_width,
        strike_step=args.strike_step,
        manual_center=args.center,
        delivery_date=args.delivery_date
    )

    c_c = legs['contracts']['c_center']
    c_w = legs['contracts']['c_wing']
    p_c = legs['contracts']['p_center']
    p_w = legs['contracts']['p_wing']

    print("\n------------------- [ 價差架構詳情 ] -------------------")
    print(f"標的期貨現價: {ref_price:.2f} | 點位修正: {price_diff:+.1f} | 修正後參考價: {adj_ref_price:.2f}")
    print(f"Center ATM: {legs['center_strike']:.0f}")
    print(f"Upper Wing Call: {legs['call_wing_strike']:.0f} (+{legs['wing_width']:.0f})")
    print(f"Lower Wing Put: {legs['put_wing_strike']:.0f} (-{legs['wing_width']:.0f})")
    print(f"到期日: {legs['delivery_date']} (DTE: {legs['dte']} 天)")
    print(f"• Bear Call: Sell {c_c['strike']:.0f}C @ {c_c['price']:.1f} (IV: {c_c['iv']*100:.1f}%, Δ: {c_c['delta']:+.2f}) + Buy {c_w['strike']:.0f}C @ {c_w['price']:.1f} (IV: {c_w['iv']*100:.1f}%, Δ: {c_w['delta']:+.2f})")
    print(f"  -> 預估買權淨收入: {legs['bear_call_credit']:+.1f} 點 | Net Delta: {legs['bear_call_delta']:+.2f}")
    print(f"• Bull Put:  Sell {p_c['strike']:.0f}P @ {p_c['price']:.1f} (IV: {p_c['iv']*100:.1f}%, Δ: {p_c['delta']:+.2f}) + Buy {p_w['strike']:.0f}P @ {p_w['price']:.1f} (IV: {p_w['iv']*100:.1f}%, Δ: {p_w['delta']:+.2f})")
    print(f"  -> 預估賣權淨收入: {legs['bull_put_credit']:+.1f} 點 | Net Delta: {legs['bull_put_delta']:+.2f}")
    print(f"總淨權利金收入: {legs['total_credit']:+.1f} 點 (約 NT$ {legs['total_credit_twd'] * args.quantity:,.0f})")
    print(f"組合總 Net Delta: {legs['total_net_delta']:+.2f}")
    print(f"單邊最大潛在風險: {legs['max_risk_points']:.1f} 點 (約 NT$ {legs['max_risk_twd'] * args.quantity:,.0f})")
    print("---------------------------------------------------------")

    # 執行下單
    results = execute_spread_orders(
        api=api,
        legs=legs,
        quantity=args.quantity,
        dry_run=args.dry_run,
        mode=args.mode,
        max_retries=args.max_retries
    )

    # LINE 推播
    if not args.no_line:
        line_msg = build_notification_message(
            legs=legs,
            und_name=und_name,
            quantity=args.quantity,
            results=results,
            dry_run=args.dry_run
        )
        print("\n[INFO] 正在發送 LINE 開倉推播...")
        ok = send_push_message(line_msg)
        if ok:
            print("✅ LINE 推播發送成功！")
        else:
            print("⚠️ LINE 推播發送失敗，請確認 trade/.env 設定。")

    print("\n🎉 [完成] open_Shioaji.py 執行完畢！")


if __name__ == "__main__":
    main()
    # 僅在手動雙擊且非 dry-run 模式下保持終端機視窗，避免自動化背景呼叫被掛起
    if sys.stdin and hasattr(sys.stdin, 'isatty') and sys.stdin.isatty() and not ("--dry-run" in sys.argv):
        try:
            time.sleep(60 * 60 * 20)
        except KeyboardInterrupt:
            pass
