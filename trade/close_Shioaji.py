#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
close_Shioaji.py
==============================================================================
永豐 Shioaji 選擇權 (OP) 全自動平倉程式

功能:
  - 專門平倉 Shioaji 選擇權 (OP) 未平倉部位 (例如: TXO, TX1, TX2, TX4, TX5 等)。
  - 【嚴格安全限制】絕對不碰、不連線、不操作 IBKR (Interactive Brokers)！
  - 僅平倉選擇權 (OP)，預設不平倉期貨部位 (例如: 微台 TMF, 小台 MXF, 大台 TXF)。
  - 全自動執行，無任何阻塞式互動提問 (Zero Interactive Prompts)。
  - 平倉時使用最佳對手價 (買方部位以 Bid 賣出、賣方部位以 Ask 買進) 確保即時撮合成交。
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
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import shioaji as sj
from shioaji.contracts import Option
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


def init_shioaji(config: dict) -> Tuple[sj.Shioaji, Dict[str, Option]]:
    """初始化並登入 Shioaji，快取選擇權合約"""
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

    # 快取選擇權合約表
    opt_cache: Dict[str, Option] = {}
    for cat in ['TX1', 'TX2', 'TX4', 'TX5', 'TXO', 'TGO', 'TEO', 'TFO']:
        if hasattr(api.Contracts.Options, cat):
            for c in getattr(api.Contracts.Options, cat):
                opt_cache[c.code] = c

    return api, opt_cache


# ==============================================================================
# 🔍 篩選選擇權 (OP) 未平倉部位
# ==============================================================================
def get_open_op_positions(
    api: sj.Shioaji,
    opt_cache: Dict[str, Option],
    target_code: str = ""
) -> List[Tuple[object, Option]]:
    """
    僅篩選 Shioaji 選擇權 (OP) 未平倉部位。
    排除期貨 (如 TMF, MXF, TXF)。
    """
    if not api.futopt_account:
        print("⚠️ 未找到有效期貨/選擇權帳號")
        return []

    positions = api.list_positions(account=api.futopt_account)
    op_positions = []

    for p in positions:
        # 如果指定了特定代號則過濾
        if target_code and p.code != target_code:
            continue

        c = opt_cache.get(p.code)
        # 判斷是否為選擇權合約
        is_op = (c is not None) or p.code.startswith(('TXO', 'TX1', 'TX2', 'TX4', 'TX5', 'TGO', 'TEO', 'TFO'))

        if is_op:
            if not c and hasattr(api.Contracts, "get"):
                c = api.Contracts.get(p.code)
            op_positions.append((p, c))
        else:
            print(f"ℹ️ [保留非選擇權部位] {p.code} ({p.direction} {p.quantity}口 @ {p.price}) - 不執行平倉")

    return op_positions


# ==============================================================================
# 🚀 執行平倉
# ==============================================================================
def close_op_positions(
    api: sj.Shioaji,
    op_positions: List[Tuple[object, Option]],
    dry_run: bool = False
) -> List[dict]:
    """
    依序平倉所有選擇權部位:
    - 原持有多方 (Buy) -> 送出 賣出 (Sell) 平倉
    - 原持有空方 (Sell) -> 送出 買進 (Buy) 平倉
    - 價格使用撮合對手價 (Sell 用 Bid, Buy 用 Ask)
    """
    closed_records = []
    if not op_positions:
        print("✅ 目前無任何 Shioaji 選擇權 (OP) 未平倉部位需要平倉。")
        return closed_records

    # 批次取得所有要平倉合約的即時快照
    valid_contracts = [c for _, c in op_positions if c is not None]
    snapshots = {}
    if valid_contracts:
        try:
            snapshots = {s.code: s for s in api.snapshots(valid_contracts)}
        except Exception as e:
            print(f"⚠️ 取得即時報價快照失敗: {e}")

    print(f"\n=======================================================")
    print(f"  {'🔍 【DRY-RUN 模擬平倉】' if dry_run else '🚀 【實盤平倉執行中】'} 共 {len(op_positions)} 筆選擇權部位")
    print(f"=======================================================")

    for p, c in op_positions:
        code = p.code
        cur_direction = p.direction
        qty = p.quantity
        entry_price = p.price
        pnl = getattr(p, "pnl", 0.0)

        # 決定平倉方向
        if cur_direction == sj.constant.Action.Buy:
            close_action = sj.constant.Action.Sell
            close_action_str = "賣出平倉 (SELL)"
        else:
            close_action = sj.constant.Action.Buy
            close_action_str = "買入平倉 (BUY)"

        # 決定平倉限價
        snap = snapshots.get(code)
        if close_action == sj.constant.Action.Sell:
            # 賣出時使用買進價 (Bid) 搶即時成交
            limit_price = snap.buy_price if (snap and snap.buy_price and snap.buy_price > 0) else (
                snap.close if (snap and snap.close and snap.close > 0) else (
                    getattr(p, "last_price", 0.0) or (c.reference if c else entry_price)
                )
            )
        else:
            # 買進時使用賣出價 (Ask) 搶即時成交
            limit_price = snap.sell_price if (snap and snap.sell_price and snap.sell_price > 0) else (
                snap.close if (snap and snap.close and snap.close > 0) else (
                    getattr(p, "last_price", 0.0) or (c.reference if c else entry_price)
                )
            )

        limit_price = float(limit_price)

        record = {
            "code": code,
            "name": getattr(c, "name", code) if c else code,
            "cur_direction": str(cur_direction),
            "close_action": close_action_str,
            "quantity": qty,
            "entry_price": entry_price,
            "limit_price": limit_price,
            "pnl": pnl,
            "status": "SIMULATED" if dry_run else "SUBMITTED",
            "trade_id": ""
        }

        if dry_run:
            print(f"👉 [DRY-RUN] {close_action_str} {code} 數量: {qty} 限價: {limit_price} (成本: {entry_price}, 現價: {getattr(p, 'last_price', 'N/A')}, 預估損益: {pnl:+.0f})")
            closed_records.append(record)
            continue

        # 實盤下單
        if not c:
            print(f"❌ 找不到 {code} 的合約物件，跳過平倉")
            record["status"] = "FAILED_NO_CONTRACT"
            closed_records.append(record)
            continue

        # 建立平倉委託單 (優先使用 FuturesOCType.Cover 平倉)
        order = api.Order(
            price=limit_price,
            quantity=qty,
            action=close_action,
            price_type=sj.constant.FuturesPriceType.LMT,
            order_type=sj.constant.OrderType.ROD,
            octype=sj.constant.FuturesOCType.Cover
        )

        try:
            trade = api.place_order(c, order)
            trade_status = getattr(trade.status, "status", "UNKNOWN")
            trade_id = getattr(trade.status, "id", "")
            print(f"✅ {close_action_str} {code} 數量: {qty} 限價: {limit_price} -> 委託已送出 (狀態: {trade_status}, 委託號: {trade_id})")
            record["status"] = str(trade_status)
            record["trade_id"] = str(trade_id)
        except Exception as e:
            # 若因 Cover 失敗則嘗試 Auto
            err_msg = str(e)
            print(f"⚠️ 平倉委託 (Cover) 發生異常: {err_msg}，嘗試使用 Auto 委託...")
            try:
                order.octype = sj.constant.FuturesOCType.Auto
                trade = api.place_order(c, order)
                trade_status = getattr(trade.status, "status", "UNKNOWN")
                trade_id = getattr(trade.status, "id", "")
                print(f"✅ [Auto 重試成功] {close_action_str} {code} -> 狀態: {trade_status}")
                record["status"] = str(trade_status)
                record["trade_id"] = str(trade_id)
            except Exception as e2:
                print(f"❌ 平倉失敗 ({code}): {e2}")
                record["status"] = f"ERROR: {e2}"

        closed_records.append(record)
        time.sleep(0.3)

    return closed_records


