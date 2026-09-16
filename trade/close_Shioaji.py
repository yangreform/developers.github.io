#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
close_Shioaji.py
==============================================================================
永豐 Shioaji 期權全部位市價全自動平倉程式

功能:
  - 自動清空 Shioaji 期貨與選擇權所有未平倉部位 (包含微台 TMF, 小台 MXF, 大台 TXF, 台指選擇權 TXO 等)。
  - 【嚴格安全限制】絕對不碰、不連線、不操作 IBKR (Interactive Brokers)！
  - 全部以【市價單 (MKT) / 一定範圍市價 (MKP)】強制撮合平倉，不保留任何期貨或選擇權部位。
  - 全自動執行，無任何阻塞式互動提問 (Zero Interactive Prompts)。
  - 平倉完成後自動推播彙總報告至手機 LINE。
  - 支援 --dry-run 模擬試算模式。
==============================================================================
"""

import os
import sys
import time
import argparse
from typing import Dict, List, Tuple

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
        sys.stderr.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
    except Exception:
        pass

import shioaji as sj
from dotenv import load_dotenv, find_dotenv

# 引入本機 LINE 通知模組
try:
    from notifier import send_push_message
except ImportError:
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from notifier import send_push_message


# ==============================================================================
# 🔐 載入環境設定與登入
# ==============================================================================
def load_config() -> dict:
    """從 trade/.env 載入設定"""
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


def init_shioaji(config: dict) -> Tuple[sj.Shioaji, Dict[str, object]]:
    """初始化並登入 Shioaji，快取期貨與選擇權合約"""
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

    # 快取期貨與選擇權合約表
    contract_cache: Dict[str, object] = {}

    # 1. 期貨類別 (微台, 小台, 大台, 電子, 金融等)
    for cat in ['TMF', 'MXF', 'TXF', 'TE', 'TF', 'ZE', 'ZF']:
        if hasattr(api.Contracts.Futures, cat):
            for c in getattr(api.Contracts.Futures, cat):
                contract_cache[c.code] = c

    # 2. 選擇權類別 (週選, 月選, 週五選 TXU/TXV/TXX/TXY/TXZ, 黃金選, 電子選等，以及所有 TX* 類別)
    opt_cats = ['TX1', 'TX2', 'TX3', 'TX4', 'TX5', 'TXO', 'TXU', 'TXV', 'TXX', 'TXY', 'TXZ', 'TGO', 'TEO', 'TFO']
    all_opt_cats = sorted(list(set(opt_cats + [attr for attr in dir(api.Contracts.Options) if attr.startswith("TX")])))
    for cat in all_opt_cats:
        if hasattr(api.Contracts.Options, cat):
            for c in getattr(api.Contracts.Options, cat):
                contract_cache[c.code] = c

    return api, contract_cache


# ==============================================================================
# 🔍 取得所有未平倉部位 (期貨與選擇權全部納入，不予過濾保留)
# ==============================================================================
def get_all_open_positions(
    api: sj.Shioaji,
    contract_cache: Dict[str, object],
    target_code: str = ""
) -> List[Tuple[object, object]]:
    """
    取得所有 Shioaji 期貨與選擇權未平倉部位 (不再過濾保留微台或任何期貨部位)。
    【安全保證】只操作 Shioaji 帳戶，絕不連線或碰觸 IBKR！
    """
    if not api.futopt_account:
        print("⚠️ 未找到有效期貨/選擇權帳號")
        return []

    positions = api.list_positions(account=api.futopt_account)
    all_positions = []

    for p in positions:
        # 若有指定特定代號則過濾
        if target_code and p.code != target_code:
            continue

        c = contract_cache.get(p.code)
        if not c:
            # 嘗試動態查詢
            if hasattr(api.Contracts.Futures, "get"):
                c = api.Contracts.Futures.get(p.code)
            if not c and hasattr(api.Contracts.Options, "get"):
                c = api.Contracts.Options.get(p.code)
            if not c and hasattr(api.Contracts, "get"):
                c = api.Contracts.get(p.code)

        all_positions.append((p, c))

    return all_positions


# ==============================================================================
# 🚀 執行全部市價平倉
# ==============================================================================
def close_all_positions_market(
    api: sj.Shioaji,
    positions: List[Tuple[object, object]],
    dry_run: bool = False
) -> List[dict]:
    """
    依序市價平倉所有部位 (期貨與選擇權):
    - 原多方 (Buy) -> 送出 市價賣出平倉 (SELL MKT / MKP)
    - 原空方 (Sell) -> 送出 市價買進平倉 (BUY MKT / MKP)
    - 支援期交所夜盤時段自動轉為「一定範圍市價單 (MKP)」容錯機制
    """
    closed_records = []
    if not positions:
        print("✅ 目前 Shioaji 帳戶內無任何未平倉部位 (期貨與選擇權皆為零)。")
        return closed_records

    # 批次取得所有要平倉合約的即時快照供報告參考
    valid_contracts = [c for _, c in positions if c is not None]
    snapshots = {}
    if valid_contracts:
        try:
            snapshots = {s.code: s for s in api.snapshots(valid_contracts)}
        except Exception as e:
            print(f"⚠️ 取得即時報價快照失敗: {e}")

    print(f"\n=======================================================")
    print(f"  {'🔍 【DRY-RUN 模擬平倉】' if dry_run else '🚀 【實盤市價平倉執行中】'} 共 {len(positions)} 筆部位 (期貨 + 選擇權)")
    print(f"=======================================================")

    for p, c in positions:
        code = p.code
        cur_direction = p.direction
        qty = p.quantity
        entry_price = p.price
        pnl = getattr(p, "pnl", 0.0)

        # 辨識商品類型
        is_opt = (c is not None and getattr(c, "security_type", None) == sj.constant.SecurityType.Option) or code.startswith(('TXO', 'TX1', 'TX2', 'TX4', 'TX5'))
        pos_type_str = "選擇權" if is_opt else "期貨"

        # 決定平倉方向
        if cur_direction == sj.constant.Action.Buy:
            close_action = sj.constant.Action.Sell
            close_action_str = "市價賣出 (SELL MKT)"
        else:
            close_action = sj.constant.Action.Buy
            close_action_str = "市價買入 (BUY MKT)"

        # 預估市價估值 (取即時行情供推播與 Log 顯示)
        snap = snapshots.get(code)
        ref_est_price = 0.0
        if snap:
            if close_action == sj.constant.Action.Sell:
                ref_est_price = snap.buy_price or snap.close or getattr(p, "last_price", 0.0)
            else:
                ref_est_price = snap.sell_price or snap.close or getattr(p, "last_price", 0.0)
        if not ref_est_price:
            ref_est_price = getattr(p, "last_price", 0.0) or (c.reference if c else entry_price)

        record = {
            "code": code,
            "name": getattr(c, "name", code) if c else code,
            "type": pos_type_str,
            "cur_direction": str(cur_direction),
            "close_action": close_action_str,
            "quantity": qty,
            "entry_price": entry_price,
            "est_price": float(ref_est_price),
            "pnl": pnl,
            "status": "SIMULATED" if dry_run else "SUBMITTED",
            "trade_id": ""
        }

        if dry_run:
            print(f"👉 [DRY-RUN] [{pos_type_str}] {close_action_str} {code} 數量: {qty} (進場: {entry_price}, 預估市價: {ref_est_price}, 損益: {pnl:+.0f})")
            closed_records.append(record)
            continue

        # 實盤下單
        if not c:
            print(f"❌ 找不到 {code} 的合約物件，跳過平倉")
            record["status"] = "FAILED_NO_CONTRACT"
            closed_records.append(record)
            continue

        # 建立市價平倉委託單 (優先使用 FuturesPriceType.MKT + IOC)
        order = api.Order(
            price=0,
            quantity=qty,
            action=close_action,
            price_type=sj.constant.FuturesPriceType.MKT,
            order_type=sj.constant.OrderType.IOC,
            octype=sj.constant.FuturesOCType.Cover
        )

        try:
            trade = api.place_order(c, order)
            trade_status = getattr(trade.status, "status", "UNKNOWN")
            trade_id = getattr(trade.status, "id", "")
            print(f"✅ [{pos_type_str}] {close_action_str} {code} 數量: {qty} -> 委託已送出 (狀態: {trade_status}, 委託號: {trade_id})")
            record["status"] = str(trade_status)
            record["trade_id"] = str(trade_id)
        except Exception as e:
            err_msg = str(e)
            print(f"⚠️ [{code}] 市價平倉 (MKT IOC) 發生異常: {err_msg}，嘗試使用「一定範圍市價單 (MKP)」...")
            # 容錯機制 1: 期交所夜盤不收市價單，自動轉一定範圍市價 (MKP)
            try:
                order.price_type = sj.constant.FuturesPriceType.MKP
                order.order_type = sj.constant.OrderType.IOC
                order.octype = sj.constant.FuturesOCType.Cover
                trade = api.place_order(c, order)
                trade_status = getattr(trade.status, "status", "UNKNOWN")
                trade_id = getattr(trade.status, "id", "")
                print(f"✅ [MKP 重試成功] {close_action_str} {code} -> 狀態: {trade_status}")
                record["status"] = str(trade_status)
                record["trade_id"] = str(trade_id)
            except Exception as e2:
                # 容錯機制 2: 若 Cover 拒單則嘗試 Auto
                print(f"⚠️ [{code}] MKP Cover 異常: {e2}，嘗試 Auto octype...")
                try:
                    order.octype = sj.constant.FuturesOCType.Auto
                    trade = api.place_order(c, order)
                    trade_status = getattr(trade.status, "status", "UNKNOWN")
                    trade_id = getattr(trade.status, "id", "")
                    print(f"✅ [MKP Auto 成功] {close_action_str} {code} -> 狀態: {trade_status}")
                    record["status"] = str(trade_status)
                    record["trade_id"] = str(trade_id)
                except Exception as e3:
                    # 容錯機制 3: 若市價皆受阻，以強勢對手價限價搶撮合
                    print(f"⚠️ [{code}] 市價委託受阻: {e3}，改以強勢限價 ({ref_est_price}) 平倉...")
                    try:
                        order.price = float(ref_est_price)
                        order.price_type = sj.constant.FuturesPriceType.LMT
                        order.order_type = sj.constant.OrderType.ROD
                        order.octype = sj.constant.FuturesOCType.Auto
                        trade = api.place_order(c, order)
                        trade_status = getattr(trade.status, "status", "UNKNOWN")
                        trade_id = getattr(trade.status, "id", "")
                        print(f"✅ [強勢限價成功] {close_action_str} {code} 限價 {ref_est_price} -> 狀態: {trade_status}")
                        record["status"] = str(trade_status)
                        record["trade_id"] = str(trade_id)
                    except Exception as e4:
                        print(f"❌ 平倉失敗 ({code}): {e4}")
                        record["status"] = f"ERROR: {e4}"

        closed_records.append(record)
        time.sleep(0.3)

    return closed_records


# ==============================================================================
# 📱 LINE 推播訊息格式化
# ==============================================================================
def build_close_notification(records: List[dict], dry_run: bool = False) -> str:
    """格式化全平倉報告"""
    tag = "【DRY-RUN 模擬平倉】" if dry_run else "【永豐 Shioaji 市價平倉通知】"
    if not records:
        return f"{tag} 全部位平倉作業\n-----------------------------------------\n✅ 目前帳戶內無任何未平倉部位 (期貨與選擇權皆為零)。"

    total_pnl = sum(r.get("pnl", 0.0) for r in records)
    fut_count = sum(1 for r in records if r.get("type") == "期貨")
    opt_count = sum(1 for r in records if r.get("type") == "選擇權")

    lines = [
        f"{tag} 期權全部位市價平倉",
        f"-----------------------------------------",
        f"📦 平倉部位: 共 {len(records)} 筆 (期貨: {fut_count} 筆, 選擇權: {opt_count} 筆)",
        f"⚡ 平倉模式: 全部市價強制平倉 (MKT / MKP)",
        f"🛡️ 安全隔離: 僅限 Shioaji (絕不碰 IBKR 部位)",
        f"-----------------------------------------"
    ]

    for idx, r in enumerate(records, 1):
        lines.append(
            f"{idx}. [{r['type']}] {r['code']} ({r['close_action']})\n"
            f"   口數: {r['quantity']} | 預估成交價: {r['est_price']:.1f}\n"
            f"   進場成本: {r['entry_price']:.1f} | 預估損益: {r['pnl']:+.0f}\n"
            f"   狀態: {r['status']}"
        )

    lines.append(f"-----------------------------------------")
    lines.append(f"💰 總預估損益: {total_pnl:+.0f} 點")
    lines.append(f"⚡ 執行結果: {'全部模擬完成' if dry_run else '實盤市價平倉委託已全數發送'}")

    return "\n".join(lines)


# ==============================================================================
# 🏁 主流程 (完全全自動，不阻塞詢問)
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="永豐 Shioaji 期權全部位市價全自動平倉程式 (絕不動 IBKR)")
    parser.add_argument("--dry-run", action="store_true", help="模擬試算模式 (不實際送出委託)")
    parser.add_argument("--no-line", action="store_true", help="不發送 LINE 推播訊息")
    parser.add_argument("--code", type=str, default="", help="指定平倉單一合約代號 (預設為全部市價平倉)")

    args = parser.parse_args()

    print("=================================================================")
    print("  🧹 啟動 永豐 Shioaji 期權全部位市價全自動平倉程式")
    print("  🔒 【安全宣告】本程式只操作 Shioaji 期貨與選擇權，絕不連線或更動 IBKR！")
    print("=================================================================")

    config = load_config()
    api, contract_cache = init_shioaji(config)

    # 1. 取得所有未平倉部位 (包含微台、期貨、選擇權)
    positions = get_all_open_positions(api, contract_cache, target_code=args.code.strip())
    print(f"📊 偵測到 {len(positions)} 筆 Shioaji 未平倉部位 (包含期貨與選擇權)。")

    # 2. 執行全部市價平倉
    closed_records = close_all_positions_market(
        api=api,
        positions=positions,
        dry_run=args.dry_run
    )

    # 3. LINE 推播
    if not args.no_line:
        line_msg = build_close_notification(closed_records, dry_run=args.dry_run)
        print("\n[INFO] 正在發送 LINE 平倉推播...")
        ok = send_push_message(line_msg)
        if ok:
            print("✅ LINE 平倉推播發送成功！")
        else:
            print("⚠️ LINE 推播發送失敗，請確認 trade/.env 設定。")

    print("\n🎉 [完成] close_Shioaji.py 執行完畢！")


if __name__ == "__main__":
    main()
    time.sleep(60*60*20)
