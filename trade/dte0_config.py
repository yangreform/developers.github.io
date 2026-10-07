#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DTE0 Configuration Helper (trade/dte0_config.py)
================================================================================
專門維護與讀寫 trade/.env 中的 DTE0_CONFIG_JSON：
  1. load_dte0_config(): 即時讀取並解析 DTE0_CONFIG_JSON，支援容錯與格式自動修復。
  2. save_dte0_config(cfg): 將設定安全寫回 trade/.env。
  3. update_dte0_wings(symbol, wing_call, wing_put): 更新指定標的之上下翼點位。
  4. clear_dte0_wings(symbol): 將指定標的之上下翼點位設為 "NIL"。
================================================================================
"""

import os
import json
import re

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')

DEFAULT_DTE0_CONFIG = {
    "SPX": {
        "symbols": ["SPX"],
        "wing_width_call": 20,
        "wing_width_put": 20,
        "bid_up": 14,
        "iron": "short",
        "price_jump": 20,
        "TP": 4,
        "hedge_sym": "MES",
        "hedge_qty": 1,
        "hedge_step": 5.0,
        "buffer": 2.5,
        "max_qty": 2,
        "wing_call": "NIL",
        "wing_put": "NIL"
    }
}


def safe_parse_json(raw_str: str) -> dict:
    """容錯解析 JSON 字串，自動修復無引號鍵名與缺失的結尾括號。"""
    if not raw_str or not raw_str.strip():
        return {}
    clean_str = raw_str.strip().strip("'").strip('"').strip()

    # 1. 嘗試直接解析
    try:
        return json.loads(clean_str)
    except Exception:
        pass

    # 2. 嘗試自動修復常見格式錯誤 (如 wing_call: -> "wing_call": 以及補全括號)
    repaired = clean_str
    repaired = re.sub(r'(\b\w+\b)\s*:', r'"\1":', repaired)
    # 確保只有單層雙引號
    repaired = re.sub(r'""(\w+)""', r'"\1"', repaired)

    # 檢查大括號閉合
    open_count = repaired.count('{')
    close_count = repaired.count('}')
    if open_count > close_count:
        repaired += '}' * (open_count - close_count)

    try:
        return json.loads(repaired)
    except Exception:
        pass

    # 3. 正則表達式提取核心欄位作為保底
    extracted = {}
    spx_match = re.search(r'("SPX"|\bSPX\b)', clean_str)
    if spx_match:
        extracted["SPX"] = {"symbols": ["SPX"], "hedge_sym": "MES", "hedge_qty": 1}
        w_c = re.search(r'wing_call["\s:]+["\']?([^,"\s\}]+)', clean_str)
        w_p = re.search(r'wing_put["\s:]+["\']?([^,"\s\}]+)', clean_str)
        h_s = re.search(r'hedge_sym["\s:]+["\']?([^,"\s\}]+)', clean_str)
        h_q = re.search(r'hedge_qty["\s:]+([0-9]+)', clean_str)
        if w_c:
            extracted["SPX"]["wing_call"] = w_c.group(1).strip()
        if w_p:
            extracted["SPX"]["wing_put"] = w_p.group(1).strip()
        if h_s:
            extracted["SPX"]["hedge_sym"] = h_s.group(1).strip()
        if h_q:
            extracted["SPX"]["hedge_qty"] = int(h_q.group(1).strip())
        return extracted

    return {}


def load_dte0_config() -> dict:
    """即時自 trade/.env 讀取 DTE0_CONFIG_JSON。"""
    raw = ""
    if os.path.exists(ENV_PATH):
        try:
            with open(ENV_PATH, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line_s = line.strip()
                    if line_s.startswith('DTE0_CONFIG_JSON='):
                        raw = line_s.split('=', 1)[1].strip()
                        break
        except Exception as e:
            print(f"[警告] 讀取 .env 失敗: {e}")

    if not raw:
        raw = os.getenv("DTE0_CONFIG_JSON", "")

    parsed = safe_parse_json(raw)
    if not parsed:
        parsed = dict(DEFAULT_DTE0_CONFIG)

    # 確保 SPX 鍵存在基本結構
    if "SPX" not in parsed:
        parsed["SPX"] = dict(DEFAULT_DTE0_CONFIG["SPX"])
    else:
        for k, v in DEFAULT_DTE0_CONFIG["SPX"].items():
            if k not in parsed["SPX"]:
                parsed["SPX"][k] = v

    return parsed


def save_dte0_config(config_dict: dict) -> bool:
    """將 DTE0_CONFIG_JSON 寫回 trade/.env。"""
    try:
        json_str = json.dumps(config_dict, ensure_ascii=False)
        target_line = f"DTE0_CONFIG_JSON='{json_str}'\n"

        lines = []
        found = False
        if os.path.exists(ENV_PATH):
            with open(ENV_PATH, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if line.strip().startswith('DTE0_CONFIG_JSON='):
                        lines.append(target_line)
                        found = True
                    else:
                        lines.append(line)

        if not found:
            lines.append(target_line)

        with open(ENV_PATH, 'w', encoding='utf-8') as f:
            f.writelines(lines)

        os.environ["DTE0_CONFIG_JSON"] = json_str
        return True
    except Exception as e:
        print(f"[錯誤] 寫入 DTE0_CONFIG_JSON 至 .env 失敗: {e}")
        return False


def update_dte0_wings(symbol: str = "SPX", wing_call=None, wing_put=None, hedge_sym: str = "MES", hedge_qty: int = 1) -> dict:
    """更新指定標的之 wing_call 與 wing_put 點位。"""
    cfg = load_dte0_config()
    sym_upper = symbol.upper()
    if sym_upper not in cfg:
        cfg[sym_upper] = {
            "symbols": [sym_upper],
            "hedge_sym": hedge_sym or "MES",
            "hedge_qty": hedge_qty or 1,
            "wing_call": "NIL",
            "wing_put": "NIL"
        }

    item = cfg[sym_upper]
    if hedge_sym:
        item["hedge_sym"] = hedge_sym
    if hedge_qty:
        item["hedge_qty"] = int(hedge_qty)

    def _fmt(val):
        if val is None or str(val).strip().upper() in ('NIL', 'NONE', ''):
            return "NIL"
        try:
            f = float(val)
            return f"{f:g}"
        except (ValueError, TypeError):
            return str(val).strip()

    if wing_call is not None:
        item["wing_call"] = _fmt(wing_call)
    if wing_put is not None:
        item["wing_put"] = _fmt(wing_put)

    save_dte0_config(cfg)
    print(f"[DTE0 設定更新] 成功更新 trade/.env 之 DTE0_CONFIG_JSON: {sym_upper} wing_call=\"{item['wing_call']}\", wing_put=\"{item['wing_put']}\" (hedge_sym={item.get('hedge_sym')}, qty={item.get('hedge_qty')})")
    return cfg


def clear_dte0_wings(symbol: str = "SPX") -> dict:
    """將指定標的之 wing_call 與 wing_put 設為 NIL。"""
    return update_dte0_wings(symbol=symbol, wing_call="NIL", wing_put="NIL")


if __name__ == '__main__':
    print("目前 DTE0_CONFIG_JSON:", load_dte0_config())
