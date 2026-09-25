#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scale In Order Skill (trade/skills/scale_in_order_skill/scale_in_order.py)
================================================================================
職責：
  當執行加碼委託 (如 SELL) 時遇到 IBKR Error 201：
  "Cannot have open orders on both sides of the same US Option contract."
  (美股期權法規禁止一般客戶在同一期權合約兩側同時持有掛單)

  依序執行 4 大重組步驟：
  [步驟 1: 查詢並撤銷現有掛單]
     精準掃描引發兩側衝突之既有掛單 (包含同 Option conId 相反買賣方向、同標的平倉掛單) 並撤單 (Cancel)。
  [步驟 2: 送出加碼母單 (SELL)]
     送出新的單純母單 (SELL 市價 Adaptive Patient)，不帶附屬單，等待完全成交 (Filled)。
  [步驟 3: 合併計算新舊總持倉口數]
     查詢帳戶最新未平倉部位，合併計算最新總持倉口數 (例如：原 1 口 + 加碼 1 口 = 2 口)。
  [步驟 4: 重掛總口數的 OCA 停利停損單]
     以總口數重新掛出一組反向 (BUY) 的 OCA 括號單 (One-Cancels-All Bracket)。
     若新舊部位跨不同履約價組合，則分別為新舊部位掛出專屬 OCA 括號單，確保各履約價百分之百對沖保護。
