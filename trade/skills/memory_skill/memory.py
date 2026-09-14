#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Memory Skill: 歷史推薦標的紀錄與冷卻過濾器 (trade/skills/memory_skill/memory.py)
================================================================================
職責：
  1. 負責讀寫 trade/memory/selection_history.json
  2. 提供冷卻期（Cooldown Window）計算，避免近期重複推薦相同標的
  3. 提供黑名單（Blacklist）管理，供手動排除特定標的
  4. 儲存推薦的豐富後設資料（時間、內部人買額、所選 Call 合約細節等）
================================================================================
"""

import os
import sys
import json
import datetime
import tempfile

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

DEFAULT_MEMORY_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "memory"))
DEFAULT_HISTORY_FILE = os.path.join(DEFAULT_MEMORY_DIR, "selection_history.json")


class SelectionMemory:
    """
    歷史標的記憶與防重複過濾器
    """

    def __init__(self, file_path=None, default_cooldown_days=14, cooldown_days=None):
        self.file_path = file_path or DEFAULT_HISTORY_FILE
        if cooldown_days is not None:
            default_cooldown_days = cooldown_days
        self.default_cooldown_days = default_cooldown_days
        self.data = {
            "version": "1.0",
            "cooldown_days": default_cooldown_days,
            "blacklist": [],
            "history": [],
        }
        self.load()
        if cooldown_days is not None:
            self.data["cooldown_days"] = cooldown_days

    def load(self):
        """從 JSON 檔案載入歷史紀錄"""
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        self.data.update(loaded)
            except Exception as e:
                print(f"[WARN] [MemorySkill] 讀取歷史檔案失敗 ({self.file_path}): {e}")
        else:
            # 自動建立目錄與初始結構
            os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
            self.save()

    def save(self):
        """原子寫入（Atomic Write）儲存歷史紀錄，防止並行寫入或斷電損壞"""
        dir_name = os.path.dirname(self.file_path)
        os.makedirs(dir_name, exist_ok=True)

        try:
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
                json.dump(self.data, tf, indent=2, ensure_ascii=False)
                temp_name = tf.name
            # Windows 原子替換
            if os.path.exists(self.file_path):
                os.replace(temp_name, self.file_path)
            else:
                os.rename(temp_name, self.file_path)
        except Exception as e:
            print(f"[ERROR] [MemorySkill] 儲存歷史檔案異常: {e}")
            if os.path.exists(temp_name):
                try:
                    os.remove(temp_name)
                except Exception:
                    pass

    @property
    def cooldown_days(self):
        return self.data.get("cooldown_days", self.default_cooldown_days)

    @cooldown_days.setter
    def cooldown_days(self, days):
        self.data["cooldown_days"] = max(0, int(days))
        self.save()

    @property
    def blacklist(self):
        return [s.upper() for s in self.data.get("blacklist", [])]

    def add_blacklist(self, symbol, reason=None):
        """手動新增永久排除黑名單"""
        sym = symbol.strip().upper()
        bl = self.data.setdefault("blacklist", [])
        if sym not in bl:
            bl.append(sym)
            self.save()
            print(f"[INFO] [MemorySkill] 已將 {sym} 加入永久黑名單{f' (原因: {reason})' if reason else ''}")

    def remove_blacklist(self, symbol):
        """自黑名單中移除"""
        sym = symbol.strip().upper()
        bl = self.data.setdefault("blacklist", [])
        if sym in bl:
            bl.remove(sym)
            self.save()
            print(f"[INFO] [MemorySkill] 已將 {sym} 自黑名單中移除")

    def get_excluded_symbols(self, cooldown_days=None):
        """
        取得目前必須排除的所有標的代號清單（黑名單 + 冷卻期內的標的）。
        如果 cooldown_days <= 0，則排除歷史上所有出現過的標的。
        """
        excluded = set(self.blacklist)
        days = self.cooldown_days if cooldown_days is None else cooldown_days
        now = datetime.datetime.now()

        for record in self.data.get("history", []):
            sym = record.get("symbol", "").strip().upper()
            if not sym:
                continue

            # 若 days <= 0，代表永久不可重複
            if days <= 0:
                excluded.add(sym)
                continue

            # 計算冷卻時間
            rec_time_str = record.get("recommended_at", "")
            try:
                # 支援各種 ISO 或標準日期時間格式
                if "T" in rec_time_str:
                    rec_dt = datetime.datetime.fromisoformat(rec_time_str)
                else:
                    rec_dt = datetime.datetime.strptime(rec_time_str, "%Y-%m-%d %H:%M:%S")
            except Exception:
                try:
                    rec_dt = datetime.datetime.strptime(rec_time_str[:10], "%Y-%m-%d")
                except Exception:
                    rec_dt = now

            diff_days = (now - rec_dt).total_seconds() / 86400.0
            if diff_days < days:
                excluded.add(sym)

        return sorted(list(excluded))

    def is_cooling_down(self, symbol, cooldown_days=None):
        """
        檢查單一標的是否處於冷卻或排除狀態
        回傳: (is_excluded: bool, reason: str)
        """
        sym = symbol.strip().upper()
        if sym in self.blacklist:
            return True, f"標的 {sym} 位於手動黑名單中"

        days = self.cooldown_days if cooldown_days is None else cooldown_days
        now = datetime.datetime.now()

        for record in reversed(self.data.get("history", [])):
            if record.get("symbol", "").strip().upper() == sym:
                if days <= 0:
                    return True, f"標的 {sym} 已於 {record.get('recommended_at')} 推薦過 (永久排除模式)"

                rec_time_str = record.get("recommended_at", "")
                try:
                    if "T" in rec_time_str:
                        rec_dt = datetime.datetime.fromisoformat(rec_time_str)
                    else:
                        rec_dt = datetime.datetime.strptime(rec_time_str, "%Y-%m-%d %H:%M:%S")
                    diff_days = (now - rec_dt).total_seconds() / 86400.0
                    if diff_days < days:
                        remaining = round(days - diff_days, 1)
                        return True, f"標的 {sym} 於 {record.get('recommended_at')} 推薦過，尚在冷卻期中 (剩餘約 {remaining} 天)"
                except Exception:
                    pass

        return False, ""

    def record_selection(
        self,
        symbol,
        company_name=None,
        insider_net_buy=None,
        net_buy_total=None,
        selected_call=None,
        contract=None,
        status="recommended",
        notes=None,
        reason=None,
        **kwargs
    ):
        """
        紀錄一筆新的推薦歷史
        """
        sym = symbol.strip().upper()
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        net_buy_val = insider_net_buy if insider_net_buy is not None else net_buy_total
        call_val = selected_call if selected_call is not None else contract
        notes_val = notes if notes is not None else reason

        entry = {
            "symbol": sym,
            "recommended_at": now_str,
            "company_name": company_name or "",
            "insider_net_buy": float(net_buy_val) if net_buy_val is not None else 0.0,
            "selected_call": call_val or {},
            "status": status,
            "notes": notes_val or "",
        }

        history = self.data.setdefault("history", [])
        history.append(entry)
        self.save()
        print(f"[SUCCESS] [MemorySkill] 已成功記錄推薦標的: {sym} (時間: {now_str})")
        return entry

    def get_history(self, limit=10):
        """取得最近 N 筆推薦紀錄"""
        h = self.data.get("history", [])
        return h[-limit:] if limit else h


if __name__ == "__main__":
    print("=" * 60)
    print(" Memory Skill 獨立單元測試")
    print("=" * 60)

    memory = SelectionMemory()
    print(f"歷史記錄檔位置: {memory.file_path}")
    print(f"目前設定冷卻天數: {memory.cooldown_days} 天")
    print(f"永久黑名單: {memory.blacklist}")

    excluded = memory.get_excluded_symbols()
    print(f"目前被排除 (冷卻中或黑名單) 的標的清單: {excluded}")

    for test_sym in ["UBER", "GME", "DELL", "AAPL"]:
        cooling, reason = memory.is_cooling_down(test_sym)
        print(f"  • {test_sym:<5}: {'⛔ 排除中' if cooling else '✅ 可推薦'} ({reason or '無衝突'})")