# ==============================================================================
# 📱 LINE 推播訊息格式化
# ==============================================================================
def build_close_notification(records: List[dict], dry_run: bool = False) -> str:
    """格式化平倉報告"""
    tag = "【DRY-RUN 模擬平倉】" if dry_run else "【永豐 Shioaji 平倉通知】"
    if not records:
        return f"{tag} 選擇權 (OP) 平倉作業\n-----------------------------------------\n✅ 目前帳戶內無任何選擇權未平倉部位。"

    total_pnl = sum(r.get("pnl", 0.0) for r in records)
    lines = [
        f"{tag} 選擇權 (OP) 全數平倉",
        f"-----------------------------------------",
        f"📦 平倉部位總數: {len(records)} 筆",
        f"🛡️ 操作範圍: 僅限 Shioaji 選擇權 (未更動任何 IBKR 與期貨部位)",
        f"-----------------------------------------"
    ]

    for idx, r in enumerate(records, 1):
        lines.append(
            f"{idx}. {r['code']} ({r['close_action']})\n"
            f"   口數: {r['quantity']} | 平倉限價: {r['limit_price']:.1f}\n"
            f"   進場成本: {r['entry_price']:.1f} | 預估損益: {r['pnl']:+.0f} 點\n"
            f"   狀態: {r['status']}"
        )

    lines.append(f"-----------------------------------------")
    lines.append(f"💰 總預估損益: {total_pnl:+.0f} 點 (約 NT$ {total_pnl * 50:,.0f})")
    lines.append(f"⚡ 執行結果: {'全部模擬完成' if dry_run else '實盤平倉委託已全數發送'}")

    return "\n".join(lines)


# ==============================================================================
# 🏁 主流程 (完全全自動，不阻塞詢問)
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="永豐 Shioaji 選擇權 (OP) 全自動平倉程式 (絕不動 IBKR)")
    parser.add_argument("--dry-run", action="store_true", help="模擬試算模式 (不實際送出委託)")
    parser.add_argument("--no-line", action="store_true", help="不發送 LINE 推播訊息")
    parser.add_argument("--code", type=str, default="", help="指定平倉單一選擇權合約代號 (預設為全數平倉)")

    args = parser.parse_args()

    print("=================================================================")
    print("  🧹 啟動 永豐 Shioaji 選擇權 (OP) 全自動平倉程式")
    print("  🔒 【安全宣告】本程式只操作 Shioaji 選擇權，絕不連線或更動 IBKR！")
    print("=================================================================")

    config = load_config()
    api, opt_cache = init_shioaji(config)

    # 1. 取得並篩選 OP 部位
    op_positions = get_open_op_positions(api, opt_cache, target_code=args.code.strip())
    print(f"📊 偵測到 {len(op_positions)} 筆 Shioaji 選擇權未平倉部位。")

    # 2. 執行平倉
    closed_records = close_op_positions(
        api=api,
        op_positions=op_positions,
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
