# ScaleInOrderSkill (加碼防衝突與 OCA 括號單重構技能)

## 📌 技能簡介
當交易系統執行加碼委託 (例如在已有 Short Bull Put 或空單部位的情況下再次送出 `SELL` 委託單) 時，若交易所存在原先該合約之未成交反向平倉掛單 (例如 `BUY` 停利單或 `BUY` 停損單)，IBKR 將依據美股期權法規退回訂單並回報：
```text
Error 201: Order rejected - reason:Cannot have open orders on both sides of the same US Option contract.
You are attempting to add an order for a contract where an open order already exists on the opposite side of the market.
Customers are prevented, by regulation, from entering a buy and a sell order for the same option contract.
```
本 Skill 專門處理此問題，透過標準 4 步驟重組機制解除兩側掛單衝突，順利完成加碼並為總持倉重新掛出一組 OCA 停利停損括號單。

---

## 🎯 四大執行流程

```
[步驟 1: 查詢並撤銷現有掛單]
   找出所有該合約尚未成交的反向平倉單 (BUY 停利/停損) 並撤單 (Cancel)
                  │
                  ▼
[步驟 2: 送出加碼母單 (SELL)]
   送出新的單純 SELL 加碼母單 (市價 Adaptive Patient)，不帶附屬單，等待確定完全成交 (Filled)
                  │
                  ▼
[步驟 3: 合併計算新舊總持倉口數]
   向帳戶查詢該合約最新持倉口數 (例如：原本 1 口 + 加碼 1 口 = 2 口)
                  │
                  ▼
[步驟 4: 重掛總口數的 OCA 停利停損單]
   以總口數 2 口重新掛出一組 BUY 的 OCA 括號單 (One-Cancels-All Bracket)
```

1. **步驟 1: 查詢並撤銷現有掛單**
   - 掃描 `ib.reqAllOpenOrders()`，精準比對同一合約（支援 `BAG` 垂直價差組合單之 legs 與單腿期權）。
   - 撤銷所有未成交之反向掛單 (`BUY` 停利與停損)，等待確認全數取消 (`Cancelled`)。
   - 記錄舊單口數作為推導基礎。

2. **步驟 2: 送出加碼母單 (SELL)**
   - 獨立送出母單 (`action=SELL`, `MKT` Adaptive Patient)，不附加子單以避免再次觸發法規衝突。
   - 等待母單完全成交 (`Filled`)，記錄實際成交均價。

3. **步驟 3: 合併計算新舊總持倉口數**
   - 查詢 `ib.reqPositions()`，計算該標的/組合腿之真實最新淨持倉。
   - 確定加碼後的最新總口數（如 1 + 1 = 2 口）。

4. **步驟 4: 重掛總口數的 OCA 停利停損單**
   - 建立同一個 `ocaGroup`（如 `OCA_META_xxxxxx`，`ocaType=1` 互斥取消）。
   - 掛出總口數之 `BUY` LimitOrder (停利單) 與 `BUY` StopOrder (停損單)。
   - 透過 `trade/notifier.py` 發送加碼調倉完成的手機 LINE 彙總推播。

---

## 💻 調用範例

```python
from skills.scale_in_order_skill import ScaleInOrderSkill

skill = ScaleInOrderSkill(ib)
result = skill.execute(
    contract=combo_or_contract,
    symbol="META",
    action="SELL",
    quantity=1.0,
    credit=6.15,
    take_profit_price=2.70,
    stop_loss_price=10.80,
    target_account="U19632085",
    dry_run=False,
    item=suggestion_item
)
```
