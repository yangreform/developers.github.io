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
import argparse
import datetime
from typing import Dict, List, Optional, Tuple

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
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

    # 快取 TXO 選擇權合約
    txo_by_date: Dict[str, List[Option]] = {}
    txo_by_code: Dict[str, Option] = {}
    for cat in ['TX1', 'TX2', 'TX4', 'TX5', 'TXO']:
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
    """
    if manual_price and manual_price > 0:
        return "手動指定", float(manual_price)

    today_str = datetime.date.today().strftime("%Y/%m/%d")
    fut_candidates = []

    # 搜尋近月期貨
    for cat in ['TMF', 'MXF', 'TXF']:
        if hasattr(api.Contracts.Futures, cat):
            for c in getattr(api.Contracts.Futures, cat):
                if c.delivery_date >= today_str:
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
    """選擇目標選擇權到期日與計算 DTE"""
    today = datetime.date.today()
    today_str = today.strftime("%Y/%m/%d")

    available_dates = sorted([d for d in txo_by_date.keys() if d >= today_str])
    if not available_dates:
        raise RuntimeError("❌ 沒有找到未到期的選擇權合約到期日")

    if target_date:
        cleaned = target_date.replace("-", "/").replace(".", "/")
        if len(cleaned) == 8 and cleaned.isdigit():
            cleaned = f"{cleaned[:4]}/{cleaned[4:6]}/{cleaned[6:]}"
        if cleaned in txo_by_date:
            selected = cleaned
        else:
            raise ValueError(f"❌ 指定的到期日 {target_date} 不在可用列表: {available_dates}")
    else:
        selected = available_dates[0]

    try:
        exp_dt = datetime.datetime.strptime(selected, "%Y/%m/%d").date()
        dte = (exp_dt - today).days
    except Exception:
        dte = 0

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
    wing_width: float = 200.0,
    strike_step: float = 100.0,
    manual_center: Optional[float] = None,
    delivery_date: Optional[str] = None
) -> dict:
    """
    計算中心 ATM、上翼 Call、下翼 Put，並取得 4 口合約及即時報價
    """
    # 1. 決定中心 ATM 與翅膀
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

    legs_info = {
        "center_strike": center_strike,
        "call_wing_strike": call_wing_strike,
        "put_wing_strike": put_wing_strike,
        "wing_width": wing_width,
        "delivery_date": selected_date,
        "dte": dte,
        "ref_price": ref_price,
        "contracts": {
            "c_wing": {
                "contract": c_wing,
                "action": sj.constant.Action.Buy,
                "action_desc": "買入 (BUY)",
                "strike": call_wing_strike,
                "right": "Call",
                "role": "Upper Wing Call",
                "price": c_wing_price,
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
                "snap": snapshots.get(p_wing.code)
            },
        },
        "bear_call_credit": bear_call_credit,
        "bull_put_credit": bull_put_credit,
        "total_credit": total_credit,
        "total_credit_twd": total_credit * 50.0,
        "max_risk_points": max_risk_points,
        "max_risk_twd": max_risk_points * 50.0,
    }
    return legs_info


# ==============================================================================
# 🚀 執行下單
# ==============================================================================
def execute_spread_orders(
    api: sj.Shioaji,
    legs: dict,
    quantity: int = 1,
    dry_run: bool = False,
    mode: str = "single"
) -> List[dict]:
    """
    依序下單:
    單腿模式 (single):
      1. 買入 Upper Wing Call (先買翅膀鎖定風險與保證金)
      2. 買入 Lower Wing Put  (先買翅膀鎖定風險與保證金)
      3. 賣出 Center Call
      4. 賣出 Center Put
    """
    execution_results = []
    c_map = legs["contracts"]

    if dry_run:
        print("\n=======================================================")
        print("  🔍 【DRY-RUN 模擬開倉】不送出真實委託至期交所")
        print("=======================================================")
        for key in ["c_wing", "p_wing", "c_center", "p_center"]:
            info = c_map[key]
            c = info["contract"]
            print(f"👉 模擬委託: {info['action_desc']:<10} {c.code:<12} 履約價: {info['strike']:<7} {info['right']:<4} 數量: {quantity} 限價: {info['price']}")
            execution_results.append({
                "role": info["role"],
                "code": c.code,
                "strike": info["strike"],
                "right": info["right"],
                "action": info["action_desc"],
                "quantity": quantity,
                "price": info["price"],
                "status": "SIMULATED",
                "trade_id": "DRY_RUN_000"
            })
        return execution_results

    # 實盤下單
    print("\n=======================================================")
    print("  🚀 【實盤開倉中】開始送出 4 口委託至永豐證券...")
    print("=======================================================")

    # 嚴格順序: 買方先執行，再執行賣方
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
            octype=sj.constant.FuturesOCType.New  # 開新倉
        )

        try:
            trade = api.place_order(c, order)
            print(f"✅ [{info['role']}] {info['action_desc']} {c.code} 數量: {quantity} 限價: {price} -> 委託已送出 (狀態: {trade.status.status})")
            execution_results.append({
                "role": info["role"],
                "code": c.code,
                "strike": info["strike"],
                "right": info["right"],
                "action": info["action_desc"],
                "quantity": quantity,
                "price": price,
                "status": str(trade.status.status),
                "trade_id": getattr(trade.status, "id", "")
            })
        except Exception as e:
            print(f"❌ [{info['role']}] 下單失敗 ({c.code}): {e}")
            execution_results.append({
                "role": info["role"],
                "code": c.code,
                "strike": info["strike"],
                "right": info["right"],
                "action": info["action_desc"],
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
        f"🎯 標的: {und_name} (參考價: {legs['ref_price']:.1f})\n"
        f"📅 到期日: {legs['delivery_date']} (DTE: {legs['dte']} 天)\n"
        f"📊 口數: 每腿 {quantity} 口 (共 4 口)\n"
        f"\n"
        f"Center ATM: {legs['center_strike']:.0f}\n"
        f"Upper Wing Call: {legs['call_wing_strike']:.0f} (+{legs['wing_width']:.0f})\n"
        f"Lower Wing Put: {legs['put_wing_strike']:.0f} (-{legs['wing_width']:.0f})\n"
        f"-----------------------------------------\n"
        f"📉 1. 買權空頭價差 (Bear Call Spread):\n"
        f"  - 賣出 (Sell) Call {legs['center_strike']:.0f} ({c_map['c_center']['contract'].code}) @ {c_map['c_center']['price']:.1f}\n"
        f"  - 買入 (Buy)  Call {legs['call_wing_strike']:.0f} ({c_map['c_wing']['contract'].code}) @ {c_map['c_wing']['price']:.1f}\n"
        f"  預估淨收入: {legs['bear_call_credit']:+.1f} 點\n"
        f"\n"
        f"📈 2. 賣權多頭價差 (Bull Put Spread):\n"
        f"  - 賣出 (Sell) Put {legs['center_strike']:.0f} ({c_map['p_center']['contract'].code}) @ {c_map['p_center']['price']:.1f}\n"
        f"  - 買入 (Buy)  Put {legs['put_wing_strike']:.0f} ({c_map['p_wing']['contract'].code}) @ {c_map['p_wing']['price']:.1f}\n"
        f"  預估淨收入: {legs['bull_put_credit']:+.1f} 點\n"
        f"-----------------------------------------\n"
        f"💰 總淨權利金: {legs['total_credit']:+.1f} 點 (約 NT$ {legs['total_credit_twd'] * quantity:,.0f})\n"
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
    parser.add_argument("--strike-step", type=float, default=100.0, help="中心 ATM 履約價檔位取整 (預設: 100 點)")
    parser.add_argument("--center", type=float, default=None, help="手動指定中心 ATM 履約價 (例如: 45600)")
    parser.add_argument("--price", type=float, default=None, help="手動指定標的期貨參考價")
    parser.add_argument("--delivery-date", type=str, default=None, help="指定到期日 (YYYY/MM/DD 或 YYYYMMDD，預設為最近到期日)")
    parser.add_argument("-q", "--quantity", type=int, default=1, help="每腿口數 (預設: 1 口)")
    parser.add_argument("--dry-run", action="store_true", help="模擬試算模式 (不實際送出委託)")
    parser.add_argument("--no-line", action="store_true", help="不發送 LINE 推播訊息")
    parser.add_argument("--mode", type=str, default="single", choices=["single", "combo"], help="下單模式: single (依序單腿) 或 combo (複式單)")

    args = parser.parse_args()

    print("=================================================================")
    print("  🚀 啟動 永豐 Shioaji 選擇權雙向價差開倉程式 (Bear Call + Bull Put)")
    print("=================================================================")

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
    print(f"📊 標的參考: {und_name} | 現價: {ref_price:.2f}")

    # 計算價差架構
    legs = calculate_spread_legs(
        api=api,
        txo_by_date=txo_by_date,
        ref_price=ref_price,
        wing_width=wing_width,
        strike_step=args.strike_step,
        manual_center=args.center,
        delivery_date=args.delivery_date
    )

    print("\n------------------- [ 價差架構詳情 ] -------------------")
    print(f"Center ATM: {legs['center_strike']:.0f}")
    print(f"Upper Wing Call: {legs['call_wing_strike']:.0f} (+{legs['wing_width']:.0f})")
    print(f"Lower Wing Put: {legs['put_wing_strike']:.0f} (-{legs['wing_width']:.0f})")
    print(f"到期日: {legs['delivery_date']} (DTE: {legs['dte']} 天)")
    print(f"預估買權價差淨收入: {legs['bear_call_credit']:+.1f} 點")
    print(f"預估賣權價差淨收入: {legs['bull_put_credit']:+.1f} 點")
    print(f"總淨權利金收入: {legs['total_credit']:+.1f} 點 (約 NT$ {legs['total_credit_twd'] * args.quantity:,.0f})")
    print(f"單邊最大潛在風險: {legs['max_risk_points']:.1f} 點 (約 NT$ {legs['max_risk_twd'] * args.quantity:,.0f})")
    print("---------------------------------------------------------")

    # 執行下單
    results = execute_spread_orders(
        api=api,
        legs=legs,
        quantity=args.quantity,
        dry_run=args.dry_run,
        mode=args.mode
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
