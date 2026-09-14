---
name: memory-skill
description: 負責管理歷史推薦標的記錄、冷卻期（Cooldown Window）計算與黑名單過濾，確保不重複推薦相同標的。
---

# Memory Skill (歷史標的記憶與冷卻管理技能)

## 📌 職責說明
`Memory Skill` 專門負責管理交易系統的歷史選股記憶，確保自動化流水線具備時間感知能力，不會在短時間內重複推薦或重疊建倉同一檔標的。

## ⚙️ 核心功能
1. **冷卻期機制 (Cooldown Window)**：
   - 預設冷卻期為 14 天（可自由設定天數或設為 0 代表永久不重複）。
   - 當標的推薦已超過冷卻天數，且內部人再次有顯著爆量買盤時，允許解除冷卻並重新評估。
2. **黑名單機制 (Blacklist)**：
   - 支援手動加入永久排除名單，杜絕特定流動性差或地雷標的。
3. **豐富後設資料持久化 (`selection_history.json`)**：
   - 記錄推薦時間、內部人淨買額、所搭配的期權 CALL 合約規格（履約價、到期日、現價）等。
4. **安全原子寫入**：
   - 透過暫存檔原子替換寫入，防止突發斷電或程式崩潰導致 JSON 損毀。

## 🚀 API 快速導覽
```python
from trade.skills.memory_skill import SelectionMemory

memory = SelectionMemory()

# 取得目前被冷卻/排除的標的清單
excluded_symbols = memory.get_excluded_symbols()  # e.g. ['GME', 'UBER']

# 檢查特定標的是否在冷卻中
is_cooling, reason = memory.is_cooling_down("UBER")

# 紀錄新的選股推薦
memory.record_selection(
    symbol="ONON",
    company_name="On Holding AG",
    insider_net_buy=8500000.0,
    selected_call={"strike": 29.0, "exp_date": "20260925", "ref_price": 1.00}
)
```

## 🧪 獨立測試指令
```powershell
python trade/skills/memory_skill/memory.py
```