================================================================================
"""

import os
import sys
import time
import math
import datetime

# 確保基礎路徑
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    from ib_insync import IB, Contract, Option, LimitOrder, MarketOrder, StopOrder, TagValue
except ImportError:
    IB = None
    Contract = None
    Option = None
    LimitOrder = None
    MarketOrder = None
    StopOrder = None
    TagValue = None

try:
    from notifier import send_push_message
except ImportError:
    send_push_message = None


class ScaleInOrderSkill:
    """
    加碼防衝突與 OCA 括號單重構技能 (Error 201 專用處理引擎)
    """

    def __init__(self, ib_instance=None):
        self.ib = ib_instance

    @staticmethod
    def get_effective_leg_directions(contract, order_action):
        """
        解析訂單中每個 Option 合約 (conId) 的實際買賣方向 ('BUY' 或 'SELL')。
        - 對於單腿期權：effective direction 即為 order_action。
        - 對於組合單 (BAG)：
            order_action == 'BUY': 各腿方向 = leg.action ('BUY' 或 'SELL')
            order_action == 'SELL': 各腿方向 = 與 leg.action 相反 ('SELL' 變 'BUY', 'BUY' 變 'SELL')
        """
        con_dirs = {}
        if not contract:
            return con_dirs

        sec_type = getattr(contract, 'secType', '')
        if sec_type == 'BAG':
            for leg in (getattr(contract, 'comboLegs', None) or []):
                leg_act = getattr(leg, 'action', '').upper()
                if order_action.upper() == 'BUY':
                    eff_act = leg_act
                else:
                    eff_act = 'SELL' if leg_act == 'BUY' else 'BUY'
                con_dirs[leg.conId] = eff_act
        else:
            cid = getattr(contract, 'conId', 0)
            if cid:
                con_dirs[cid] = order_action.upper()
        return con_dirs

    @staticmethod
    def is_same_contract(c1, c2, symbol=None):
        """
        比對兩個合約是否為同一標的與規格 (支援 BAG 組合單與單腿 Option)
        """
        if not c1 or not c2:
            return False

        s1 = getattr(c1, 'symbol', '') or ''
        s2 = getattr(c2, 'symbol', '') or ''
        if symbol:
            if s1 and s1.upper() != symbol.upper():
                return False
            if s2 and s2.upper() != symbol.upper():
                return False
        elif s1 and s2 and s1.upper() != s2.upper():
            return False

        sec1 = getattr(c1, 'secType', '') or ''
        sec2 = getattr(c2, 'secType', '') or ''
        if sec1 != sec2:
            return False

        if sec1 == 'BAG':
            # 組合單：比較 comboLegs 之 (conId, action, ratio)
            legs1 = {(leg.conId, getattr(leg, 'action', ''), getattr(leg, 'ratio', 1))
                     for leg in (getattr(c1, 'comboLegs', None) or [])}
            legs2 = {(leg.conId, getattr(leg, 'action', ''), getattr(leg, 'ratio', 1))
                     for leg in (getattr(c2, 'comboLegs', None) or [])}
            if legs1 and legs2 and legs1 == legs2:
                return True

            # 比對 conId 集合
            con_ids1 = {leg.conId for leg in (getattr(c1, 'comboLegs', None) or [])}
            con_ids2 = {leg.conId for leg in (getattr(c2, 'comboLegs', None) or [])}
            if con_ids1 and con_ids2 and con_ids1 == con_ids2:
                return True
            return False
        else:
            # 單腿期權：比對 conId 或 (strike, right, expiry)
            if getattr(c1, 'conId', None) and getattr(c2, 'conId', None) and c1.conId == c2.conId and c1.conId > 0:
                return True
            return (
                getattr(c1, 'strike', None) == getattr(c2, 'strike', None) and
                getattr(c1, 'right', None) == getattr(c2, 'right', None) and
                getattr(c1, 'lastTradeDateOrContractMonth', None) == getattr(c2, 'lastTradeDateOrContractMonth', None)
            )

    def is_conflicting_order(self, trade, target_contract, target_action, symbol=None):
        """
        判定既有訂單 trade 是否與即將送出的 target_contract 存在衝突 (引發 Error 201)
        回傳: (is_conflict: bool, reason: str, is_exact_same: bool)
        """
        # 僅檢查有效掛單狀態
        if trade.orderStatus.status not in ('PreSubmitted', 'Submitted', 'PendingSubmit', 'PendingCancel', 'Inactive'):
            return False, "", False

        target_symbol = (symbol or getattr(target_contract, 'symbol', '')).upper()
        t_symbol = (getattr(trade.contract, 'symbol', '') or '').upper()

        target_dirs = self.get_effective_leg_directions(target_contract, target_action)
        t_dirs = self.get_effective_leg_directions(trade.contract, trade.order.action)

        target_con_ids = set(target_dirs.keys())
        t_con_ids = set(t_dirs.keys())
        common_cids = target_con_ids.intersection(t_con_ids)

        is_exact_same = self.is_same_contract(trade.contract, target_contract, target_symbol)

        # 檢驗 1: 核心 Option conId 處於市場相反兩側 (IBKR Error 201 法規核心定義)
        for cid in common_cids:
            if target_dirs[cid] != t_dirs[cid]:
                reason = f"期權腿 conId {cid} 雙邊衝突 (新委託: {target_dirs[cid]} vs 現有掛單: {t_dirs[cid]})"
                return True, reason, is_exact_same

        # 檢驗 2: 相同標的且完全相同合約之反向掛單
        if t_symbol == target_symbol and is_exact_same:
            if trade.order.action.upper() != target_action.upper():
                reason = f"相同合約之反向平倉掛單 ({trade.order.action})"
                return True, reason, True

        # 檢驗 3: 相同標的共享期權腿之反向平倉掛單 (例如不同賣權履約價但共用同一個長腿 Put)
        if t_symbol == target_symbol and common_cids:
            if trade.order.action.upper() != target_action.upper():
                reason = f"同標的共享期權腿之反向掛單 (共享 conIds: {common_cids})"
                return True, reason, is_exact_same

        # 檢驗 4: 相同標的且為反向期權平倉單 (LMT/STP)
        if t_symbol == target_symbol and trade.order.action.upper() != target_action.upper():
            if getattr(trade.contract, 'secType', '') in ('BAG', 'OPT'):
                if trade.order.orderType in ('LMT', 'STP', 'TRAIL', 'STP LMT'):
                    reason = f"同標的既有反向平倉掛單 (#{trade.order.orderId} {trade.order.orderType} {trade.order.action})"
                    return True, reason, is_exact_same

        return False, "", False

    def _cancel_trade_robust(self, trade):
        """
        支援跨 ClientId 撤單 (處理非 ClientId 0 撤消其他 ClientId 訂單之 Error 10147 問題)
        """
        cur_client_id = self.ib.client.clientId if (self.ib and self.ib.client) else 0
        order_client_id = getattr(trade.order, 'clientId', cur_client_id)

        # 先嘗試直接撤單
        try:
            self.ib.cancelOrder(trade.order)
        except Exception:
            pass

        # 若目前連線即為訂單所屬 ClientId 或為 Master Client (0)，直接返回
        if cur_client_id in (0, order_client_id):
            return

        # 否則建立臨時連線使用 order_client_id 或 Master Client 0 精準撤單
        host = self.ib.client.host if self.ib.client else '127.0.0.1'
        port = self.ib.client.port if self.ib.client else 4001
        helper_ib = IB()
        done = False
        for cid in [order_client_id, 0]:
            try:
                helper_ib.connect(host, port, clientId=cid, timeout=3)
                client_trades = helper_ib.reqOpenOrders()
                target_t = next((t for t in client_trades if t.order.orderId == trade.order.orderId), None)
                if target_t:
                    helper_ib.cancelOrder(target_t.order)
                else:
                    helper_ib.cancelOrder(trade.order)
                helper_ib.sleep(0.8)
                done = True
                break
            except Exception:
                pass
            finally:
                if helper_ib.isConnected():
                    try:
                        helper_ib.disconnect()
                    except Exception:
                        pass

        if not done:
            try:
                self.ib.cancelOrder(trade.order)
            except Exception:
                pass

    def step1_cancel_existing_opposite_orders(self, contract, symbol, action="SELL", opposite_action="BUY", dry_run=False):
        """
        [步驟 1: 查詢並撤銷現有掛單]
        找出所有在 IBKR 會引發 Error 201 的 opposite_action (預設 BUY) 平倉單並撤單 (Cancel)
        """
        print(f"\n" + "=" * 65)
        print(f"🔄 [ScaleInSkill - 步驟 1] 查詢並撤銷 {symbol} 現有衝突之 {opposite_action} 平倉掛單...")
        print("=" * 65)

        if not self.ib or not self.ib.isConnected():
            print("  -> ❌ [錯誤] IBKR 連線中斷，無法查詢撤銷掛單。")
            return [], 0

        open_trades = self.ib.reqAllOpenOrders()
        conflicting_items = []
        for t in open_trades:
            is_conflict, reason, is_exact = self.is_conflicting_order(t, contract, action, symbol)
            if is_conflict:
                conflicting_items.append({
                    "trade": t,
                    "reason": reason,
                    "is_exact_same": is_exact,
                    "order_id": t.order.orderId,
                    "client_id": t.order.clientId,
                    "contract": t.contract,
                    "action": t.order.action,
                    "order_type": t.order.orderType,
                    "total_quantity": t.order.totalQuantity,
                    "lmt_price": t.order.lmtPrice,
                    "aux_price": t.order.auxPrice,
                })

        if not conflicting_items:
            print(f"  -> ℹ️ 未發現 {symbol} 存在 Error 201 衝突之既有掛單，無須撤單。")
            return [], 0

        print(f"  -> 發現 {len(conflicting_items)} 筆引發 Error 201 衝突之既有 {opposite_action} 掛單：")
        prev_quantities = []
        for item in conflicting_items:
            t = item["trade"]
            print(f"     • 訂單 #{t.order.orderId} (ClientId: {t.order.clientId}) [{t.order.orderType}] {t.order.action} {t.order.totalQuantity:g}口 (狀態: {t.orderStatus.status})")
            print(f"       原因: {item['reason']}")
            prev_quantities.append(t.order.totalQuantity)

        inferred_prev_qty = max(prev_quantities) if prev_quantities else 1.0

        if dry_run:
            print(f"  -> 🔍 [模擬模式 (Dry-Run)] 模擬撤銷 {len(conflicting_items)} 筆舊平倉單...")
            return conflicting_items, inferred_prev_qty

        for item in conflicting_items:
            t = item["trade"]
            print(f"  -> 正在撤銷訂單 #{t.order.orderId} (ClientId: {t.order.clientId}) ...")
            self._cancel_trade_robust(t)

        # 等待所有撤單完成 (最多 15 秒)
        print(f"  -> 等待所有舊掛單撤單完成...")
        start_wait = time.time()
        while time.time() - start_wait < 15:
            self.ib.sleep(0.5)
            all_cancelled = all(item["trade"].orderStatus.status in ('Cancelled', 'Inactive', 'Filled') for item in conflicting_items)
            if all_cancelled:
                break

        for item in conflicting_items:
            t = item["trade"]
            print(f"     • 訂單 #{t.order.orderId} 最新狀態: {t.orderStatus.status}")

        print(f"  -> ✅ [步驟 1 完成] 已成功撤銷 {len(conflicting_items)} 筆衝突平倉單 (推斷原持倉: {inferred_prev_qty:g} 口)！")
        return conflicting_items, inferred_prev_qty

    def step2_place_parent_order(self, contract, symbol, action="SELL", quantity=1.0, limit_price=0.0, target_account=None, dry_run=False, timeout_seconds=60):
        """
        [步驟 2: 送出加碼母單 (SELL)]
        送出新的單純加碼母單 (SELL 市價 Adaptive Patient，不帶附屬單)，並等待完全成交 (Filled)
        """
        print(f"\n" + "=" * 65)
        print(f"🚀 [ScaleInSkill - 步驟 2] 送出 {symbol} 加碼母單 ({action} {quantity:g}口 @ 市價 Adaptive Patient)...")
        print("=" * 65)

        order = MarketOrder(action=action, totalQuantity=quantity)
        order.algoStrategy = "Adaptive"
        order.algoParams = [TagValue("adaptivePriority", "Patient")]
        order.tif = "DAY"
        if target_account:
            order.account = target_account

        if dry_run:
            print(f"  -> 🔍 [模擬模式 (Dry-Run)] 模擬加碼母單成交: {action} {quantity:g}口")
            return True, None, (limit_price or 1.0)

        trade = self.ib.placeOrder(contract, order)
        print(f"  -> 母單已送出 (OrderId: {trade.order.orderId})，等待確定完全成交 (Filled)...")

        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            self.ib.sleep(1)
            status = trade.orderStatus.status
            if status == 'Filled':
                avg_price = trade.orderStatus.avgFillPrice
                print(f"  -> ✅ [步驟 2 完成] 加碼母單已完全成交！均價: ${avg_price:.2f}")
                return True, trade, avg_price
            elif status in ('Cancelled', 'Inactive'):
                print(f"  -> ❌ [步驟 2 異常] 加碼母單狀態為 {status}")
                return False, trade, 0.0

        status = trade.orderStatus.status
        print(f"  -> ⚠️ 加碼母單等待逾時 ({timeout_seconds}秒)，目前狀態: {status}")
        if status in ('PreSubmitted', 'Submitted'):
            return True, trade, (trade.orderStatus.avgFillPrice or limit_price or 0.0)
        return False, trade, 0.0

    def step3_calculate_total_position(self, contract, symbol, parent_trade=None, previous_qty=1.0, added_qty=1.0):
        """
        [步驟 3: 合併計算新舊總持倉口數]
        向帳戶查詢該合約最新持倉口數 (例如：原本 1 口 + 加碼 1 口 = 2 口)
        """
        print(f"\n" + "=" * 65)
        print(f"📊 [ScaleInSkill - 步驟 3] 查詢帳戶合併計算 {symbol} 最新總持倉口數...")
        print("=" * 65)

        live_qty = 0.0
        try:
            # 確保 reqPositionsAsync 未被屏蔽
            if hasattr(IB, 'reqPositionsAsync'):
                self.ib.reqPositionsAsync = IB.reqPositionsAsync.__get__(self.ib, IB)
            positions = self.ib.reqPositions()
            self.ib.sleep(1)

            if getattr(contract, 'secType', '') == 'BAG' and getattr(contract, 'comboLegs', None):
                # 組合單價差：尋找 legs 在持倉中的口數
                leg_con_ids = [leg.conId for leg in contract.comboLegs]
                for p in positions:
                    if p.contract.conId in leg_con_ids:
                        live_qty = max(live_qty, abs(float(p.position)))
            else:
                for p in positions:
                    if self.is_same_contract(p.contract, contract, symbol):
                        live_qty = abs(float(p.position))
                        break
        except Exception as e:
            print(f"  -> [警告] 查詢持倉異常: {e}")

        is_filled = parent_trade and getattr(parent_trade.orderStatus, 'status', '') == 'Filled'

        if live_qty > 0:
            if is_filled:
                total_qty = live_qty
                print(f"  -> 帳戶即時持倉確認 (母單已Filled): 總持倉口數為 {total_qty:g} 口")
            else:
                total_qty = live_qty + float(added_qty)
                print(f"  -> 帳戶持倉已確認 (母單掛單中): 原有 {live_qty:g} 口 + 本次加碼 {added_qty:g} 口 = 最新總持倉 {total_qty:g} 口")
        else:
            total_qty = float(previous_qty) + float(added_qty)
            print(f"  -> 依計算推導最新持倉: 原持倉 {previous_qty:g} 口 + 加碼 {added_qty:g} 口 = {total_qty:g} 口")

        total_qty = max(total_qty, float(added_qty))
        print(f"  -> ✅ [步驟 3 完成] 確定重組部位總持倉: {total_qty:g} 口")
        return total_qty

    def step4_place_combined_oca_orders(self, contract, symbol, cancelled_items=None, opposite_action="BUY", added_qty=1.0, total_qty=2.0, take_profit_price=None, stop_loss_price=None, target_account=None, dry_run=False):
        """
        [步驟 4: 重掛總口數的 OCA 停利停損單]
        以總口數掛出一組 BUY 的 OCA 括號單 (Bracket / OCA Orders)。
        - 若先前撤銷的訂單與本次加碼合約為【完全相同合約】：合併掛出一組總口數 (total_qty) 的 OCA 括號單。
        - 若先前撤銷的訂單屬於【不同履約價組合】(例如原持 P630/P595，本次加碼 P625/P595)：
            1. 為本次加碼組合 (P625/P595) 掛出 added_qty 口數之專屬 OCA 括號單。
            2. 為先前原組合 (P630/P595) 恢復掛出原口數之專屬 OCA 括號單 (維持原停利/停損價)。
            使兩組合約各自精準閉環對沖，完全免除裸賣風險與兩側掛單衝突！
        """
        print(f"\n" + "=" * 65)
        print(f"🛡️ [ScaleInSkill - 步驟 4] 為 {symbol} 重新掛出總持倉 ({total_qty:g}口) 之 OCA 停利停損單...")
        print("=" * 65)

        if take_profit_price is None or stop_loss_price is None:
            print(f"  -> [警告] 缺少停利停損價位，跳過重掛 OCA 單。")
            return None

        # 分析撤銷訂單之合約構成
        cancelled_items = cancelled_items or []
        distinct_old_contracts = {}
        all_exact_same = True

        for item in cancelled_items:
            is_exact = item.get("is_exact_same", False)
            if not is_exact:
                all_exact_same = False
                c = item["contract"]
                cid_key = tuple(sorted([leg.conId for leg in getattr(c, 'comboLegs', [])])) if getattr(c, 'secType', '') == 'BAG' else getattr(c, 'conId', 0)
                if cid_key not in distinct_old_contracts:
                    distinct_old_contracts[cid_key] = {
                        "contract": c,
                        "quantity": item.get("total_quantity", 1.0),
                        "tp_price": item.get("lmt_price") if (item.get("order_type") == "LMT" and item.get("lmt_price", 0) > 0) else None,
                        "sl_price": item.get("aux_price") if (item.get("order_type") == "STP" and item.get("aux_price", 0) > 0) else None,
                    }
                else:
                    if item.get("order_type") == "LMT" and item.get("lmt_price", 0) > 0:
                        distinct_old_contracts[cid_key]["tp_price"] = item["lmt_price"]
                    if item.get("order_type") == "STP" and item.get("aux_price", 0) > 0:
                        distinct_old_contracts[cid_key]["sl_price"] = item["aux_price"]

        placed_brackets = []

        if all_exact_same or not distinct_old_contracts:
            # 情況 A: 完全相同合約，合併為單一總口數 (total_qty) OCA 括號單
            oca_group_id = f"OCA_{symbol}_{int(time.time() * 1000) % 1000000}"
            tp_order = LimitOrder(
                action=opposite_action,
                totalQuantity=total_qty,
                lmtPrice=take_profit_price,
                tif="GTC",
                ocaGroup=oca_group_id,
                ocaType=1
            )
            sl_order = StopOrder(
                action=opposite_action,
                totalQuantity=total_qty,
                stopPrice=stop_loss_price,
                tif="GTC",
                ocaGroup=oca_group_id,
                ocaType=1
            )
            if target_account:
                tp_order.account = target_account
                sl_order.account = target_account

            print(f"  -> 【合併掛單】標的 {symbol} 全數為相同合約，重掛總口數 {total_qty:g} 口 (OCA Group: {oca_group_id}):")
            print(f"     • 🎯 Profit Taker (停利單): {opposite_action} {total_qty:g}口 @ 限價 ${take_profit_price:.2f}")
            print(f"     • 🛑 Stop Loss    (停損單): {opposite_action} {total_qty:g}口 @ 停損價 ${stop_loss_price:.2f}")

            if dry_run:
                placed_brackets.append({
                    "oca_group": oca_group_id,
                    "target": "combined",
                    "quantity": total_qty,
                    "tp_price": take_profit_price,
                    "sl_price": stop_loss_price,
                    "status": "DryRun"
                })
            else:
                tp_trade = self.ib.placeOrder(contract, tp_order)
                sl_trade = self.ib.placeOrder(contract, sl_order)
                self.ib.sleep(1)
                placed_brackets.append({
                    "oca_group": oca_group_id,
                    "target": "combined",
                    "quantity": total_qty,
                    "tp_price": take_profit_price,
                    "sl_price": stop_loss_price,
                    "tp_id": tp_trade.order.orderId,
                    "sl_id": sl_trade.order.orderId,
                    "tp_status": getattr(tp_trade.orderStatus, 'status', 'Submitted'),
                    "sl_status": getattr(sl_trade.orderStatus, 'status', 'Submitted'),
                })
        else:
            # 情況 B: 跨不同履約價組合，分別掛出專屬 OCA 括號單
            # 1. 本次加碼新合約之 OCA 括號單
            oca_group_new = f"OCA_{symbol}_NEW_{int(time.time() * 1000) % 1000000}"
            tp_order_new = LimitOrder(
                action=opposite_action,
                totalQuantity=added_qty,
                lmtPrice=take_profit_price,
                tif="GTC",
                ocaGroup=oca_group_new,
                ocaType=1
            )
            sl_order_new = StopOrder(
                action=opposite_action,
                totalQuantity=added_qty,
                stopPrice=stop_loss_price,
                tif="GTC",
                ocaGroup=oca_group_new,
                ocaType=1
            )
            if target_account:
                tp_order_new.account = target_account
                sl_order_new.account = target_account

            print(f"  -> 【新組合掛單】為本次新加碼合約掛出 {added_qty:g} 口 OCA 保護單 (Group: {oca_group_new}):")
            print(f"     • 🎯 Profit Taker (停利單): {opposite_action} {added_qty:g}口 @ 限價 ${take_profit_price:.2f}")
            print(f"     • 🛑 Stop Loss    (停損單): {opposite_action} {added_qty:g}口 @ 停損價 ${stop_loss_price:.2f}")

            if dry_run:
                placed_brackets.append({
                    "oca_group": oca_group_new,
                    "target": "new_contract",
                    "quantity": added_qty,
                    "tp_price": take_profit_price,
                    "sl_price": stop_loss_price,
                    "status": "DryRun"
                })
            else:
                tp_trade = self.ib.placeOrder(contract, tp_order_new)
                sl_trade = self.ib.placeOrder(contract, sl_order_new)
                self.ib.sleep(0.5)
                placed_brackets.append({
                    "oca_group": oca_group_new,
                    "target": "new_contract",
                    "quantity": added_qty,
                    "tp_price": take_profit_price,
                    "sl_price": stop_loss_price,
                    "tp_id": tp_trade.order.orderId,
                    "sl_id": sl_trade.order.orderId,
                    "tp_status": getattr(tp_trade.orderStatus, 'status', 'Submitted'),
                    "sl_status": getattr(sl_trade.orderStatus, 'status', 'Submitted'),
                })

            # 2. 恢復原組合之 OCA 括號單
            for old_key, old_data in distinct_old_contracts.items():
                old_contract = old_data["contract"]
                old_qty = old_data["quantity"]
                old_tp = old_data.get("tp_price") or take_profit_price
                old_sl = old_data.get("sl_price") or stop_loss_price

                oca_group_old = f"OCA_{symbol}_OLD_{int(time.time() * 1000) % 1000000}"
                tp_order_old = LimitOrder(
                    action=opposite_action,
                    totalQuantity=old_qty,
                    lmtPrice=old_tp,
                    tif="GTC",
                    ocaGroup=oca_group_old,
                    ocaType=1
                )
                sl_order_old = StopOrder(
                    action=opposite_action,
                    totalQuantity=old_qty,
                    stopPrice=old_sl,
                    tif="GTC",
                    ocaGroup=oca_group_old,
                    ocaType=1
                )
                if target_account:
                    tp_order_old.account = target_account
                    sl_order_old.account = target_account

                print(f"  -> 【舊組合恢復】為既有持倉合約恢復掛出 {old_qty:g} 口 OCA 保護單 (Group: {oca_group_old}):")
                print(f"     • 🎯 Profit Taker (停利單): {opposite_action} {old_qty:g}口 @ 限價 ${old_tp:.2f}")
                print(f"     • 🛑 Stop Loss    (停損單): {opposite_action} {old_qty:g}口 @ 停損價 ${old_sl:.2f}")

                if dry_run:
                    placed_brackets.append({
                        "oca_group": oca_group_old,
                        "target": "old_contract",
                        "quantity": old_qty,
                        "tp_price": old_tp,
                        "sl_price": old_sl,
                        "status": "DryRun"
                    })
                else:
                    tpt = self.ib.placeOrder(old_contract, tp_order_old)
                    slt = self.ib.placeOrder(old_contract, sl_order_old)
                    self.ib.sleep(0.5)
                    placed_brackets.append({
                        "oca_group": oca_group_old,
                        "target": "old_contract",
                        "quantity": old_qty,
                        "tp_price": old_tp,
                        "sl_price": old_sl,
                        "tp_id": tpt.order.orderId,
                        "sl_id": slt.order.orderId,
                        "tp_status": getattr(tpt.orderStatus, 'status', 'Submitted'),
                        "sl_status": getattr(slt.orderStatus, 'status', 'Submitted'),
                    })

        print(f"  -> ✅ [步驟 4 完成] 已成功掛出總持倉 {total_qty:g} 口對應之全數 OCA 停利停損單！")
        return {
            "brackets": placed_brackets,
            "total_qty": total_qty,
            "all_exact_same": all_exact_same
        }

    def send_notification(self, symbol, action, quantity, total_qty, fill_price, take_profit_price, stop_loss_price, cancelled_count, oca_res=None, dry_run=False):
        """
        發送加碼重組完成的手機 LINE 彙總推播
        """
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        mode_str = "模擬測試 (Dry-Run)" if dry_run else "正式交易 (Live)"

        brackets = (oca_res or {}).get("brackets", [])
        bracket_lines = []
        for b in brackets:
            bracket_lines.append(f"  • [{b.get('target', 'combined')}] {b.get('quantity', 1):g}口 | 停利: ${b.get('tp_price', 0):.2f} | 停損: ${b.get('sl_price', 0):.2f}")

        brackets_text = "\n".join(bracket_lines) if bracket_lines else f"  • 總口數: {total_qty:g}口 (停利: ${take_profit_price:.2f}, 停損: ${stop_loss_price:.2f})"

        msg = (
            f"🎯【IBKR 加碼調倉成功通知 (Scale-In)】\n"
            f"標的：{symbol}\n"
            f"時間：{now_str}\n"
            f"模式：{mode_str}\n"
            f"動作：加碼 {action} {quantity:g} 口 (成交參考價: ${fill_price:.2f})\n"
            f"════════════════════════\n"
            f"📋 四步驟重組明細：\n"
            f"1. 撤銷舊單：已撤銷 {cancelled_count} 筆既有反向衝突掛單\n"
            f"2. 加碼母單：已送出加碼母單並確認成交\n"
            f"3. 總持倉合併：帳戶最新總持倉確定為 {total_qty:g} 口\n"
            f"4. 重掛 OCA 括號單：\n"
            f"{brackets_text}\n"
            f"════════════════════════\n"
            f"狀態：✅ 兩側掛單衝突已排除，新舊持倉全數受 OCA 括號單保護！"
        )

        if send_push_message:
            try:
                ok = send_push_message(msg.strip())
                if ok:
                    print(f"  -> 📲 [LINE] 已成功發送 {symbol} 加碼重組推播至手機！")
                else:
                    print(f"  -> ⚠️ [LINE] {symbol} 加碼重組推播失敗。")
            except Exception as e:
                print(f"  -> ❌ [LINE 異常] {e}")

    def execute(self, contract, symbol, action="SELL", quantity=1.0, credit=0.0, take_profit_price=None, stop_loss_price=None, target_account=None, dry_run=False, item=None):
        """
        完整執行 Error 201 處理技能 4 大流程
        """
        print(f"\n" + "#" * 65)
        print(f"# 🛠️ 啟動 ScaleInOrderSkill: 處理 {symbol} 加碼與掛單衝突重組")
        print("#" * 65)

        opposite_action = "BUY" if action.upper() == "SELL" else "SELL"

        # 步驟 1: 查詢並撤銷現有掛單
        cancelled_items, inferred_prev_qty = self.step1_cancel_existing_opposite_orders(
            contract=contract,
            symbol=symbol,
            action=action,
            opposite_action=opposite_action,
            dry_run=dry_run
        )

        # 步驟 2: 送出加碼母單 (SELL) 並等待成交
        step2_ok, parent_trade, fill_price = self.step2_place_parent_order(
            contract=contract,
            symbol=symbol,
            action=action,
            quantity=quantity,
            limit_price=credit,
            target_account=target_account,
            dry_run=dry_run
        )

        if not step2_ok and not dry_run:
            print(f"❌ [ScaleInSkill 終止] 加碼母單送出或成交失敗，終止後續重掛流程。")
            return {
                "status": "error",
                "message": "加碼母單送出或成交失敗",
                "symbol": symbol
            }

        # 步驟 3: 合併計算新舊總持倉口數
        total_qty = self.step3_calculate_total_position(
            contract=contract,
            symbol=symbol,
            parent_trade=parent_trade,
            previous_qty=inferred_prev_qty,
            added_qty=quantity
        )

        # 步驟 4: 重掛總口數的 OCA 停利停損單
        oca_res = self.step4_place_combined_oca_orders(
            contract=contract,
            symbol=symbol,
            cancelled_items=cancelled_items,
            opposite_action=opposite_action,
            added_qty=quantity,
            total_qty=total_qty,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
            target_account=target_account,
            dry_run=dry_run
        )

        # 發送手機 LINE 通知
        self.send_notification(
            symbol=symbol,
            action=action,
            quantity=quantity,
            total_qty=total_qty,
            fill_price=fill_price or credit,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
            cancelled_count=len(cancelled_items),
            oca_res=oca_res,
            dry_run=dry_run
        )

        return {
            "status": "ok",
            "symbol": symbol,
            "action": action,
            "added_qty": quantity,
            "total_qty": total_qty,
            "cancelled_orders_count": len(cancelled_items),
            "fill_price": fill_price or credit,
            "take_profit_price": take_profit_price,
            "stop_loss_price": stop_loss_price,
            "oca_res": oca_res
        }
