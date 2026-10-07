# WalkUpOrderSkill (自適應步進修單技能)

## 技能定位
專門針對 IBKR 自動交易中的委託執行層進行深度優化，全面取代高滑點的市價單 (Market Order) 與低撮合率的發呆限價單 (Passive Limit Order)。

## 核心機制與設計哲學
1. **動態步進讓步 (Dynamic Concession)**：
   - 預設每 3 秒讓步 $0.05（可自訂），最多讓步 $0.15 (max_slippage)。
   - **賣單 (SELL)**：由基準限價逐級向下讓步 ($base - 0.05 -> $base - 0.10 -> $base - 0.15)。
   - **買單 (BUY)**：由基準限價逐級向上加價 ($base + 0.05 -> $base + 0.10 -> $base + 0.15)。
2. **原生訂單替換 (IBKR In-Place Modify / Replace)**：
   - 沿用同一 `orderId` 與訂單實例，透過 `ib.placeOrder(contract, order)` 重新送出，IBKR 原生識別為 Order Replace，不觸發重複下單或資金佔用。
3. **逾時防接刀保護 (Timeout & Auto-Cancel)**：
   - 若 3 次讓步（共 9 秒）仍未成交，表示市場流動性枯竭或價格快速逆行，立即主動發送 `ib.cancelOrder(order)` 徹底撤單，防止死水期被動接刀。
4. **全品類相容 (Multi-Asset Compatibility)**：
   - 支援個股 (STK)、期貨 (FUT)、單腿期權 (OPT) 以及垂直價差/鐵蝶式複合單 (BAG Combo)。

## 主要介面
- `walk_up_limit_price(ib, contract, order, current_mid, max_slippage=0.15, step=0.05, step_time=3.0, max_steps=3)`
- `execute_walk_up_order(ib, contract, action, quantity, current_mid, ...)`
- `WalkUpOrderSkill(ib_instance)`
