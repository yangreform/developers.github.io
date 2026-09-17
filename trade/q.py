
import os
import datetime
import time
import threading
import calendar
import random
import math
import sys
import json

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import pandas as pd
import requests
from scipy.stats import norm
import scipy.optimize as optimize
from dotenv import load_dotenv, find_dotenv, set_key

from ib_insync import *
import shioaji as sj
from notifier import send_push_message, send_trade_notification
from vxm_config import load_vxm_config, save_vxm_config

# ==============================================================================
# 🔐 Load .env
# ==============================================================================
# 手機面板改的參數要寫回這個檔案，這樣重開 q.py 才不會消失
DOTENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if not os.path.exists(DOTENV_PATH):
    found = find_dotenv()
    if found:
        DOTENV_PATH = found
load_dotenv(DOTENV_PATH, override=True)


def persist_env_var(key: str, value) -> bool:
    """把手機面板改的參數寫回 .env，讓 q.py 重啟後還記得。失敗只印警告，不中斷程式。"""
    try:
        set_key(DOTENV_PATH, key, str(value))
        return True
    except Exception as e:
        print(f"⚠️ 無法寫入 .env ({key}={value}): {e}")
        return False


def env_str(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and (value is None or value == ""):
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value or ""

def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value not in (None, "") else default

def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value not in (None, "") else default


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "on")

# ==============================================================================
# 🛠️ Login and settings
# ==============================================================================
IB_PORT = env_int("IB_PORT", 4001)
IB_HOST = env_str("IB_HOST", "127.0.0.1")
IB_CLIENT_ID = env_int("IB_CLIENT_ID", random.randint(1, 9999))
TARGET_ACCOUNT = env_str("IB_TARGET_ACCOUNT", required=True)
REFRESH_SECONDS = env_int("REFRESH_SECONDS", 300)
IB_GREEKS_WAIT_SECONDS = env_float("IB_GREEKS_WAIT_SECONDS", 1.5)
MIN_FUTURE_DTE = env_int("MIN_FUTURE_DTE", 2)  # 期貨新開倉/對沖最小剩餘到期天數 (預設 2 天，避開 Error 201 實物交割臨期限制)

# 📱 手機監控面板設定
DASHBOARD_PORT = env_int("DASHBOARD_PORT", 5800)
DASHBOARD_PASSWORD = env_str("DASHBOARD_PASSWORD", required=False)  # 留空 = 不驗證密碼（僅建議在區網內使用）

# ------------------------------------------------------------------------------
# 🌟 HEDGE_CONFIG / SEND_WEBHOOK 完全從 .env 讀取
# 手機面板改過的值會整包寫回 .env 的 HEDGE_CONFIG_JSON，重開 q.py 後也不會消失
# ------------------------------------------------------------------------------
HEDGE_COOLDOWN_SECONDS = 60 * 5


def serialize_hedge_config(config: dict) -> str:
    """把 HEDGE_CONFIG 轉成可以寫進 .env 的單行 JSON 字串（symbols 用 list）。"""
    plain = {name: {**info, "symbols": list(info["symbols"])} for name, info in config.items()}
    return json.dumps(plain, ensure_ascii=False)


def load_hedge_config() -> dict:
    """從 .env 的 HEDGE_CONFIG_JSON 讀取設定；若 .env 未設定則拋出錯誤。"""
    raw = os.getenv("HEDGE_CONFIG_JSON", "")
    if not raw:
        raise RuntimeError(
            "❌ .env 缺少 HEDGE_CONFIG_JSON！\n"
            "請在 .env 中加入 HEDGE_CONFIG_JSON='...' 後再啟動。"
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"HEDGE_CONFIG_JSON 格式錯誤，請檢查 .env: {e}")

    # symbols 一定要是 tuple，程式其他地方用 str.startswith(tuple) 判斷群組
    for info in parsed.values():
        info["symbols"] = tuple(info["symbols"])
    return parsed


HEDGE_CONFIG = load_hedge_config()
last_hedge_times = {k: 0 for k in HEDGE_CONFIG.keys()}


# ==============================================================================
# 🇹🇼 台指 TMF / TXO 設定與基差修正 (OP_HEDGE_CONFIG_JSON 即時讀寫)
# ==============================================================================
def get_tmf_op_config() -> dict:
    """從 .env 即時讀取 OP_HEDGE_CONFIG_JSON 中台指(TXO)/TMF 的設定"""
    dotenv_path = find_dotenv()
    if not dotenv_path:
        dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(dotenv_path):
        load_dotenv(dotenv_path, override=True)

    raw = os.getenv("OP_HEDGE_CONFIG_JSON", "")
    default_cfg = {
        "price_diff": 0.0,
        "wing_width": 400,
        "dte_start": 1,
        "dte_end": 50,
        "bid_up": 600,
        "iron": "short",
        "symbols": ["TXO", "TX", "TMF", "MXF"]
    }
    if not raw:
        return default_cfg
    try:
        data = json.loads(raw)
        for key in ["台指(TXO)", "台指(TMF)", "台指", "TXO", "TMF"]:
            if key in data and isinstance(data[key], dict):
                cfg = dict(default_cfg)
                cfg.update(data[key])
                return cfg
        for k, v in data.items():
            if isinstance(v, dict) and any(s in v.get("symbols", []) for s in ["TXO", "TX", "TMF", "MXF"]):
                cfg = dict(default_cfg)
                cfg.update(v)
                return cfg
    except Exception as e:
        print(f"⚠️ 解析 OP_HEDGE_CONFIG_JSON 失敗: {e}")
    return default_cfg


def get_tmf_price_diff() -> float:
    """取得台指選擇權點位修正 (price_diff)"""
    cfg = get_tmf_op_config()
    try:
        return float(cfg.get("price_diff", 0.0))
    except (ValueError, TypeError):
        return 0.0


def save_tmf_op_config(price_diff: float = None, wing_width: int = None) -> tuple[bool, str, dict]:
    """更新 OP_HEDGE_CONFIG_JSON 中的 price_diff 與 wing_width 並寫回 trade/.env"""
    dotenv_path = find_dotenv()
    if not dotenv_path:
        dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

    raw = os.getenv("OP_HEDGE_CONFIG_JSON", "")
    cfg_data = {}
    if raw:
        try:
            cfg_data = json.loads(raw)
        except Exception:
            cfg_data = {}

    target_key = "台指(TXO)"
    for k in ["台指(TXO)", "台指(TMF)", "台指", "TXO", "TMF"]:
        if k in cfg_data:
            target_key = k
            break

    if target_key not in cfg_data:
        cfg_data[target_key] = {
            "symbols": ["TXO", "TX", "TMF", "MXF"],
            "dte_start": 1,
            "dte_end": 50,
            "wing_width": 400,
            "bid_up": 600,
            "iron": "short",
            "price_diff": 0.0
        }

    if price_diff is not None:
        try:
            cfg_data[target_key]["price_diff"] = float(price_diff)
        except (ValueError, TypeError):
            pass
    if wing_width is not None and int(wing_width) > 0:
        try:
            cfg_data[target_key]["wing_width"] = int(wing_width)
        except (ValueError, TypeError):
            pass

    new_raw = json.dumps(cfg_data, ensure_ascii=False)
    os.environ["OP_HEDGE_CONFIG_JSON"] = new_raw

    # 寫回 .env 檔案
    written = False
    if os.path.exists(dotenv_path):
        try:
            lines = []
            with open(dotenv_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            found = False
            new_lines = []
            for line in lines:
                if line.strip().startswith("OP_HEDGE_CONFIG_JSON="):
                    new_lines.append(f"OP_HEDGE_CONFIG_JSON='{new_raw}'\n")
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                new_lines.append(f"OP_HEDGE_CONFIG_JSON='{new_raw}'\n")
            with open(dotenv_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            written = True
        except Exception as e:
            print(f"⚠️ 直接寫入 .env 失敗: {e}")

    if not written:
        persist_env_var("OP_HEDGE_CONFIG_JSON", f"'{new_raw}'")

    return True, "成功更新設定", cfg_data[target_key]


SEND_WEBHOOK = env_bool("SEND_WEBHOOK", False)

WEBHOOK_URL = env_str("WEBHOOK_URL", required=False)
WEBHOOK_PASSPHRASE = env_str("WEBHOOK_PASSPHRASE", required=False)

SHIOAJI_API_KEY = env_str("SHIOAJI_API_KEY", required=False)
SHIOAJI_SECRET_KEY = env_str("SHIOAJI_SECRET_KEY", required=False)
SHIOAJI_CA_PATH = env_str("SHIOAJI_CA_PATH", required=False)
SHIOAJI_CA_PASSWD = env_str("SHIOAJI_CA_PASSWD", required=False)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "LandlordSG", "landlord_sg.db")

'''
WATCH_LIST = [
    {"symbol": "BTC", "secType": "CRYPTO", "exchange": "PAXOS", "currency": "USD", "expiry": "", "decimals": 1},
    {"symbol": "VXM", "secType": "FUT", "exchange": "CFE", "currency": "USD", "expiry": "202608", "decimals": 3},
    {"symbol": "KORU", "secType": "STK", "exchange": "SMART", "currency": "USD", "expiry": "", "decimals": 2},
    {"symbol": "SOXL", "secType": "STK", "exchange": "SMART", "currency": "USD", "expiry": "", "decimals": 2},
]
'''

ib = IB()
api = None

# ==============================================================================
# 📱 手機監控面板 (Flask Dashboard)
# ==============================================================================
from flask import Flask, request, jsonify, Response

dash_app = Flask(__name__)
SNAPSHOT_LOCK = threading.Lock()
LATEST_SNAPSHOT = {
    "updated_at": None,
    "account": {},
    "shioaji": {},
    "orders": [],
    "fills": [],
    "groups": [],
    "send_webhook": SEND_WEBHOOK,
    "note": "尚未取得任何資料，請稍候...",
}


def update_snapshot(new_data: dict) -> None:
    """執行緒安全地更新最新快照，供 Dashboard 讀取。"""
    with SNAPSHOT_LOCK:
        LATEST_SNAPSHOT.clear()
        LATEST_SNAPSHOT.update(new_data)


def get_snapshot() -> dict:
    with SNAPSHOT_LOCK:
        return dict(LATEST_SNAPSHOT)


def check_dashboard_auth(payload: dict) -> bool:
    """若有設定 DASHBOARD_PASSWORD，則要求 payload 內帶正確密碼才能修改參數。"""
    if not DASHBOARD_PASSWORD:
        return True
    return payload.get("password") == DASHBOARD_PASSWORD


DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>選擇權與 Delta 對沖監控面板</title>
<style>
  * { box-sizing: border-box; }
  body {
    background-color: #0d1117;
    color: #c9d1d9;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    margin: 0;
    padding: 16px;
    font-size: 13px;
  }
  .tabs {
    display: flex;
    border-bottom: 1px solid #30363d;
    margin-bottom: 16px;
  }
  .tablinks {
    background-color: transparent;
    border: none;
    outline: none;
    cursor: pointer;
    padding: 10px 18px;
    font-size: 14px;
    font-weight: 600;
    color: #8b949e;
    border-bottom: 2px solid transparent;
    transition: 0.2s;
  }
  .tablinks:hover {
    color: #c9d1d9;
  }
  .tablinks.active {
    color: #58a6ff;
    border-bottom: 2px solid #58a6ff;
  }
  .tabcontent {
    display: none;
  }
  .card {
    background-color: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: 16px;
    margin-bottom: 16px;
  }
  .card h3 {
    margin-top: 0;
    margin-bottom: 12px;
    color: #58a6ff;
    font-size: 15px;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 8px;
    font-size: 12px;
  }
  th, td {
    padding: 6px 10px;
    text-align: left;
    border-bottom: 1px solid #21262d;
    white-space: nowrap;
  }
  th {
    background-color: #21262d;
    color: #8b949e;
    font-weight: 600;
    position: sticky;
    top: 0;
  }
  tr:hover {
    background-color: #1c2128;
  }
  button.action-btn {
    background-color: #238636;
    color: white;
    border: none;
    padding: 5px 10px;
    border-radius: 5px;
    cursor: pointer;
    font-weight: 600;
    font-size: 12px;
  }
  button.action-btn:hover { background-color: #2ea043; }
  button.action-btn:disabled { background-color: #484f58; cursor: not-allowed; }
  button.danger-btn {
    background-color: #da3633;
    color: white;
    border: none;
    padding: 5px 10px;
    border-radius: 5px;
    cursor: pointer;
    font-weight: 600;
    font-size: 12px;
  }
  button.danger-btn:hover { background-color: #f85149; }
  .badge {
    background-color: #1f6feb;
    color: #ffffff;
    padding: 2px 6px;
    border-radius: 4px;
    font-weight: bold;
  }
  .muted { color: #8b949e; }
  .loading { color: #8b949e; font-style: italic; }
  .row { display: flex; justify-content: space-between; margin-bottom: 6px; }
  
  /* 多單 OR 賺錢 (正數)：紅色；空單 OR 賠錢 (負數)：綠色 */
  .pos { color: #f85149 !important; }
  .neg { color: #2ea043 !important; }
  
  .group-title { display: flex; justify-content: space-between; align-items: center; }
  .form-row { display: flex; gap: 8px; margin-top: 12px; }
  .form-row input {
    background: #0d1117;
    border: 1px solid #30363d;
    color: #c9d1d9;
    padding: 6px 10px;
    border-radius: 6px;
    flex: 1;
  }
  .switch-row { display: flex; justify-content: space-between; align-items: center; }
  .switch { position: relative; display: inline-block; width: 44px; height: 24px; }
  .switch input { opacity: 0; width: 0; height: 0; }
  .slider {
    position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0;
    background-color: #30363d; transition: .3s; border-radius: 24px;
  }
  .slider:before {
    position: absolute; content: ""; height: 18px; width: 18px; left: 3px; bottom: 3px;
    background-color: white; transition: .3s; border-radius: 50%;
  }
  input:checked + .slider { background-color: #238636; }
  input:checked + .slider:before { transform: translateX(20px); }
  .toast {
    position: fixed; bottom: 20px; right: 20px; background: #238636; color: white;
    padding: 10px 16px; border-radius: 6px; display: none; z-index: 1000;
  }
  .toast.show { display: block; }
</style>
</head>
<body>

<div class="tabs">
    <button class="tablinks active" onclick="openTab(event, 'overview')">Delta Hedge</button>
    <button class="tablinks" onclick="openTab(event, 'tmf_tab')">台指TMF</button>
    <button class="tablinks" onclick="openTab(event, 'ai_report')">AI 投資建議</button>
</div>

<div id="overview" class="tabcontent" style="display:block;">
  <h1>對沖監控看板</h1>
  <div class="updated" id="updated">載入中...</div>
  <div class="card">
    <div class="switch-row">
      <div>
        <h2 style="margin:0;">自動對沖下單</h2>
        <div class="muted">開啟後，當監控標的 Delta 偏離時將發送通知，並依設定下單。</div>
      </div>
      <label class="switch">
        <input type="checkbox" id="webhookToggle" onchange="toggleWebhook(this.checked)">
        <span class="slider"></span>
      </label>
    </div>
  </div>

  <!-- VXM 波動率期貨對沖監控分區 -->
  <div class="card" id="vxm-section" style="border-left: 4px solid #f0883e;">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
      <h3 style="margin:0; color:#f0883e; display:flex; align-items:center; gap:8px;">
        <span>⚡ VXM 微型波動率期貨對沖監控</span>
        <span id="vxm-badge" class="badge" style="background-color:#238636; font-size:11px;">監控中</span>
      </h3>
      <button class="action-btn" onclick="loadVXMStatus(this)" style="font-size:11px; padding:4px 10px;">🔄 重新整理 VXM</button>
    </div>

    <!-- 結構化指標卡片 -->
    <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap:10px; margin-bottom:16px;">
      <div style="background:#0d1117; padding:10px; border-radius:6px; border:1px solid #21262d;">
        <div class="muted" style="font-size:11px;">標的合約</div>
        <div style="font-size:14px; font-weight:bold; color:#58a6ff;" id="vxm-contract-display">-</div>
        <div class="muted" style="font-size:11px;" id="vxm-expiry-display">-</div>
      </div>
      <div style="background:#0d1117; padding:10px; border-radius:6px; border:1px solid #21262d;">
        <div class="muted" style="font-size:11px;">目前部位</div>
        <div style="font-size:16px; font-weight:bold;" id="vxm-pos-display">-</div>
        <div class="muted" style="font-size:11px;" id="vxm-target-display">-</div>
      </div>
      <div style="background:#0d1117; padding:10px; border-radius:6px; border:1px solid #21262d;">
        <div class="muted" style="font-size:11px;">未平倉損益 (USD)</div>
        <div style="font-size:16px; font-weight:bold;" id="vxm-pnl-display">-</div>
        <div class="muted" style="font-size:11px;">Unrealized PnL</div>
      </div>
      <div style="background:#0d1117; padding:10px; border-radius:6px; border:1px solid #21262d;">
        <div class="muted" style="font-size:11px;">當前策略判斷</div>
        <div style="font-size:12px; font-weight:bold; color:#7ee787;" id="vxm-status-text">載入中...</div>
      </div>
    </div>

    <!-- 條件設定表單 -->
    <div style="border-top:1px solid #21262d; padding-top:12px;">
      <div style="font-weight:600; color:#c9d1d9; margin-bottom:8px; font-size:13px;">⚙️ 未平倉損益下單條件設定 (修改後存入 .env)</div>
      <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap:10px; margin-bottom:12px;">
        <div>
          <label class="muted" style="font-size:11px; display:block; margin-bottom:4px;">1. 當未平倉口數等於 (口)</label>
          <input type="number" id="vxm_cfg_init_pos" step="1" oninput="markVXMDirty()" style="width:100%; background:#0d1117; border:1px solid #30363d; color:#c9d1d9; padding:6px 8px; border-radius:5px;" value="-2">
          <span class="muted" style="font-size:10px;">當未平倉口數 = 此值時才做損益檢查</span>
        </div>
        <div>
          <label class="muted" style="font-size:11px; display:block; margin-bottom:4px;">停利回補損益門檻 (USD)</label>
          <input type="number" id="vxm_cfg_tp_pnl" step="10" oninput="markVXMDirty()" style="width:100%; background:#0d1117; border:1px solid #30363d; color:#c9d1d9; padding:6px 8px; border-radius:5px;" value="200">
          <span class="muted" style="font-size:10px;">損益 > 此金額時加買 1 口</span>
        </div>
        <div>
          <label class="muted" style="font-size:11px; display:block; margin-bottom:4px;">停損門檻 (USD)</label>
          <input type="number" id="vxm_cfg_loss_pnl" step="10" oninput="markVXMDirty()" style="width:100%; background:#0d1117; border:1px solid #30363d; color:#c9d1d9; padding:6px 8px; border-radius:5px;" value="-100">
          <span class="muted" style="font-size:10px;">損益 < 此金額時加賣 1 口</span>
        </div>
      </div>
      <div style="display:flex; justify-content:flex-end; gap:8px;">
        <button class="action-btn" onclick="saveVXMConfig(this)" style="padding:6px 16px;">💾 儲存 VXM 條件設定至 .env</button>
      </div>
    </div>
  </div>
  <div id="groups"></div>
  <div id="account" class="card"></div>
  <div class="toast" id="toast"></div>
</div>

<div id="tmf_tab" class="tabcontent">
  <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; flex-wrap:wrap; gap:10px;">
    <h2 style="margin:0; display:flex; align-items:center; gap:8px;">
      <span>🇹🇼 台指選擇權 (TMF / TXO) 雙向價差與對沖控制</span>
      <span class="badge" style="background-color:#1f6feb; font-size:12px;">Shioaji 永豐 API</span>
    </h2>
    <button class="action-btn" onclick="loadTMFConfig()">🔄 重新整理設定</button>
  </div>

  <!-- TMF 參數即時讀寫卡片 -->
  <div class="card" style="border-left: 4px solid #58a6ff; margin-bottom:16px;">
    <h3 style="margin-top:0; margin-bottom:12px; color:#58a6ff; font-size:15px; display:flex; align-items:center; gap:6px;">
      <span>⚙️ 策略參數即時設定 (即時讀寫 trade/.env 的 OP_HEDGE_CONFIG_JSON)</span>
    </h3>
    
    <!-- 標的指標即時預覽 -->
    <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap:10px; margin-bottom:16px;">
      <div style="background:#0d1117; padding:10px; border-radius:6px; border:1px solid #21262d;">
        <div class="muted" style="font-size:11px;">TMF 期貨現價 (遠月 DTE 25)</div>
        <div style="font-size:16px; font-weight:bold; color:#7ee787;" id="tmf_display_price">載入中...</div>
      </div>
      <div style="background:#0d1117; padding:10px; border-radius:6px; border:1px solid #21262d;">
        <div class="muted" style="font-size:11px;">點位修正 (price_diff)</div>
        <div style="font-size:16px; font-weight:bold; color:#58a6ff;" id="tmf_display_diff">0.0</div>
      </div>
      <div style="background:#0d1117; padding:10px; border-radius:6px; border:1px solid #21262d;">
        <div class="muted" style="font-size:11px;">週選擇權參考價 (現價 + 點位差)</div>
        <div style="font-size:16px; font-weight:bold; color:#d29922;" id="tmf_display_eff_price">-</div>
      </div>
      <div style="background:#0d1117; padding:10px; border-radius:6px; border:1px solid #21262d;">
        <div class="muted" style="font-size:11px;">預估價平 ATM (Step 100)</div>
        <div style="font-size:16px; font-weight:bold; color:#f0883e;" id="tmf_display_atm">-</div>
      </div>
    </div>

    <!-- 設定輸入表單 -->
    <div style="border-top:1px solid #21262d; padding-top:14px;">
      <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap:14px; margin-bottom:14px;">
        <div>
          <label class="muted" style="font-size:12px; display:block; margin-bottom:4px; font-weight:600; color:#c9d1d9;">
            🎯 點位修正 (price_diff, 點數):
          </label>
          <input type="number" id="tmf_cfg_price_diff" step="1" oninput="updateTMFLiveCalc()" style="width:100%; background:#0d1117; border:1px solid #30363d; color:#c9d1d9; padding:8px 10px; border-radius:6px; font-size:14px;" value="0">
          <span class="muted" style="font-size:11px; display:block; margin-top:3px;">
            💡 修正 TMF 活躍期貨 (DTE~25) 與週選 (DTE~3) 基差不同步問題，讓價平與 Delta 計算正確。
          </span>
        </div>
        <div>
          <label class="muted" style="font-size:12px; display:block; margin-bottom:4px; font-weight:600; color:#c9d1d9;">
            📐 價差翼寬 (wing_width, 點數):
          </label>
          <input type="number" id="tmf_cfg_wing_width" step="50" oninput="updateTMFLiveCalc()" style="width:100%; background:#0d1117; border:1px solid #30363d; color:#c9d1d9; padding:8px 10px; border-radius:6px; font-size:14px;" value="400">
          <span class="muted" style="font-size:11px; display:block; margin-top:3px;">
            💡 Bear Call / Bull Put 翅膀距離 (例如 400 點 = 賣 46800 買 47200/46400)。
          </span>
        </div>
      </div>
      <div style="display:flex; justify-content:flex-end; gap:10px;">
        <button class="action-btn" id="btn_save_tmf_cfg" onclick="saveTMFConfig(this)" style="padding:8px 20px; font-size:13px; background-color:#238636;">
          💾 儲存設定至 .env (OP_HEDGE_CONFIG_JSON)
        </button>
      </div>
    </div>
  </div>

  <!-- 模擬下單執行區塊 -->
  <div class="card" style="border-left: 4px solid #238636;">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; flex-wrap:wrap; gap:10px;">
      <h3 style="margin:0; color:#238636; font-size:15px; display:flex; align-items:center; gap:6px;">
        <span>🚀 open_Shioaji.py 雙向價差模擬下單 (--dry-run)</span>
      </h3>
      <button class="action-btn" id="btn_run_open_shioaji" style="background-color:#1f6feb; padding:8px 18px; font-size:13px; display:flex; align-items:center; gap:6px;" onclick="runOpenShioajiDryRun()">
        <span>⚡</span> <span>執行模擬下單並顯示結果</span>
      </button>
    </div>
    <div class="muted" style="font-size:11px; margin-bottom:12px;">
      點擊上方按鈕將在背景執行 <code>trade/open_Shioaji.py --dry-run --no-line</code>，使用當前 <code>price_diff</code> 與 <code>wing_width</code> 試算雙向價差報價、Delta 與保證金，不送出真實委託。
    </div>

    <!-- 執行狀態指示 -->
    <div id="tmf_action_status" style="display:none; padding:10px 14px; border-radius:6px; margin-bottom:12px; font-size:13px; border:1px solid transparent;"></div>

    <!-- 終端機模擬控制台視窗 -->
    <div style="background:#090d13; border:1px solid #30363d; border-radius:6px; overflow:hidden;">
      <div style="background:#161b22; padding:6px 12px; border-bottom:1px solid #30363d; display:flex; justify-content:space-between; align-items:center; font-size:11px; color:#8b949e;">
        <span>🖥️ 終端機模擬日誌 (Console Output)</span>
        <div style="display:flex; gap:8px;">
          <button class="action-btn" onclick="clearTMFConsole()" style="padding:2px 8px; font-size:10px; background:#21262d;">清空日誌</button>
        </div>
      </div>
      <pre id="tmf_console_output" style="margin:0; padding:12px; max-height:450px; overflow-y:auto; font-family:Consolas, Monaco, 'Courier New', monospace; font-size:12px; line-height:1.45; color:#7ee787; white-space:pre-wrap; word-break:break-all;">點擊上方「執行模擬下單並顯示結果」按鈕開始試算...</pre>
    </div>
  </div>
</div>

<div id="ai_report" class="tabcontent">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; flex-wrap:wrap; gap:10px;">
        <h2 style="margin:0; display:flex; align-items:center; gap:8px;">
            <span>🤖 AI 選擇權異常異動投資建議</span>
            <span class="badge" style="background-color:#238636; font-size:12px;">Gemini 深度分析</span>
        </h2>
        <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
            <select id="ai_report_select" onchange="switchAIReport(this.value)" style="background:#0d1117; color:#c9d1d9; border:1px solid #30363d; padding:6px 12px; border-radius:6px; font-size:13px;"></select>
            <button class="action-btn" onclick="loadAIReportList()">🔄 重新整理報告</button>
        </div>
    </div>

    <!-- 腳本執行按鍵區塊 (py barchart_download.py 與 py barchart_analysis.py) -->
    <div style="display:flex; gap:12px; margin-bottom:14px; flex-wrap:wrap; align-items:center; background:#161b22; padding:12px 16px; border-radius:8px; border:1px solid #30363d;">
        <span style="font-size:13px; font-weight:600; color:#8b949e;">⚡ 腳本操作：</span>
        <button class="action-btn" id="btn_run_download" style="background-color:#1f6feb; display:flex; align-items:center; gap:6px; padding:7px 14px; font-size:13px;" onclick="runBarchartDownload()">
            <span>📥</span> <span>執行 barchart_download.py</span>
        </button>
        <button class="action-btn" id="btn_run_analysis" style="background-color:#238636; display:flex; align-items:center; gap:6px; padding:7px 14px; font-size:13px;" onclick="runBarchartAnalysis()">
            <span>🧠</span> <span>執行 barchart_analysis.py</span>
        </button>
    </div>

    <div id="ai_action_status" style="display:none; padding:12px 16px; border-radius:8px; margin-bottom:14px; font-size:13px; border:1px solid transparent;"></div>

    <div class="card" style="padding:16px;">
        <div id="ai_report_meta" style="margin-bottom:12px; font-size:12px; color:#8b949e; display:flex; justify-content:space-between;"></div>
        <div id="ai_report_content" style="white-space:pre-wrap; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif; line-height:1.7; font-size:14px; color:#e6edf3; background:#0d1117; padding:18px; border-radius:8px; border:1px solid #30363d; overflow-x:auto;">載入中...</div>
    </div>
</div>

<script>
const PASSWORD_KEY = "dashboard_pw";

function fmt(n, d=2) {
  if (n === null || n === undefined || isNaN(n)) return "-";
  return Number(n).toLocaleString(undefined, {minimumFractionDigits:d, maximumFractionDigits:d});
}
function cls(n) { return Number(n) > 0 ? "pos" : (Number(n) < 0 ? "neg" : ""); }

function calcDTE(exp, dte) {
  if (dte !== undefined && dte !== null && dte !== "") {
    if (typeof dte === 'number') return Math.round(dte);
    const parsed = parseInt(dte, 10);
    if (!isNaN(parsed)) return parsed;
  }
  if (!exp) return "-";
  if (typeof exp === 'number') return Math.round(exp);
  const s = String(exp).replace(/[-\/]/g, '').trim();
  if (/^\d{8}$/.test(s)) {
    const y = parseInt(s.substring(0, 4), 10);
    const m = parseInt(s.substring(4, 6), 10) - 1;
    const d = parseInt(s.substring(6, 8), 10);
    const expDate = new Date(y, m, d);
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const diffDays = Math.round((expDate - today) / (1000 * 60 * 60 * 24));
    return diffDays;
  }
  if (/^\d{6}$/.test(s)) {
    const y = parseInt(s.substring(0, 4), 10);
    const m = parseInt(s.substring(4, 6), 10) - 1;
    const expDate = new Date(y, m, 1);
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const diffDays = Math.round((expDate - today) / (1000 * 60 * 60 * 24));
    return diffDays;
  }
  const parsed = parseInt(s, 10);
  return isNaN(parsed) ? "-" : parsed;
}

function showToast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2000);
}

async function submitThreshold(groupName) {
  const upperEl = document.getElementById("u_" + groupName);
  const lowerEl = document.getElementById("l_" + groupName);
  let pw = localStorage.getItem(PASSWORD_KEY) || "";
  const body = { group: groupName, upper: upperEl.value, lower: lowerEl.value, password: pw };
  try {
    const resp = await fetch("/api/threshold", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body)
    });
    const data = await resp.json();
    if (resp.status === 401) {
      pw = prompt("請輸入管理密碼:") || "";
      localStorage.setItem(PASSWORD_KEY, pw);
      return submitThreshold(groupName);
    }
    if (data.status === "ok") {
      showToast("已更新 " + groupName);
    } else {
      showToast("錯誤: " + (data.message || "更新失敗"));
    }
  } catch (e) {
    showToast("連線失敗");
  }
}

let webhookToggleBusy = false;
async function toggleWebhook(enabled) {
  if (webhookToggleBusy) return;
  webhookToggleBusy = true;
  const toggleEl = document.getElementById("webhookToggle");
  let pw = localStorage.getItem(PASSWORD_KEY) || "";
  try {
    const resp = await fetch("/api/send_webhook", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ enabled: enabled, password: pw })
    });
    if (resp.status === 401) {
      pw = prompt("請輸入管理密碼:") || "";
      localStorage.setItem(PASSWORD_KEY, pw);
      webhookToggleBusy = false;
      return toggleWebhook(enabled);
    }
    const data = await resp.json();
    if (data.status === "ok") {
      showToast(data.send_webhook ? "自動對沖下單已啟用" : "自動對沖下單已停用");
    } else {
      toggleEl.checked = !enabled;
      showToast("錯誤: " + (data.message || "更新失敗"));
    }
  } catch (e) {
    toggleEl.checked = !enabled;
    showToast("連線失敗");
  } finally {
    webhookToggleBusy = false;
  }
}

function renderPositions(rows) {
  if (!rows || !rows.length) return "";
  let html = '<table><tr><th>代號</th><th>部位</th><th>現價</th><th>損益</th><th>Delta</th><th>Theta</th><th>DTE</th></tr>';
  for (const r of rows) {
    const dteVal = calcDTE(r.expiry, r.dte);
    html += `<tr>
      <td>${r.symbol}</td>
      <td class="${cls(r.position)}" style="font-weight:bold;">${fmt(r.position,1)}</td>
      <td>${fmt(r.market_price, r.decimals ?? 2)}</td>
      <td class="${cls(r.pnl)}" style="font-weight:bold;">${fmt(r.pnl,2)}</td>
      <td>${fmt(r.delta,4)}</td>
      <td>${fmt(r.theta,0)}</td>
      <td>${dteVal}</td>
    </tr>`;
  }
  html += "</table>";
  return html;
}

function renderGroup(g) {
  if (g.name === '未分類(Other)' && g.positions && g.positions.length > 1) {
    g.positions.sort((a, b) => {
      const da = calcDTE(a.expiry, a.dte);
      const db = calcDTE(b.expiry, b.dte);
      const numA = (typeof da === 'number') ? da : 999999;
      const numB = (typeof db === 'number') ? db : 999999;
      if (numA !== numB) return numA - numB;
      return (a.symbol || '').localeCompare(b.symbol || '');
    });
  }
  const totalPnl = (g.total_pnl !== undefined && g.total_pnl !== null) ? g.total_pnl : (g.positions ? g.positions.reduce((acc, p) => acc + (Number(p.pnl) || 0), 0) : 0);
  return `
  <div class="card">
    <div class="group-title">
      <h2>${g.name}</h2>
      <span class="badge">${g.hedge_sym}</span>
    </div>
    ${g.closed ? '<div class="muted">部位已平倉</div>' : ''}
    ${g.mute
      ? `<div class="row"><span class="muted">資料不足，暫停計算</span></div>`
      : `<div class="row"><span>Delta: ${fmt(g.total_delta,3)}</span><span>Theta: ${fmt(g.total_theta,0)}</span><span>未平倉損益: <span class="${cls(totalPnl)}">${fmt(totalPnl,2)}</span></span></div>`
    }
    ${renderPositions(g.positions)}
    <div class="form-row">
      <input type="number" step="0.1" id="u_${g.name}" placeholder="上限 (目前 ${fmt(g.upper_threshold,2)})">
      <input type="number" step="0.1" id="l_${g.name}" placeholder="下限 (目前 ${fmt(g.lower_threshold,2)})">
      <button class="action-btn" onclick="submitThreshold('${g.name}')">送出</button>
    </div>
  </div>`;
}

async function refresh() {
  try {
    const resp = await fetch("/api/snapshot");
    const data = await resp.json();
    document.getElementById("updated").textContent = "最後更新: " + (data.updated_at || "-");

    const toggleEl = document.getElementById("webhookToggle");
    if (!webhookToggleBusy && data.send_webhook !== undefined) {
      toggleEl.checked = !!data.send_webhook;
    }

    const acc = data.account || {};
    const shioaji = data.shioaji || {};
    let accHtml = "<h2>帳戶概況</h2>";
    accHtml += `<div class="row"><span>IB 淨值:</span><span>${acc.net_liq ?? "-"}</span></div>`;
    accHtml += `<div class="row"><span>IB 可用資金:</span><span>${acc.avail ?? "-"}</span></div>`;
    accHtml += `<div class="row"><span>所有部位 Theta 總計:</span><span>${fmt(acc.total_theta, 0)}</span></div>`;
    if (shioaji && shioaji.equity !== undefined) {
      accHtml += `<div class="row"><span>永豐保證金淨值:</span><span>${fmt(shioaji.equity,0)}</span></div>`;
      accHtml += `<div class="row"><span>永豐可用保證金:</span><span>${fmt(shioaji.available,0)}</span></div>`;
    }
    document.getElementById("account").innerHTML = accHtml;

    let groupsHtml = "";
    for (const g of (data.groups || [])) {
      groupsHtml += renderGroup(g);
    }
    document.getElementById("groups").innerHTML = groupsHtml || '<div class="card">目前無群組資料</div>';
    if (data.vxm) {
      updateVXMUI(data.vxm);
    } else {
      loadVXMStatus();
    }
  } catch (e) {
    document.getElementById("updated").textContent = "讀取失敗，重試中...";
  }
}

refresh();
setInterval(refresh, 15000);

function openTab(evt, tabName) {
    var i, tabcontent, tablinks;
    tabcontent = document.getElementsByClassName("tabcontent");
    for (i = 0; i < tabcontent.length; i++) {
        tabcontent[i].style.display = "none";
    }
    tablinks = document.getElementsByClassName("tablinks");
    for (i = 0; i < tablinks.length; i++) {
        tablinks[i].className = tablinks[i].className.replace(" active", "");
    }
    document.getElementById(tabName).style.display = "block";
    if(evt) evt.currentTarget.className += " active";
    
    if(tabName === 'tmf_tab') {
        loadTMFConfig();
    } else if(tabName === 'barchart') {
        loadBarchartData();
    } else if(tabName === 'ai_report') {
        loadAIReportList();
    }
}

// ==========================================
// 台指 TMF Tab Functions
// ==========================================
let LATEST_TMF_RAW_PRICE = null;

async function loadTMFConfig() {
    try {
        const resp = await fetch('/api/tmf/config?t=' + Date.now());
        const data = await resp.json();
        if (data.status === 'ok') {
            document.getElementById('tmf_cfg_price_diff').value = (data.price_diff !== undefined && data.price_diff !== null) ? data.price_diff : 0;
            document.getElementById('tmf_cfg_wing_width').value = (data.wing_width !== undefined && data.wing_width !== null) ? data.wing_width : 400;
            if (data.tmf_price !== null && data.tmf_price !== undefined && data.tmf_price > 0) {
                LATEST_TMF_RAW_PRICE = parseFloat(data.tmf_price);
                document.getElementById('tmf_display_price').textContent = LATEST_TMF_RAW_PRICE.toFixed(2);
            } else {
                document.getElementById('tmf_display_price').textContent = '待連線報價';
            }
            updateTMFLiveCalc();
        }
    } catch (e) {
        console.error("載入 TMF 設定失敗:", e);
    }
}

function updateTMFLiveCalc() {
    const diffInput = parseFloat(document.getElementById('tmf_cfg_price_diff').value) || 0.0;
    document.getElementById('tmf_display_diff').textContent = (diffInput >= 0 ? '+' : '') + diffInput.toFixed(1);
    
    if (LATEST_TMF_RAW_PRICE !== null && !isNaN(LATEST_TMF_RAW_PRICE) && LATEST_TMF_RAW_PRICE > 0) {
        const effPrice = LATEST_TMF_RAW_PRICE + diffInput;
        const atm = Math.round(effPrice / 100.0) * 100;
        document.getElementById('tmf_display_eff_price').textContent = effPrice.toFixed(2);
        document.getElementById('tmf_display_atm').textContent = atm.toFixed(0);
    } else {
        document.getElementById('tmf_display_eff_price').textContent = '-';
        document.getElementById('tmf_display_atm').textContent = '-';
    }
}

async function saveTMFConfig(btn) {
    const origText = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span>💾</span> <span>儲存中...</span>';
    
    const priceDiff = parseFloat(document.getElementById('tmf_cfg_price_diff').value) || 0.0;
    const wingWidth = parseInt(document.getElementById('tmf_cfg_wing_width').value, 10) || 400;
    
    try {
        const resp = await fetch('/api/tmf/save_config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                price_diff: priceDiff,
                wing_width: wingWidth
            })
        });
        const res = await resp.json();
        btn.disabled = false;
        btn.innerHTML = origText;
        if (res.status === 'ok') {
            showToast("✅ 台指 TMF 設定已成功儲存至 .env！");
            loadTMFConfig();
        } else {
            alert("❌ 儲存失敗: " + (res.message || "未知錯誤"));
        }
    } catch (e) {
        btn.disabled = false;
        btn.innerHTML = origText;
        alert("❌ 儲存連線失敗: " + e);
    }
}

async function runOpenShioajiDryRun() {
    const btn = document.getElementById('btn_run_open_shioaji');
    const statusBox = document.getElementById('tmf_action_status');
    const consoleBox = document.getElementById('tmf_console_output');
    
    btn.disabled = true;
    const origHtml = btn.innerHTML;
    btn.innerHTML = '<span>⏳</span> <span>模擬下單執行中 (約 3~6 秒)...</span>';
    
    statusBox.style.display = 'block';
    statusBox.style.background = '#161b22';
    statusBox.style.color = '#58a6ff';
    statusBox.style.borderColor = '#1f6feb';
    statusBox.innerHTML = '⏳ 正在背景執行 <code>trade/open_Shioaji.py --dry-run --no-line</code>，計算雙向價差報價與 Delta，請稍候...';
    
    consoleBox.textContent = `[${new Date().toLocaleTimeString()}] 🚀 正在啟動 open_Shioaji.py 模擬試算...\n`;
    
    try {
        const resp = await fetch('/api/tmf/run_open_shioaji', { method: 'POST' });
        const res = await resp.json();
        btn.disabled = false;
        btn.innerHTML = origHtml;
        
        if (res.status === 'ok') {
            statusBox.style.background = '#13231b';
            statusBox.style.color = '#3fb950';
            statusBox.style.borderColor = '#238636';
            statusBox.innerHTML = `✅ 模擬下單執行完成！(${new Date().toLocaleTimeString()})`;
            consoleBox.textContent = res.output || "（無輸出日誌）";
            consoleBox.scrollTop = consoleBox.scrollHeight;
        } else {
            statusBox.style.background = '#27171a';
            statusBox.style.color = '#f85149';
            statusBox.style.borderColor = '#da3633';
            statusBox.innerHTML = `❌ 執行失敗: ${res.message || '未知錯誤'}`;
            consoleBox.textContent = (res.output || res.message || "（無輸出）");
        }
    } catch (err) {
        btn.disabled = false;
        btn.innerHTML = origHtml;
        statusBox.style.background = '#27171a';
        statusBox.style.color = '#f85149';
        statusBox.style.borderColor = '#da3633';
        statusBox.innerHTML = `❌ 連線或執行異常: ${err}`;
        consoleBox.textContent += `\n❌ 錯誤: ${err}`;
    }
}

function clearTMFConsole() {
    document.getElementById('tmf_console_output').textContent = '日誌已清空。點擊「執行模擬下單並顯示結果」開始試算...';
}

// ==========================================
// AI Report dynamic loader
// ==========================================
async function loadAIReportList() {
    const sel = document.getElementById('ai_report_select');
    const content = document.getElementById('ai_report_content');
    const meta = document.getElementById('ai_report_meta');
    content.innerHTML = '<div class="loading">正在載入 AI 投資建議報告...</div>';
    try {
        const response = await fetch('/api/ai_reports?t=' + Date.now());
        const data = await response.json();
        if (data.status !== 'ok' || !data.reports || data.reports.length === 0) {
            content.innerHTML = '<p class="muted">尚未找到任何 AI 投資建議文字報告。<br>請先執行 <code>python barchart_analysis.py</code> 產出最新分析。</p>';
            sel.innerHTML = '<option value="">(無報告)</option>';
            meta.innerHTML = '';
            return;
        }
        sel.innerHTML = data.reports.map(r => `<option value="${r.filename}">${r.label}</option>`).join('');
        if (data.latest) {
            renderAIReportContent(data.latest);
        }
    } catch(err) {
        content.innerHTML = `<span style="color:#f85149;">載入失敗: ${err}</span>`;
    }
}

async function switchAIReport(filename) {
    if (!filename) return;
    const content = document.getElementById('ai_report_content');
    content.innerHTML = '<div class="loading">正在載入所選報告...</div>';
    try {
        const response = await fetch('/api/ai_reports/content?filename=' + encodeURIComponent(filename) + '&t=' + Date.now());
        const data = await response.json();
        if (data.status === 'ok') {
            renderAIReportContent(data);
        } else {
            content.innerHTML = `<span style="color:#f85149;">讀取錯誤: ${data.message}</span>`;
        }
    } catch(err) {
        content.innerHTML = `<span style="color:#f85149;">讀取失敗: ${err}</span>`;
    }
}

function renderAIReportContent(data) {
    const content = document.getElementById('ai_report_content');
    const meta = document.getElementById('ai_report_meta');
    meta.innerHTML = `<span>📄 檔案名稱：<strong>${data.filename}</strong></span><span>🕒 產出時間：${data.mtime || '-'}</span>`;
    content.textContent = data.content || '(空白報告)';
}

// ==========================================
// Barchart Runner Functions
// ==========================================
async function runBarchartDownload() {
    const btn = document.getElementById('btn_run_download');
    const statusBox = document.getElementById('ai_action_status');
    btn.disabled = true;
    const origHtml = btn.innerHTML;
    btn.innerHTML = '<span>⏳</span> <span>下載執行中 (約需 15~30 秒)...</span>';
    
    statusBox.style.display = 'block';
    statusBox.style.background = '#161b22';
    statusBox.style.color = '#58a6ff';
    statusBox.style.borderColor = '#1f6feb';
    statusBox.innerHTML = '⏳ 正在背景執行 <code>py barchart_download.py</code>，瀏覽器自動連線 Barchart 下載個股與 ETF 異常期權 CSV，請稍候...';

    try {
        const resp = await fetch('/api/barchart/run_download', { method: 'POST' });
        const res = await resp.json();
        if (res.status === 'ok') {
            statusBox.style.background = '#13231b';
            statusBox.style.color = '#3fb950';
            statusBox.style.borderColor = '#238636';
            statusBox.innerHTML = `✅ <strong>barchart_download.py 執行成功！</strong> 最新期權異動 CSV 已下載完成！`;
        } else {
            statusBox.style.background = '#2c1517';
            statusBox.style.color = '#f85149';
            statusBox.style.borderColor = '#da3633';
            statusBox.innerHTML = `❌ <strong>執行失敗：</strong> ${res.message || '未知錯誤'}`;
        }
    } catch (e) {
        statusBox.style.background = '#2c1517';
        statusBox.style.color = '#f85149';
        statusBox.style.borderColor = '#da3633';
        statusBox.innerHTML = `❌ <strong>連線逾時或失敗：</strong> ${e}`;
    } finally {
        btn.disabled = false;
        btn.innerHTML = origHtml;
    }
}

async function runBarchartAnalysis() {
    const btn = document.getElementById('btn_run_analysis');
    const statusBox = document.getElementById('ai_action_status');
    btn.disabled = true;
    const origHtml = btn.innerHTML;
    btn.innerHTML = '<span>⏳</span> <span>AI 分析與推播中 (約需 10~20 秒)...</span>';
    
    statusBox.style.display = 'block';
    statusBox.style.background = '#161b22';
    statusBox.style.color = '#58a6ff';
    statusBox.style.borderColor = '#1f6feb';
    statusBox.innerHTML = '⏳ 正在背景執行 <code>py barchart_analysis.py</code>，透過 Gemini 篩選優質期權合約並發送 LINE 推播...';

    try {
        const resp = await fetch('/api/barchart/run_analysis', { method: 'POST' });
        const res = await resp.json();
        if (res.status === 'ok') {
            statusBox.style.background = '#13231b';
            statusBox.style.color = '#3fb950';
            statusBox.style.borderColor = '#238636';
            statusBox.innerHTML = `✅ <strong>barchart_analysis.py 執行成功！</strong> AI 報告已存檔並完成 LINE 手機推播！`;
            // 自動刷新報表列表以載入剛出爐的最新報告
            loadAIReportList();
        } else {
            statusBox.style.background = '#2c1517';
            statusBox.style.color = '#f85149';
            statusBox.style.borderColor = '#da3633';
            statusBox.innerHTML = `❌ <strong>執行失敗：</strong> ${res.message || '未知錯誤'}`;
        }
    } catch (e) {
        statusBox.style.background = '#2c1517';
        statusBox.style.color = '#f85149';
        statusBox.style.borderColor = '#da3633';
        statusBox.innerHTML = `❌ <strong>連線逾時或失敗：</strong> ${e}`;
    } finally {
        btn.disabled = false;
        btn.innerHTML = origHtml;
    }
}

// ==========================================
// Barchart Multi-table dynamic loader & quote
// ==========================================
async function loadBarchartData() {
    const container = document.getElementById('barchart_content');
    container.innerHTML = '<div class="loading">正在讀取所有 Barchart CSV 檔案...</div>';
    try {
        const response = await fetch('/api/option/barchart_tables?t=' + Date.now());
        const res = await response.json();
        if (res.status === 'error') {
            container.innerHTML = `<span style="color:#f85149;">錯誤: ${res.message}</span>`;
            return;
        }
        const tables = res.tables || [];
        if (!tables.length) {
            container.innerHTML = '<p class="muted">在 Barchart 目錄下未找到任何 CSV 檔案。</p>';
            return;
        }

        window.BARCHART_TABLES = tables;
        let fullHtml = '';
        tables.forEach((t, tIdx) => {
            fullHtml += `<div class="card" style="margin-bottom:24px;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                    <h3 style="margin:0;">${t.title} <span class="badge" style="background-color:#30363d; font-size:11px; font-weight:normal;">${t.filename} (${t.rows.length} 筆)</span></h3>
                </div>
                <div style="overflow-x:auto; max-height:480px;">
                    <table>
                        <thead>
                            <tr>`;
            
            // Replicate exact columns from the CSV
            t.columns.forEach(col => {
                fullHtml += `<th>${col}</th>`;
            });
            fullHtml += `<th>操作 (Action)</th></tr></thead><tbody>`;

            t.rows.forEach((row, rIdx) => {
                fullHtml += `<tr>`;
                t.columns.forEach(col => {
                    let val = row[col];
                    if (val === null || val === undefined) val = "";
                    let clsName = "";
                    if (col.includes('Profit') || col.includes('Gain') || col.includes('%TP')) {
                        clsName = "pos";
                    } else if (col.includes('Loss')) {
                        clsName = "neg";
                    } else if (col === 'DTE' || col === 'Exp Date') {
                        val = calcDTE(row['Exp Date'], row['DTE']);
                    }
                    fullHtml += `<td class="${clsName}">${val}</td>`;
                });

                // Check option type (Call or Put)
                let optType = (row.Type || row.type || '').toString().trim();
                let isCall = optType.toLowerCase().includes('call');
                let isPut = optType.toLowerCase().includes('put');

                // Render Action Button
                if (t.strategy === 'bull_put') {
                    fullHtml += `<td><button class="action-btn" id="btn_${tIdx}_${rIdx}" onclick="onBullPutAction(this, ${tIdx}, ${rIdx})">下單 (Bull Put)</button></td>`;
                } else if (t.strategy === 'long_call') {
                    fullHtml += `<td><button class="action-btn" style="background-color:#1f6feb;" id="btn_${tIdx}_${rIdx}" onclick="onLongCallAction(this, ${tIdx}, ${rIdx})">下單 (Buy Call)</button></td>`;
                } else if (t.strategy === 'short_strangle' || t.strategy === 'iron_condor') {
                    fullHtml += `<td><button class="action-btn" style="background-color:#9e6a03;" id="btn_${tIdx}_${rIdx}" onclick="onShortStrangleAction(this, ${tIdx}, ${rIdx})">下單 (雙賣)</button></td>`;
                } else {
                    // Single Option / UOA table: default to Limit Bid Call / Put
                    let btnColor = isPut ? '#da3633' : '#1f6feb';
                    let btnText = isCall ? '下單 (Limit Bid Call)' : (isPut ? '下單 (Limit Bid Put)' : '下單 (Limit Bid)');
                    fullHtml += `<td><button class="action-btn" style="background-color:${btnColor}; font-weight:600;" id="btn_${tIdx}_${rIdx}" onclick="onSingleOptionAction(this, ${tIdx}, ${rIdx})">${btnText}</button></td>`;
                }

                fullHtml += `</tr>`;
            });

            fullHtml += `</tbody></table></div></div>`;
        });

        container.innerHTML = fullHtml;
    } catch (err) {
        container.innerHTML = `<span style="color:#f85149;">讀取失敗: ${err}</span>`;
    }
}

async function onBullPutAction(btn, tIdx, rIdx) {
    const row = (typeof tIdx === 'number' && window.BARCHART_TABLES && window.BARCHART_TABLES[tIdx])
        ? window.BARCHART_TABLES[tIdx].rows[rIdx]
        : (tIdx && typeof tIdx === 'object' ? tIdx : rIdx);
    if (!row) {
        alert("無法讀取此筆 Bull Put Spread 資料！");
        return;
    }

    const origText = btn.innerText;
    btn.disabled = true;
    btn.innerText = "查詢報價中...";

    const symbol = row.Symbol || row.symbol || '';
    const expDate = row['Exp Date'] || row['Expiration Date'] || row.exp_date || '';
    const leg1Strike = row['Leg1 Strike'] || row['Leg 1 Strike'] || row['Short Put Strike'] || row['Sell Put Strike'] || row.leg1_strike || 0;
    const leg2Strike = row['Leg2 Strike'] || row['Leg 2 Strike'] || row['Long Put Strike'] || row['Buy Put Strike'] || row.leg2_strike || 0;
    const maxProfit = row['Max Profit'] || row['Credit'] || row.max_profit || 0;

    try {
        const resp = await fetch('/api/option/barchart_quote', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                strategy: 'bull_put',
                symbol: symbol,
                exp_date: expDate,
                leg1_strike: leg1Strike,
                leg2_strike: leg2Strike,
                max_profit: maxProfit
            })
        });
        const q = await resp.json();
        btn.disabled = false;
        btn.innerText = origText;

        if (q.status !== 'ok') {
            alert("取得報價失敗: " + (q.message || "未知錯誤"));
            return;
        }

        const msg = `【下單詢問視窗 - Bull Put Spread】\n\n` +
                    `標的: ${q.symbol}\n` +
                    `到期日: ${q.exp_date}\n` +
                    `Sell Put: ${q.leg1_strike}\n` +
                    `Buy Put: ${q.leg2_strike}\n\n` +
                    `IBKR 即時報價:\n` +
                    `- Bid: ${q.bid !== null ? q.bid : 'N/A'}\n` +
                    `- Ask: ${q.ask !== null ? q.ask : 'N/A'}\n\n` +
                    `預計下單內容:\n` +
                    `- 動作: SELL 1口 (限價單)\n` +
                    `- 限價價格: $${q.limit_price}\n\n` +
                    `確定要送出此筆 Bull Put Spread 下單嗎？`;

        if (!confirm(msg)) return;

        // Execute trade
        btn.disabled = true;
        btn.innerText = "送出委託中...";
        const trResp = await fetch('/api/option/execute_barchart_trade', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                strategy: 'bull_put',
                symbol: q.symbol,
                action: 'SELL',
                limit_price: q.limit_price,
                leg1_conId: q.leg1_conId,
                leg2_conId: q.leg2_conId
            })
        });
        const trData = await trResp.json();
        btn.disabled = false;
        btn.innerText = origText;
        alert(trData.message || (trData.status === 'ok' ? '下單成功！' : '下單失敗'));
    } catch (e) {
        btn.disabled = false;
        btn.innerText = origText;
        alert("下單連線失敗: " + e);
    }
}

async function onShortStrangleAction(btn, tIdx, rIdx) {
    const row = (typeof tIdx === 'number' && window.BARCHART_TABLES && window.BARCHART_TABLES[tIdx])
        ? window.BARCHART_TABLES[tIdx].rows[rIdx]
        : (tIdx && typeof tIdx === 'object' ? tIdx : rIdx);
    if (!row) {
        alert("無法讀取此筆雙賣資料！");
        return;
    }

    const origText = btn.innerText;
    btn.disabled = true;
    btn.innerText = "查詢報價中...";

    const symbol = row.Symbol || row.symbol || '';
    const expDate = row['Exp Date'] || row['Expiration Date'] || row.exp_date || row['Expiry'] || '';
    // 取得同位置的 Short Put 與 Short Call 履約價
    let shortPut = row['Short Put Strike'] || row['Short Put'] || row['Sell Put Strike'] || row['Sell Put'] || row['Leg2 Strike'] || row['Leg1 Strike'] || row['Put Strike'] || row['SP'] || 0;
    let shortCall = row['Short Call Strike'] || row['Short Call'] || row['Sell Call Strike'] || row['Sell Call'] || row['Leg3 Strike'] || row['Leg2 Strike'] || row['Call Strike'] || row['SC'] || 0;
    let maxProfit = row['Max Profit'] || row['Credit'] || row['Midpoint'] || 0;

    try {
        const resp = await fetch('/api/option/barchart_quote', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                strategy: 'short_strangle',
                symbol: symbol,
                exp_date: expDate,
                short_put_strike: shortPut,
                short_call_strike: shortCall,
                max_profit: maxProfit
            })
        });
        const q = await resp.json();
        btn.disabled = false;
        btn.innerText = origText;

        if (q.status !== 'ok') {
            alert("取得報價失敗: " + (q.message || "未知錯誤"));
            return;
        }

        const msg = `【下單詢問視窗 - 雙賣 (Short Strangle)】\n\n` +
                    `標的: ${q.symbol}\n` +
                    `到期日: ${q.exp_date}\n` +
                    `Sell Put (履約價): ${q.short_put_strike}\n` +
                    `Sell Call (履約價): ${q.short_call_strike}\n\n` +
                    `IBKR 即時報價:\n` +
                    `- Bid: ${q.bid !== null ? q.bid : 'N/A'}\n` +
                    `- Ask: ${q.ask !== null ? q.ask : 'N/A'}\n\n` +
                    `預計下單內容:\n` +
                    `- 動作: SELL 1口 (雙賣限價單)\n` +
                    `- 限價價格: $${q.limit_price}\n\n` +
                    `確定要送出此筆雙賣下單嗎？`;

        if (!confirm(msg)) return;

        // Execute trade
        btn.disabled = true;
        btn.innerText = "送出委託中...";
        const trResp = await fetch('/api/option/execute_barchart_trade', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                strategy: 'short_strangle',
                symbol: q.symbol,
                action: 'SELL',
                limit_price: q.limit_price,
                put_conId: q.put_conId,
                call_conId: q.call_conId
            })
        });
        const trData = await trResp.json();
        btn.disabled = false;
        btn.innerText = origText;
        alert(trData.message || (trData.status === 'ok' ? '下單成功！' : '下單失敗'));
    } catch (e) {
        btn.disabled = false;
        btn.innerText = origText;
        alert("下單連線失敗: " + e);
    }
}

async function onLongCallAction(btn, tIdx, rIdx) {
    const row = (typeof tIdx === 'number' && window.BARCHART_TABLES && window.BARCHART_TABLES[tIdx])
        ? window.BARCHART_TABLES[tIdx].rows[rIdx]
        : (tIdx && typeof tIdx === 'object' ? tIdx : rIdx);
    if (!row) {
        alert("無法讀取此筆期權合約資料！");
        return;
    }

    const origText = btn.innerText;
    btn.disabled = true;
    btn.innerText = "查詢報價中...";

    const symbol = row.Symbol || row.symbol || '';
    const expDate = row['Exp Date'] || row['Expiration Date'] || row.exp_date || '';
    const strike = row.Strike || row.strike || 0;
    const bid = row.Bid || row.bid || 0;
    const ask = row.Ask || row.ask || 0;

    try {
        const resp = await fetch('/api/option/barchart_quote', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                strategy: 'long_call',
                symbol: symbol,
                exp_date: expDate,
                strike: strike,
                bid: bid,
                ask: ask
            })
        });
        const q = await resp.json();
        btn.disabled = false;
        btn.innerText = origText;

        if (q.status !== 'ok') {
            alert("取得報價失敗: " + (q.message || "未知錯誤"));
            return;
        }

        const msg = `【下單詢問視窗 - Long Call】\n\n` +
                    `標的: ${q.symbol}\n` +
                    `到期日: ${q.exp_date}\n` +
                    `Buy Call: ${q.strike}\n\n` +
                    `IBKR 即時報價:\n` +
                    `- Bid (買方出價): ${q.bid !== null ? q.bid : 'N/A'}\n` +
                    `- Ask (賣方要價): ${q.ask !== null ? q.ask : 'N/A'}\n\n` +
                    `預計下單內容:\n` +
                    `- 動作: BUY 1口 (限價單)\n` +
                    `- 限價價格: $${q.limit_price} (Bid 限價)\n\n` +
                    `確定要送出此筆 Buy Call 下單嗎？`;

        if (!confirm(msg)) return;

        // Execute trade
        btn.disabled = true;
        btn.innerText = "送出委託中...";
        const trResp = await fetch('/api/option/execute_barchart_trade', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                strategy: 'long_call',
                symbol: q.symbol,
                action: 'BUY',
                limit_price: q.limit_price,
                conId: q.conId
            })
        });
        const trData = await trResp.json();
        btn.disabled = false;
        btn.innerText = origText;
        alert(trData.message || (trData.status === 'ok' ? '下單成功！' : '下單失敗'));
    } catch (e) {
        btn.disabled = false;
        btn.innerText = origText;
        alert("下單連線失敗: " + e);
    }
}

async function onSingleOptionAction(btn, tIdx, rIdx) {
    const row = (typeof tIdx === 'number' && window.BARCHART_TABLES && window.BARCHART_TABLES[tIdx]) ? window.BARCHART_TABLES[tIdx].rows[rIdx] : tIdx;
    if (!row) {
        alert("無法讀取此筆期權合約資料！");
        return;
    }
    const origText = btn.innerText;
    btn.disabled = true;
    btn.innerText = "查詢報價中...";

    const optType = (row.Type || row.type || 'Call').toString().trim();
    const symbol = row.Symbol || row.symbol || '';
    const expDate = row['Exp Date'] || row['Expiration Date'] || row.exp_date || '';
    const strike = row.Strike || row.strike || 0;
    const csvBid = row.Bid || row.bid || 0;
    const csvAsk = row.Ask || row.ask || 0;
    const latest = row.Latest || row.lastPrice || row.price || 0;

    try {
        const resp = await fetch('/api/option/barchart_quote', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                strategy: 'single_option',
                symbol: symbol,
                exp_date: expDate,
                strike: strike,
                type: optType,
                bid: csvBid,
                ask: csvAsk,
                latest: latest
            })
        });
        const q = await resp.json();
        btn.disabled = false;
        btn.innerText = origText;

        if (q.status !== 'ok') {
            alert("取得報價失敗: " + (q.message || "未知錯誤"));
            return;
        }

        const confirmMsg = `【下單詢問視窗 - 選擇權限價買單 (Limit Bid)】\n\n` +
                          `標的代號: ${q.symbol} (${q.type})\n` +
                          `到期日期: ${q.exp_date}\n` +
                          `履約價格: $${q.strike}\n\n` +
                          `IBKR 即時報價:\n` +
                          `- Bid (買方出價): $${q.bid !== null ? q.bid : 'N/A'}\n` +
                          `- Ask (賣方要價): $${q.ask !== null ? q.ask : 'N/A'}\n\n` +
                          `預計委託內容:\n` +
                          `- 委託動作: BUY 1口\n` +
                          `- 委託種類: 限價單 (Limit Order)\n` +
                          `- 限價價格: $${q.limit_price} (Limit Bid)\n\n` +
                          `請確認限價價格 (直接按確定送出，或手動調整金額)：`;

        const userPrice = prompt(confirmMsg, q.limit_price);
        if (userPrice === null) return; // 使用者按取消

        const finalPrice = parseFloat(userPrice);
        if (isNaN(finalPrice) || finalPrice <= 0) {
            alert("請輸入有效的限價金額！");
            return;
        }

        // 送出委託
        btn.disabled = true;
        btn.innerText = "送出委託中...";
        const trResp = await fetch('/api/option/execute_barchart_trade', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                strategy: 'single_option',
                symbol: q.symbol,
                action: 'BUY',
                limit_price: finalPrice,
                conId: q.conId,
                opt_type: q.type
            })
        });
        const trData = await trResp.json();
        btn.disabled = false;
        btn.innerText = origText;
        alert(trData.message || (trData.status === 'ok' ? '下單成功！已送出委託。' : '下單失敗'));
    } catch (e) {
        btn.disabled = false;
        btn.innerText = origText;
        alert("下單連線失敗: " + e);
    }
}

function onGenericAction(btn, row) {
    onSingleOptionAction(btn, row);
}

let vxmFormDirty = false;

function markVXMDirty() {
  vxmFormDirty = true;
}

function updateVXMUI(d) {
  if (!d) return;
  const box = document.getElementById("vxm-console-box");
  if (box && d.terminal_output) {
    box.textContent = d.terminal_output;
  }
  const cDisp = document.getElementById("vxm-contract-display");
  if (cDisp) cDisp.textContent = d.trading_contract || d.local_symbol || "-";
  const eDisp = document.getElementById("vxm-expiry-display");
  if (eDisp) eDisp.textContent = `到期: ${d.expiry || '-'} (conId: ${d.con_id || '-'})`;
  const pDisp = document.getElementById("vxm-pos-display");
  if (pDisp) {
    pDisp.textContent = `${d.current_pos} 口`;
    pDisp.className = d.current_pos < 0 ? "pos" : (d.current_pos > 0 ? "neg" : "");
  }
  const tDisp = document.getElementById("vxm-target-display");
  if (tDisp && d.config) tDisp.textContent = `條件: ${d.config.target_init_pos} 口`;
  const pnlDisp = document.getElementById("vxm-pnl-display");
  if (pnlDisp && d.unrealized_pnl !== undefined) {
    const pnlVal = Number(d.unrealized_pnl);
    const pnlSign = pnlVal > 0 ? "+" : "";
    pnlDisp.textContent = `${pnlSign}${pnlVal.toFixed(2)} USD`;
    pnlDisp.className = pnlVal > 0 ? "pos" : (pnlVal < 0 ? "neg" : "");
  }
  const sText = document.getElementById("vxm-status-text");
  if (sText) {
    sText.textContent = d.status_line || "正常監控中";
    sText.style.color = d.action ? "#f85149" : "#7ee787";
  }

  if (d.config && !vxmFormDirty) {
    if (document.getElementById("vxm_cfg_init_pos")) document.getElementById("vxm_cfg_init_pos").value = d.config.target_init_pos ?? -2;
    if (document.getElementById("vxm_cfg_tp_pnl")) document.getElementById("vxm_cfg_tp_pnl").value = d.config.tp_pnl ?? 200;
    if (document.getElementById("vxm_cfg_loss_pnl")) document.getElementById("vxm_cfg_loss_pnl").value = d.config.loss_pnl ?? -100;
  }
}

async function loadVXMStatus(btn) {
  let origText = "";
  if (btn) {
    origText = btn.innerText;
    btn.innerText = "查詢中...";
    btn.disabled = true;
  }
  try {
    const res = await fetch('/api/vxm/status');
    const d = await res.json();
    if (d.status === 'ok') {
      updateVXMUI(d);
      if (btn) showToast("VXM 狀態已重新整理");
    }
  } catch (e) {
    console.error("載入 VXM 狀態失敗:", e);
  } finally {
    if (btn) {
      btn.innerText = origText;
      btn.disabled = false;
    }
  }
}

async function saveVXMConfig(btn) {
  const origText = btn.innerText;
  btn.disabled = true;
  btn.innerText = "儲存中...";
  const pw = localStorage.getItem(PASSWORD_KEY) || "";

  const payload = {
    password: pw,
    target_init_pos: parseInt(document.getElementById("vxm_cfg_init_pos").value, 10),
    tp_pnl: parseFloat(document.getElementById("vxm_cfg_tp_pnl").value),
    loss_pnl: parseFloat(document.getElementById("vxm_cfg_loss_pnl").value)
  };

  try {
    const res = await fetch('/api/vxm/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    const d = await res.json();
    btn.disabled = false;
    btn.innerText = origText;

    if (d.status === 'ok') {
      showToast(d.message || "VXM 策略設定已成功寫入 .env！");
      vxmFormDirty = false;
      loadVXMStatus();
    } else {
      alert("儲存失敗: " + (d.message || "未知錯誤"));
    }
  } catch (e) {
    btn.disabled = false;
    btn.innerText = origText;
    alert("儲存連線失敗: " + e);
  }
}
</script>
</body>
</html>
"""

# ==============================================================================
# 🌟 全期貨商品動態尋找最近可用合約 (自動滾倉/到期檢測)
# ==============================================================================
FUTURE_EXCHANGE_MAP = {
    "MNQ": "CME", "NQ": "CME",
    "MES": "CME", "ES": "CME",
    "M2K": "CME", "RTY": "CME",
    "M6E": "CME", "EUR": "CME",
    "MJY": "CME", "JPY": "CME",
    "MBT": "CME", "BTC": "CME",
    "MCL": "NYMEX", "CL": "NYMEX",
    "MHNG": "NYMEX", "MNG": "NYMEX", "NG": "NYMEX", "LN": "NYMEX",
    "MHG": "COMEX", "HG": "COMEX",
    "MGC": "COMEX", "GC": "COMEX",
    "YC": "CBOT", "XC": "CBOT", "ZC": "CBOT",
    "VXM": "CFE",
}

FUTURE_SYMBOL_ALIAS = {
    "XC": "YC",
    "MNG": "MHNG",
}

FUTURE_CONTRACT_CACHE = {}


def is_unexpired(exp_str: str, today_str: str, min_days: int = 2) -> bool:
    """
    檢查期貨合約是否具備足夠的到期天數 (DTE) 以供新開倉/對沖交易：
    1. 必須具備足夠的剩餘天數 (DTE >= min_days，預設 2 天)，避免在到期當日或前 1-2 天新開倉。
    2. 避開 IBKR 實物交割 (Physical Delivery) 與臨期平倉 (Near-Expiration) 風控限制 (Error 201)。
    3. 當合約 DTE < min_days 時，判定為不可下單合約，促使系統自動往後換月 (Rollover)。
    """
    if not exp_str:
        return False
    clean_exp = str(exp_str).strip()
    try:
        if len(clean_exp) >= 8:
            exp_date = datetime.date(int(clean_exp[:4]), int(clean_exp[4:6]), int(clean_exp[6:8]))
        elif len(clean_exp) == 6:
            year = int(clean_exp[:4])
            month = int(clean_exp[4:6])
            last_day = calendar.monthrange(year, month)[1]
            exp_date = datetime.date(year, month, last_day)
        else:
            return False

        if len(today_str) >= 8:
            today_date = datetime.date(int(today_str[:4]), int(today_str[4:6]), int(today_str[6:8]))
        else:
            today_date = datetime.date.today()

        dte = (exp_date - today_date).days
        return dte >= min_days
    except Exception:
        if len(clean_exp) >= 8:
            return clean_exp[:8] > today_str
        return clean_exp > today_str


def get_target_future_contract(ib_instance: IB, symbol: str, min_days_to_expiry: int | None = None):
    """
    自動搜尋並回傳該商品最近可以下單的正確合法期貨合約。
    支援自動過濾已到期與臨期合約 (DTE >= min_days_to_expiry，預設 2 天，避開 IBKR Error 201 實物交割與臨期風控限制)、
    按到期日排序取最近可交易月，並自動進行合約資格化 (qualifyContracts)。
    """
    if not ib_instance or not ib_instance.isConnected():
        return None

    if min_days_to_expiry is None:
        min_days_to_expiry = MIN_FUTURE_DTE

    actual_symbol = FUTURE_SYMBOL_ALIAS.get(symbol.upper(), symbol.upper())
    exchange = FUTURE_EXCHANGE_MAP.get(actual_symbol, "CME")
    today_str = datetime.date.today().strftime('%Y%m%d')
    now_ts = time.time()

    cache_key = f"{actual_symbol}_{exchange}"
    cached = FUTURE_CONTRACT_CACHE.get(cache_key)
    if cached:
        contract, cached_time = cached
        if (now_ts - cached_time < 1800) and is_unexpired(contract.lastTradeDateOrContractMonth, today_str, min_days=min_days_to_expiry):
            return contract

    details = []
    try:
        details = ib_instance.reqContractDetails(Future(symbol=actual_symbol, exchange=exchange, currency='USD'))
    except Exception:
        pass

    if not details:
        try:
            details = ib_instance.reqContractDetails(Future(symbol=actual_symbol, exchange=exchange))
        except Exception:
            pass

    if not details:
        try:
            details = ib_instance.reqContractDetails(Future(symbol=actual_symbol))
        except Exception:
            pass

    valid_details = [
        d for d in details
        if d.contract.exchange not in ['QBALGO', 'SMART']
        and is_unexpired(d.contract.lastTradeDateOrContractMonth, today_str, min_days=min_days_to_expiry)
    ]

    if not valid_details:
        print(f"⚠️ [期貨合約搜尋] 找不到 {symbol} ({actual_symbol} @ {exchange}) 的可用近月合約！")
        return None

    # 按到期月份升冪排序，取得最接近可下單的有效合約
    valid_details = sorted(valid_details, key=lambda d: d.contract.lastTradeDateOrContractMonth)
    target_contract = valid_details[0].contract
    try:
        ib_instance.qualifyContracts(target_contract)
    except Exception as q_err:
        print(f"⚠️ [期貨合約資格化異常] {target_contract.symbol}: {q_err}")

    FUTURE_CONTRACT_CACHE[cache_key] = (target_contract, now_ts)
    return target_contract


VXM_CACHE = {
    "target_contract": None,
    "last_contract_check": 0,
    "last_trade_time": 0
}
VXM_CURRENT_DATA = {}


def get_target_vxm_contract(ib_instance):
    return get_target_future_contract(ib_instance, 'VXM')


def compute_vxm_evaluation(current_pos, unrealized_pnl, target_contract_symbol, expiry, con_id, cfg):
    target_init_pos = int(cfg.get("target_init_pos", -2))
    tp_pnl = float(cfg.get("tp_pnl", 200.0))
    loss_pnl = float(cfg.get("loss_pnl", -100.0))

    action = None
    qty = 0
    decision_reason = ""

    # 當未平倉口數 == target_init_pos 時，才執行停利/停損損益檢查
    if current_pos == target_init_pos:
        if unrealized_pnl < loss_pnl:
            action = 'SELL'
            qty = 1
            decision_reason = f"未平倉口數 ({current_pos}) 符合條件 ({target_init_pos}) 且未平倉損益 ({unrealized_pnl:.2f}) < 停損門檻 ({loss_pnl:.2f})，加賣 1 口"
        elif unrealized_pnl > tp_pnl:
            action = 'BUY'
            qty = 1
            decision_reason = f"未平倉口數 ({current_pos}) 符合條件 ({target_init_pos}) 且未平倉損益 ({unrealized_pnl:.2f}) > 停利回補損益門檻 ({tp_pnl:.2f})，加買 1 口"
        else:
            status_line = f"未平倉口數 ({current_pos}) 符合條件 ({target_init_pos})，損益 ({unrealized_pnl:.2f}) 介於門檻間，無需調整"
    else:
        status_line = f"未平倉口數 ({current_pos}) != 設定條件 ({target_init_pos})，不觸發停利/停損檢查"

    if decision_reason:
        status_line = decision_reason

    terminal_output = (
        f"================ 開始處理 VXM 策略 ================\n"
        f"-> 鎖定最近期可交易 VXM: {target_contract_symbol} (到期: {expiry}, conId: {con_id})\n"
        f"[VXM 狀態] 目前部位: {current_pos} 口 (條件: {target_init_pos}) | 未平倉損益: {unrealized_pnl:.2f} USD | 交易合約: {target_contract_symbol}\n"
        f"-> [VXM] {status_line}"
    )

    return {
        "status": "ok",
        "terminal_output": terminal_output,
        "status_line": status_line,
        "action": action,
        "qty": qty,
        "trading_contract": target_contract_symbol,
        "local_symbol": target_contract_symbol,
        "expiry": expiry,
        "con_id": con_id,
        "current_pos": current_pos,
        "unrealized_pnl": round(unrealized_pnl, 2),
        "config": cfg
    }


def evaluate_and_run_vxm(ib_instance, execute_order=True):
    global VXM_CURRENT_DATA
    if not ib_instance or not ib_instance.isConnected():
        return VXM_CURRENT_DATA

    target_contract = get_target_vxm_contract(ib_instance)
    local_symbol = target_contract.localSymbol if target_contract else "VXMU6"
    expiry = target_contract.lastTradeDateOrContractMonth if target_contract else "20260916"
    con_id = target_contract.conId if target_contract else 866999756

    # 取得目前所有 VXM 部位與損益
    vxm_positions = [p for p in ib_instance.portfolio() if p.contract.symbol == 'VXM']
    current_pos = 0
    total_unrealized_pnl = 0.0
    held_contract = None

    for p in vxm_positions:
        current_pos += int(p.position)
        if p.unrealizedPNL is not None:
            total_unrealized_pnl += p.unrealizedPNL
        if p.position != 0:
            held_contract = p.contract

    trade_contract = held_contract if held_contract else target_contract
    if trade_contract and hasattr(trade_contract, 'conId') and trade_contract.conId:
        try:
            ib_instance.qualifyContracts(trade_contract)
        except Exception:
            pass

    trading_contract_name = trade_contract.localSymbol if trade_contract and trade_contract.localSymbol else local_symbol

    cfg = load_vxm_config()
    target_init_pos = int(cfg.get("target_init_pos", -2))
    tp_pnl = float(cfg.get("tp_pnl", 200.0))
    loss_pnl = float(cfg.get("loss_pnl", -100.0))

    trade_action = None
    trade_qty = 0
    reason = ""

    # 當未平倉口數 == target_init_pos 時，才執行停利/停損條件檢查
    if current_pos == target_init_pos:
        if total_unrealized_pnl < loss_pnl:
            trade_action = 'SELL'
            trade_qty = 1
            reason = f"未平倉口數 ({current_pos}) 符合條件 ({target_init_pos}) 且未平倉損益 ({total_unrealized_pnl:.2f}) < 停損門檻 ({loss_pnl:.2f})，加賣 1 口"
        elif total_unrealized_pnl > tp_pnl:
            trade_action = 'BUY'
            trade_qty = 1
            reason = f"未平倉口數 ({current_pos}) 符合條件 ({target_init_pos}) 且未平倉損益 ({total_unrealized_pnl:.2f}) > 停利回補損益門檻 ({tp_pnl:.2f})，加買 1 口"
        else:
            status_line = f"未平倉口數 ({current_pos}) 符合條件 ({target_init_pos})，損益 ({total_unrealized_pnl:.2f}) 介於門檻間，無需調整"
    else:
        status_line = f"未平倉口數 ({current_pos}) != 設定條件 ({target_init_pos})，不觸發停利/停損檢查"

    if reason:
        status_line = reason

    terminal_output = (
        f"================ 開始處理 VXM 策略 ================\n"
        f"-> 鎖定最近期可交易 VXM: {local_symbol} (到期: {expiry}, conId: {con_id})\n"
        f"[VXM 狀態] 目前部位: {current_pos} 口 (條件: {target_init_pos}) | 未平倉損益: {total_unrealized_pnl:.2f} USD | 交易合約: {trading_contract_name}\n"
        f"-> [VXM] {status_line}"
    )

    print(terminal_output)

    now_ts = time.time()
    if execute_order and trade_action and trade_qty > 0:
        if now_ts - VXM_CACHE.get("last_trade_time", 0) > 60:
            if SEND_WEBHOOK:
                from ib_insync import TagValue
                order = MarketOrder(trade_action, trade_qty)
                order.tif = 'DAY'
                order.algoStrategy = 'Adaptive'
                order.algoParams = [TagValue('adaptivePriority', 'Patient')]
                if TARGET_ACCOUNT:
                    order.account = TARGET_ACCOUNT
                trade = ib_instance.placeOrder(trade_contract, order)
                VXM_CACHE["last_trade_time"] = now_ts
                print(f"🚀 [VXM 下單成功] 已送出 {trade_action} {trade_qty} 口 {trade_contract.localSymbol}，原因: {reason}")
                
                try:
                    payload_dict = {
                        "strategy": "VXM_HEDGE",
                        "symbol": "VXM",
                        "contract": trade_contract.localSymbol,
                        "action": trade_action,
                        "quantity": trade_qty,
                        "current_pos": current_pos,
                        "target_init_pos": target_init_pos,
                        "unrealized_pnl": total_unrealized_pnl,
                        "reason": reason
                    }
                    send_trade_notification(
                        symbol="VXM",
                        message=f"已執行 VXM 策略下單: {trade_action} {trade_qty} 口 {trade_contract.localSymbol}\n原因: {reason}\n未平倉損益: {total_unrealized_pnl:.2f} USD",
                        payload_data=payload_dict
                    )
                except Exception as ex:
                    print(f"⚠️ VXM LINE 推播發送失敗: {ex}")
            else:
                print(f"-> [VXM 守門] 觸發下單條件 ({reason})，但自動對沖下單開關未開啟 (SEND_WEBHOOK=False)，跳過下單。")
        else:
            print(f"-> [VXM 守門] 冷卻中 (距離上次下單未滿 60 秒)，暫不重複送單。")

    VXM_CURRENT_DATA = {
        "status": "ok",
        "terminal_output": terminal_output,
        "status_line": status_line,
        "action": trade_action,
        "qty": trade_qty,
        "trading_contract": trading_contract_name,
        "local_symbol": local_symbol,
        "expiry": expiry,
        "con_id": con_id,
        "current_pos": current_pos,
        "unrealized_pnl": round(total_unrealized_pnl, 2),
        "config": cfg,
        "updated_at": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    return VXM_CURRENT_DATA


def get_vxm_status_data():
    global VXM_CURRENT_DATA
    cfg = load_vxm_config()
    if VXM_CURRENT_DATA:
        current_pos = VXM_CURRENT_DATA.get("current_pos", -2)
        unrealized_pnl = VXM_CURRENT_DATA.get("unrealized_pnl", 0.0)
        target_sym = VXM_CURRENT_DATA.get("local_symbol", "VXMU6")
        expiry = VXM_CURRENT_DATA.get("expiry", "20260916")
        con_id = VXM_CURRENT_DATA.get("con_id", 866999756)
        trading_sym = VXM_CURRENT_DATA.get("trading_contract", target_sym)
        
        eval_res = compute_vxm_evaluation(current_pos, unrealized_pnl, target_sym, expiry, con_id, cfg)
        eval_res["trading_contract"] = trading_sym
        eval_res["updated_at"] = VXM_CURRENT_DATA.get("updated_at", "")
        return eval_res

    if ib and ib.isConnected():
        try:
            return evaluate_and_run_vxm(ib, execute_order=False)
        except Exception:
            pass

    return compute_vxm_evaluation(-2, 0.0, "VXMU6", "20260916", 866999756, cfg)


@dash_app.route("/dashboard", methods=["GET"])
def dashboard_page():
    return Response(DASHBOARD_HTML, mimetype="text/html")


@dash_app.route("/api/snapshot", methods=["GET"])
def api_snapshot():
    return jsonify(get_snapshot())


@dash_app.route("/api/vxm/status", methods=["GET"])
def api_vxm_status():
    return jsonify(get_vxm_status_data())


@dash_app.route("/api/vxm/config", methods=["POST"])
def api_vxm_config():
    payload = request.get_json(silent=True) or {}
    if not check_dashboard_auth(payload):
        return jsonify({"status": "error", "message": "密碼錯誤"}), 401
    
    try:
        new_cfg = {
            "target_init_pos": int(payload.get("target_init_pos", 2)),
            "tp_pnl": float(payload.get("tp_pnl", 400.0)),
            "loss_pnl": float(payload.get("loss_pnl", payload.get("step_losses", {}).get("2", -400.0)))
        }
    except (ValueError, TypeError) as e:
        return jsonify({"status": "error", "message": f"參數格式錯誤: {e}"}), 400

    ok, result = save_vxm_config(new_cfg)
    if ok:
        print(f"[手機面板] VXM 策略參數已儲存至 .env: {new_cfg}")
        updated_status = get_vxm_status_data()
        return jsonify({"status": "ok", "message": "VXM 策略設定已成功寫入 .env", "data": updated_status})
    else:
        return jsonify({"status": "error", "message": f"寫入 .env 失敗: {result}"}), 500


@dash_app.route("/api/threshold", methods=["POST"])
def api_threshold():
    payload = request.get_json(silent=True) or {}

    if not check_dashboard_auth(payload):
        return jsonify({"status": "error", "message": "密碼錯誤"}), 401

    group = payload.get("group")
    if group not in HEDGE_CONFIG:
        return jsonify({"status": "error", "message": f"找不到群組: {group}"}), 400

    upper_raw = payload.get("upper")
    lower_raw = payload.get("lower")

    try:
        if upper_raw not in (None, ""):
            HEDGE_CONFIG[group]["upper_threshold"] = float(upper_raw)
        if lower_raw not in (None, ""):
            HEDGE_CONFIG[group]["lower_threshold"] = float(lower_raw)
    except ValueError:
        return jsonify({"status": "error", "message": "上限/下限必須是數字"}), 400

    persist_env_var("HEDGE_CONFIG_JSON", serialize_hedge_config(HEDGE_CONFIG))

    print(
        f"📱 [手機面板] {group} 門檻已更新並寫入 .env："
        f"上限={HEDGE_CONFIG[group]['upper_threshold']}, "
        f"下限={HEDGE_CONFIG[group]['lower_threshold']}"
    )

    return jsonify({
        "status": "ok",
        "group": group,
        "upper_threshold": HEDGE_CONFIG[group]["upper_threshold"],
        "lower_threshold": HEDGE_CONFIG[group]["lower_threshold"],
    })


@dash_app.route("/api/send_webhook", methods=["POST"])
def api_send_webhook():
    global SEND_WEBHOOK
    payload = request.get_json(silent=True) or {}

    if not check_dashboard_auth(payload):
        return jsonify({"status": "error", "message": "密碼錯誤"}), 401

    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"status": "error", "message": "enabled 必須是 true/false"}), 400

    SEND_WEBHOOK = enabled
    persist_env_var("SEND_WEBHOOK", "true" if enabled else "false")
    print(f"📱 [手機面板] 自動對沖送單 (SEND_WEBHOOK) 已切換為: {SEND_WEBHOOK}，已寫入 .env")

    return jsonify({"status": "ok", "send_webhook": SEND_WEBHOOK})



BARCHART_DIR = r"C:\Users\Administrator\Desktop\docker_mc\developers.github.io\trade\Barchart"
LATEST_TMF_PRICE = 0.0
TXO_OPTIONS_BY_DATE = {}

def clean_num(val):
    if pd.isna(val): return 0.0
    s = str(val).replace(',', '').replace('$', '').replace('%', '').strip()
    try:
        return float(s)
    except:
        return 0.0

@dash_app.route('/api/option/portfolio', methods=['GET'])
def get_dash_portfolio():
    snap = get_snapshot()
    raw_positions = snap.get('options_positions', [])
    positions = []
    for p in raw_positions:
        pos = p.get('position', 0)
        action = 'BUY' if pos < 0 else 'SELL'
        positions.append({
            'conId': p.get('conId', getattr(p.get('contract'), 'conId', 0)),
            'symbol': p.get('symbol', ''),
            'localSymbol': p.get('localSymbol', ''),
            'position': pos,
            'marketPrice': p.get('marketPrice', 0.0),
            'action': action
        })
    return jsonify({'status': 'ok', 'positions': positions})

@dash_app.route('/api/option/close', methods=['POST'])
def close_dash_position():
    payload = request.get_json(silent=True) or {}
    conId = payload.get('conId')
    action = payload.get('action')
    qty = payload.get('quantity')
    
    if not conId or not action or not qty:
        return jsonify({'status': 'error', 'message': 'Missing parameters.'})

    import random
    from ib_insync import IB, Contract, MarketOrder, TagValue
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    local_ib = IB()
    local_ib.reqPositionsAsync = lambda: asyncio.sleep(0)
    local_ib.reqAccountUpdatesAsync = lambda acct: asyncio.sleep(0)
    local_ib.reqAccountUpdatesMultiAsync = lambda acct: asyncio.sleep(0)
    local_ib.reqOpenOrdersAsync = lambda: asyncio.sleep(0)
    local_ib.reqCompletedOrdersAsync = lambda apiOnly: asyncio.sleep(0)
    local_ib.reqExecutionsAsync = lambda: asyncio.sleep(0)
    try:
        local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(8000, 8999), timeout=10)
        contract = Contract(conId=int(conId))
        local_ib.qualifyContracts(contract)
        
        order = MarketOrder(action, float(qty))
        order.tif = 'DAY'
        order.algoStrategy = 'Adaptive'
        order.algoParams = [TagValue('adaptivePriority', 'Patient')]
        if TARGET_ACCOUNT:
            order.account = TARGET_ACCOUNT
        
        local_ib.placeOrder(contract, order)
        local_ib.sleep(1)
        res_msg = f'已送出平倉單: {action} {qty} (conId: {conId})'
        try:
            send_trade_notification('OPTION', res_msg, {'action': action, 'qty': qty, 'conId': conId})
        except Exception:
            pass
        return jsonify({'status': 'ok', 'message': res_msg})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})
    finally:
        try:
            local_ib.disconnect()
        except Exception:
            pass

@dash_app.route('/api/option/barchart_tables', methods=['GET'])
def get_barchart_tables():
    import glob, re
    try:
        csv_files = glob.glob(os.path.join(BARCHART_DIR, "*.csv"))
        tables = []
        for f in sorted(csv_files):
            fname = os.path.basename(f)
            df = pd.read_csv(f)
            if 'Exp Date' in df.columns:
                df = df.dropna(subset=['Exp Date'])
            else:
                df = df.dropna(how='all')
            
            # Clean Symbol if footer
            if 'Symbol' in df.columns:
                df = df[~df['Symbol'].astype(str).str.contains('Barchart|Page', case=False, na=False)]

            cols_lower = [str(c).lower().strip() for c in df.columns]
            m_profile = re.search(r'screener-(.*?)-\d{2}-\d{2}-\d{4}', fname)
            profile_name = m_profile.group(1).upper() if m_profile else fname.replace('.csv', '')

            if 'unusual' in fname.lower() or ('type' in cols_lower and 'strike' in cols_lower):
                strategy = 'single_option'
                cat_name = "個股" if "stock" in fname.lower() else ("ETF" if "etf" in fname.lower() else "")
                title = f"異常選擇權異動 ({cat_name} UOA - {profile_name})"
            elif 'condor' in fname.lower() or any('condor' in c for c in cols_lower) or any('short call' in c and 'short put' in c for c in cols_lower) or any('leg3' in c or 'leg4' in c for c in cols_lower) or 'strangle' in fname.lower():
                strategy = 'short_strangle'
                title = f"雙賣 (Short Strangle) ({profile_name})"
            elif any('leg1 strike' in c for c in cols_lower):
                strategy = 'bull_put'
                title = f"Bull Put Spread ({profile_name})"
            elif any('call' in c for c in cols_lower) or 'call' in fname.lower():
                strategy = 'long_call'
                title = f"Long Call ({profile_name})"
            else:
                strategy = 'single_option'
                title = f"選擇權策略 ({profile_name})"

            tables.append({
                'filename': fname,
                'title': title,
                'strategy': strategy,
                'columns': list(df.columns),
                'rows': df.fillna('').to_dict(orient='records')
            })
        return jsonify({'status': 'ok', 'tables': tables})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

BARCHART_REPORTS_DIR = os.path.join(BARCHART_DIR, "reports")
os.makedirs(BARCHART_REPORTS_DIR, exist_ok=True)

@dash_app.route('/api/ai_reports', methods=['GET'])
def get_ai_reports():
    import glob, datetime
    try:
        txt_files = glob.glob(os.path.join(BARCHART_REPORTS_DIR, "*.txt")) + glob.glob(os.path.join(BARCHART_DIR, "ai_*.txt"))
        # Exclude internal temporary files if any
        txt_files = list(set([f for f in txt_files if os.path.isfile(f)]))
        txt_files.sort(key=lambda x: os.path.getmtime(x), reverse=True)

        reports = []
        for f in txt_files:
            fname = os.path.basename(f)
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(f)).strftime("%Y-%m-%d %H:%M:%S")
            label = f"{fname} ({mtime})"
            reports.append({
                "filename": fname,
                "label": label,
                "mtime": mtime,
                "size": os.path.getsize(f)
            })

        latest_data = None
        if txt_files:
            latest_file = txt_files[0]
            with open(latest_file, "r", encoding="utf-8", errors="ignore") as fp:
                latest_content = fp.read()
            latest_data = {
                "filename": os.path.basename(latest_file),
                "content": latest_content,
                "mtime": datetime.datetime.fromtimestamp(os.path.getmtime(latest_file)).strftime("%Y-%m-%d %H:%M:%S")
            }

        return jsonify({
            "status": "ok",
            "reports": reports,
            "latest": latest_data
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@dash_app.route('/api/ai_reports/content', methods=['GET'])
def get_ai_report_content():
    import datetime
    filename = request.args.get('filename', '').strip()
    if not filename:
        return jsonify({"status": "error", "message": "Missing filename"})
    filename = os.path.basename(filename)
    target_path = os.path.join(BARCHART_REPORTS_DIR, filename)
    if not os.path.exists(target_path):
        target_path = os.path.join(BARCHART_DIR, filename)

    if not os.path.exists(target_path):
        return jsonify({"status": "error", "message": "Report file not found"})

    try:
        with open(target_path, "r", encoding="utf-8", errors="ignore") as fp:
            content = fp.read()
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(target_path)).strftime("%Y-%m-%d %H:%M:%S")
        return jsonify({
            "status": "ok",
            "filename": filename,
            "content": content,
            "mtime": mtime
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

BARCHART_TASK_LOCK = threading.Lock()

@dash_app.route('/api/barchart/run_download', methods=['POST'])
def api_run_barchart_download():
    import subprocess, sys
    if not BARCHART_TASK_LOCK.acquire(blocking=False):
        return jsonify({"status": "error", "message": "目前已有背景工作正在執行中，請稍候完成再試。"})

    try:
        script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "barchart_download.py")
        if not os.path.exists(script_path):
            return jsonify({"status": "error", "message": f"找不到腳本: {script_path}"})

        proc = subprocess.run(
            [sys.executable, script_path],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            text=True,
            timeout=180,
            encoding="utf-8",
            errors="replace"
        )
        if proc.returncode == 0:
            return jsonify({
                "status": "ok",
                "message": "Barchart CSV 下載完成！",
                "output": proc.stdout[-300:] if proc.stdout else ""
            })
        else:
            err_msg = proc.stderr.strip() if proc.stderr else proc.stdout.strip()
            return jsonify({
                "status": "error",
                "message": err_msg[-300:] if err_msg else f"指令退出代碼: {proc.returncode}"
            })
    except subprocess.TimeoutExpired:
        return jsonify({"status": "error", "message": "下載執行逾時 (超過 180 秒)"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})
    finally:
        BARCHART_TASK_LOCK.release()


@dash_app.route('/api/barchart/run_analysis', methods=['POST'])
def api_run_barchart_analysis():
    import subprocess, sys
    if not BARCHART_TASK_LOCK.acquire(blocking=False):
        return jsonify({"status": "error", "message": "目前已有背景工作正在執行中，請稍候完成再試。"})

    try:
        script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "barchart_analysis.py")
        if not os.path.exists(script_path):
            return jsonify({"status": "error", "message": f"找不到腳本: {script_path}"})

        proc = subprocess.run(
            [sys.executable, script_path],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            text=True,
            timeout=120,
            encoding="utf-8",
            errors="replace"
        )
        if proc.returncode == 0:
            return jsonify({
                "status": "ok",
                "message": "AI 分析完成並已推播 LINE！",
                "output": proc.stdout[-300:] if proc.stdout else ""
            })
        else:
            err_msg = proc.stderr.strip() if proc.stderr else proc.stdout.strip()
            return jsonify({
                "status": "error",
                "message": err_msg[-300:] if err_msg else f"指令退出代碼: {proc.returncode}"
            })
    except subprocess.TimeoutExpired:
        return jsonify({"status": "error", "message": "分析執行逾時 (超過 120 秒)"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})
    finally:
        BARCHART_TASK_LOCK.release()

@dash_app.route('/api/option/barchart_quote', methods=['POST'])
def quote_barchart_trade():
    payload = request.get_json(silent=True) or {}
    strategy = payload.get('strategy')
    symbol = (payload.get('symbol') or '').strip().upper()
    raw_exp = str(payload.get('exp_date', '')).strip()
    exp_date = raw_exp.replace('-', '').replace('/', '')
    if len(exp_date) != 8 or not exp_date.isdigit():
        for fmt in ("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
            try:
                exp_date = datetime.datetime.strptime(raw_exp, fmt).strftime("%Y%m%d")
                break
            except ValueError:
                pass
    
    if not symbol or not exp_date:
        return jsonify({'status': 'error', 'message': f'缺少標的代號或到期日 (symbol: {symbol}, exp_date: {raw_exp})'})

    import random, asyncio, math
    from ib_insync import IB, Option, Contract, ComboLeg
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    local_ib = IB()
    local_ib.reqPositionsAsync = lambda: asyncio.sleep(0)
    local_ib.reqAccountUpdatesAsync = lambda acct: asyncio.sleep(0)
    local_ib.reqAccountUpdatesMultiAsync = lambda acct: asyncio.sleep(0)
    local_ib.reqOpenOrdersAsync = lambda: asyncio.sleep(0)
    local_ib.reqCompletedOrdersAsync = lambda apiOnly: asyncio.sleep(0)
    local_ib.reqExecutionsAsync = lambda: asyncio.sleep(0)

    try:
        local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(5000, 6999), timeout=10, readonly=True)
        if strategy == 'bull_put':
            leg1 = clean_num(payload.get('leg1_strike'))
            leg2 = clean_num(payload.get('leg2_strike'))
            credit = clean_num(payload.get('max_profit'))
            c1 = Option(symbol, exp_date, leg1, 'P', 'SMART')
            c2 = Option(symbol, exp_date, leg2, 'P', 'SMART')
            local_ib.qualifyContracts(c1, c2)
            if not c1.conId or not c2.conId:
                return jsonify({
                    'status': 'error',
                    'message': f'無法在 IBKR 找到對應的期權合約: {symbol} {exp_date} P{leg1} / P{leg2}，請確認標的代號或到期日是否正確。'
                })

            contract = Contract(secType='BAG', symbol=symbol, currency='USD', exchange='SMART')
            l1 = ComboLeg(conId=c1.conId, ratio=1, action='BUY', exchange='SMART')
            l2 = ComboLeg(conId=c2.conId, ratio=1, action='SELL', exchange='SMART')
            contract.comboLegs = [l1, l2]

            ticker = local_ib.reqMktData(contract, "", True, False)
            limit_price = credit if credit > 0 else 1.0
            for _ in range(15):
                local_ib.sleep(0.1)
                if ticker.bid and not math.isnan(ticker.bid) and ticker.bid > 0:
                    limit_price = ticker.bid
                    break
            
            bid_val = ticker.bid if ticker.bid and not math.isnan(ticker.bid) else None
            ask_val = ticker.ask if ticker.ask and not math.isnan(ticker.ask) else None
            return jsonify({
                'status': 'ok',
                'strategy': 'bull_put',
                'symbol': symbol,
                'exp_date': payload.get('exp_date'),
                'leg1_strike': leg1,
                'leg2_strike': leg2,
                'bid': bid_val,
                'ask': ask_val,
                'limit_price': round(limit_price, 2),
                'leg1_conId': c1.conId,
                'leg2_conId': c2.conId
            })

        elif strategy in ('short_strangle', 'iron_condor'):
            short_put = clean_num(payload.get('short_put_strike'))
            short_call = clean_num(payload.get('short_call_strike'))
            credit = clean_num(payload.get('max_profit'))
            c_put = Option(symbol, exp_date, short_put, 'P', 'SMART')
            c_call = Option(symbol, exp_date, short_call, 'C', 'SMART')
            local_ib.qualifyContracts(c_put, c_call)
            if not c_put.conId or not c_call.conId:
                return jsonify({
                    'status': 'error',
                    'message': f'無法在 IBKR 找到對應的期權合約: {symbol} {exp_date} P{short_put} / C{short_call}，請確認標的代號或到期日是否正確。'
                })

            contract = Contract(secType='BAG', symbol=symbol, currency='USD', exchange='SMART')
            l1 = ComboLeg(conId=c_put.conId, ratio=1, action='BUY', exchange='SMART')
            l2 = ComboLeg(conId=c_call.conId, ratio=1, action='BUY', exchange='SMART')
            contract.comboLegs = [l1, l2]

            ticker = local_ib.reqMktData(contract, "", True, False)
            limit_price = credit if credit > 0 else 1.0
            for _ in range(15):
                local_ib.sleep(0.1)
                if ticker.bid and not math.isnan(ticker.bid) and ticker.bid > 0:
                    limit_price = ticker.bid
                    break

            bid_val = ticker.bid if ticker.bid and not math.isnan(ticker.bid) else None
            ask_val = ticker.ask if ticker.ask and not math.isnan(ticker.ask) else None
            return jsonify({
                'status': 'ok',
                'strategy': 'short_strangle',
                'symbol': symbol,
                'exp_date': payload.get('exp_date'),
                'short_put_strike': short_put,
                'short_call_strike': short_call,
                'bid': bid_val,
                'ask': ask_val,
                'limit_price': round(limit_price, 2),
                'put_conId': c_put.conId,
                'call_conId': c_call.conId
            })

        elif strategy in ('single_option', 'long_call', 'long_put', 'generic'):
            strike = clean_num(payload.get('strike'))
            csv_ask = clean_num(payload.get('ask'))
            csv_bid = clean_num(payload.get('bid'))
            opt_type_str = str(payload.get('type', '')).upper()
            right = 'P' if ('PUT' in opt_type_str or strategy == 'long_put') else 'C'

            c = Option(symbol, exp_date, strike, right, 'SMART')
            local_ib.qualifyContracts(c)
            if not c.conId:
                return jsonify({
                    'status': 'error',
                    'message': f'無法在 IBKR 找到對應的期權合約: {symbol} {exp_date} {right}{strike}，請確認標的代號或到期日是否正確。'
                })

            ticker = local_ib.reqMktData(c, "", True, False)
            limit_price = csv_bid if csv_bid > 0 else csv_ask
            for _ in range(15):
                local_ib.sleep(0.1)
                if ticker.bid and not math.isnan(ticker.bid) and ticker.bid > 0:
                    limit_price = ticker.bid
                    break
                elif ticker.ask and not math.isnan(ticker.ask) and ticker.ask > 0 and limit_price <= 0:
                    limit_price = ticker.bid if (ticker.bid and not math.isnan(ticker.bid) and ticker.bid > 0) else csv_bid

            if limit_price <= 0:
                limit_price = csv_bid if csv_bid > 0 else (csv_ask if csv_ask > 0 else 0.05)

            bid_val = ticker.bid if ticker.bid and not math.isnan(ticker.bid) else csv_bid
            ask_val = ticker.ask if ticker.ask and not math.isnan(ticker.ask) else csv_ask
            return jsonify({
                'status': 'ok',
                'strategy': 'single_option',
                'symbol': symbol,
                'exp_date': payload.get('exp_date'),
                'strike': strike,
                'type': 'Call' if right == 'C' else 'Put',
                'bid': bid_val,
                'ask': ask_val,
                'limit_price': round(limit_price, 2),
                'conId': c.conId
            })

        return jsonify({'status': 'error', 'message': f'Unknown strategy: {strategy}'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})
    finally:
        try:
            local_ib.disconnect()
        except Exception:
            pass

@dash_app.route('/api/option/execute_barchart_trade', methods=['POST'])
def execute_barchart_trade():
    payload = request.get_json(silent=True) or {}
    strategy = payload.get('strategy')
    symbol = payload.get('symbol')
    action = payload.get('action', 'SELL')
    limit_price = clean_num(payload.get('limit_price'))

    import random, asyncio
    from ib_insync import IB, Contract, ComboLeg, LimitOrder
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    local_ib = IB()
    local_ib.reqPositionsAsync = lambda: asyncio.sleep(0)
    local_ib.reqAccountUpdatesAsync = lambda acct: asyncio.sleep(0)
    local_ib.reqAccountUpdatesMultiAsync = lambda acct: asyncio.sleep(0)
    local_ib.reqOpenOrdersAsync = lambda: asyncio.sleep(0)
    local_ib.reqCompletedOrdersAsync = lambda apiOnly: asyncio.sleep(0)
    local_ib.reqExecutionsAsync = lambda: asyncio.sleep(0)

    try:
        local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(5000, 6999), timeout=10)
        if strategy == 'bull_put':
            leg1_conId = payload.get('leg1_conId')
            leg2_conId = payload.get('leg2_conId')
            contract = Contract(secType='BAG', symbol=symbol, currency='USD', exchange='SMART')
            l1 = ComboLeg(conId=int(leg1_conId), ratio=1, action='BUY', exchange='SMART')
            l2 = ComboLeg(conId=int(leg2_conId), ratio=1, action='SELL', exchange='SMART')
            contract.comboLegs = [l1, l2]

            order = LimitOrder(action, 1, limit_price)
            if TARGET_ACCOUNT:
                order.account = TARGET_ACCOUNT
            order.tif = 'DAY'
            order.transmit = True
            local_ib.placeOrder(contract, order)
            local_ib.sleep(1)
            res_msg = f'已成功送出 {symbol} Bull Put Spread 下單: {action} 1口 (限價 {limit_price})'
            try:
                send_trade_notification(symbol, res_msg, payload)
            except Exception:
                pass
            return jsonify({'status': 'ok', 'message': res_msg})

        elif strategy in ('short_strangle', 'iron_condor'):
            put_conId = payload.get('put_conId')
            call_conId = payload.get('call_conId')
            contract = Contract(secType='BAG', symbol=symbol, currency='USD', exchange='SMART')
            l1 = ComboLeg(conId=int(put_conId), ratio=1, action='BUY', exchange='SMART')
            l2 = ComboLeg(conId=int(call_conId), ratio=1, action='BUY', exchange='SMART')
            contract.comboLegs = [l1, l2]

            order = LimitOrder(action, 1, limit_price)
            if TARGET_ACCOUNT:
                order.account = TARGET_ACCOUNT
            order.tif = 'DAY'
            order.transmit = True
            local_ib.placeOrder(contract, order)
            local_ib.sleep(1)
            res_msg = f'已成功送出 {symbol} 雙賣 (Short Strangle) 下單: {action} 1口 (限價 {limit_price})'
            try:
                send_trade_notification(symbol, res_msg, payload)
            except Exception:
                pass
            return jsonify({'status': 'ok', 'message': res_msg})

        elif strategy in ('single_option', 'long_call', 'long_put', 'generic'):
            conId = payload.get('conId')
            opt_type = payload.get('opt_type', 'Option')
            contract = Contract(conId=int(conId))
            local_ib.qualifyContracts(contract)

            order = LimitOrder(action, 1, limit_price)
            if TARGET_ACCOUNT:
                order.account = TARGET_ACCOUNT
            order.tif = 'DAY'
            order.transmit = True
            local_ib.placeOrder(contract, order)
            local_ib.sleep(1)
            res_msg = f'已成功送出 {symbol} {opt_type} 下單: {action} 1口 (Limit Bid 限價 {limit_price})'
            try:
                send_trade_notification(symbol, res_msg, payload)
            except Exception:
                pass
            return jsonify({'status': 'ok', 'message': res_msg})

        return jsonify({'status': 'error', 'message': f'Unknown strategy: {strategy}'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'下單失敗: {e}'})
    finally:
        try:
            local_ib.disconnect()
        except Exception:
            pass

@dash_app.route('/api/option/data_bull_put', methods=['GET'])
def get_dash_data_bull_put():
    return get_barchart_tables()

@dash_app.route('/api/option/trade_bull_put', methods=['POST'])
def place_dash_trade_bull_put():
    return execute_barchart_trade()

@dash_app.route('/api/option/txo_strangles', methods=['GET'])
def get_txo_strangles():
    global api, LATEST_TMF_PRICE, TXO_OPTIONS_BY_DATE
    if api is None:
        return jsonify({'status': 'error', 'message': 'Shioaji not connected'})
    try:
        import shioaji as sj
        underlying = LATEST_TMF_PRICE
        if not underlying or underlying <= 0:
            target_code = list(api.Contracts.Futures.TMF.keys())[0]
            c = api.Contracts.Futures.TMF[target_code]
            snap = api.snapshots([c])
            if snap and len(snap) > 0:
                underlying = snap[0].close
        if not underlying or underlying <= 0:
            return jsonify({'status': 'error', 'message': '無法取得 TMF 即時行情'})

        tmf_diff = get_tmf_price_diff()
        eff_underlying = underlying + tmf_diff
        atm_strike = round(eff_underlying / 50.0) * 50
        
        if not TXO_OPTIONS_BY_DATE:
            by_date = {}
            target_cats = ['TX1', 'TX2', 'TX3', 'TX4', 'TX5', 'TXO', 'TXU', 'TXV', 'TXX', 'TXY', 'TXZ']
            all_tx_cats = sorted(list(set(target_cats + [a for a in dir(api.Contracts.Options) if a.startswith('TX')])))
            for cat in all_tx_cats:
                if hasattr(api.Contracts.Options, cat):
                    for opt in getattr(api.Contracts.Options, cat):
                        if opt.delivery_date not in by_date:
                            by_date[opt.delivery_date] = []
                        by_date[opt.delivery_date].append(opt)
            TXO_OPTIONS_BY_DATE = by_date

        sorted_dates = sorted(list(TXO_OPTIONS_BY_DATE.keys()))
        results = []
        for d in sorted_dates[:2]:
            opts = TXO_OPTIONS_BY_DATE.get(d, [])
            calls = [o for o in opts if o.option_right == sj.constant.OptionRight.Call and o.strike_price == atm_strike]
            puts = [o for o in opts if o.option_right == sj.constant.OptionRight.Put and o.strike_price == atm_strike]
            if calls and puts:
                c_opt = calls[0]
                p_opt = puts[0]
                results.append({
                    'delivery_date': d,
                    'atm_strike': atm_strike,
                    'call_code': c_opt.code,
                    'call_name': getattr(c_opt, 'name', ''),
                    'call_symbol': getattr(c_opt, 'symbol', ''),
                    'put_code': p_opt.code,
                    'put_name': getattr(p_opt, 'name', ''),
                    'put_symbol': getattr(p_opt, 'symbol', ''),
                    'symbol': f"TXO {d} ATM {atm_strike} (Call+Put 雙賣)"
                })
        return jsonify({'status': 'ok', 'underlying_price': underlying, 'price_diff': tmf_diff, 'effective_price': eff_underlying, 'atm_strike': atm_strike, 'data': results})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@dash_app.route('/api/option/trade_txo_strangle', methods=['POST'])
def trade_txo_strangle():
    global api
    payload = request.get_json(silent=True) or {}
    call_code = payload.get('call')
    put_code = payload.get('put')
    if api is None or not getattr(api, 'futopt_account', None):
        return jsonify({'status': 'error', 'message': 'Shioaji not connected or futopt account not available'})
    try:
        import shioaji as sj
        call_contract = None
        put_contract = None
        target_cats = ['TX1', 'TX2', 'TX3', 'TX4', 'TX5', 'TXO', 'TXU', 'TXV', 'TXX', 'TXY', 'TXZ']
        all_tx_cats = sorted(list(set(target_cats + [a for a in dir(api.Contracts.Options) if a.startswith('TX')])))
        for cat in all_tx_cats:
            if hasattr(api.Contracts.Options, cat):
                for c in getattr(api.Contracts.Options, cat):
                    if c.code == call_code: call_contract = c
                    if c.code == put_code: put_contract = c
                    
        if not call_contract or not put_contract:
            return jsonify({'status': 'error', 'message': f'Cannot find contract for Call {call_code} or Put {put_code}'})

        oc = api.Order(price=0, quantity=1, action=sj.constant.Action.Sell, price_type=sj.constant.FuturesPriceType.MKT, order_type=sj.constant.OrderType.IOC, octype=sj.constant.FuturesOCType.Auto, account=api.futopt_account)
        op = api.Order(price=0, quantity=1, action=sj.constant.Action.Sell, price_type=sj.constant.FuturesPriceType.MKT, order_type=sj.constant.OrderType.IOC, octype=sj.constant.FuturesOCType.Auto, account=api.futopt_account)
        
        trade_c = api.place_order(call_contract, oc)
        trade_p = api.place_order(put_contract, op)
        res_msg = f'TXO 雙賣市價下單成功！Call: {call_code}, Put: {put_code}'
        try:
            send_trade_notification('TXO', res_msg, payload)
        except Exception:
            pass
        return jsonify({'status': 'ok', 'message': f'TXO 雙賣市價下單成功！\nCall: {call_code}\nPut: {put_code}'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'下單失敗: {e}'})


# ==============================================================================
# 🇹🇼 台指 TMF 監控與雙向價差開倉 API
# ==============================================================================
TMF_SIM_LOCK = threading.Lock()

@dash_app.route('/api/tmf/config', methods=['GET'])
def api_get_tmf_config():
    global api, LATEST_TMF_PRICE
    cfg = get_tmf_op_config()
    price_diff = float(cfg.get("price_diff", 0.0))
    wing_width = int(cfg.get("wing_width", 400))
    
    tmf_price = None
    if LATEST_TMF_PRICE and LATEST_TMF_PRICE > 0:
        tmf_price = float(LATEST_TMF_PRICE)
    elif api is not None and hasattr(api, 'Contracts') and hasattr(api.Contracts, 'Futures') and hasattr(api.Contracts.Futures, 'TMF'):
        try:
            target_code = list(api.Contracts.Futures.TMF.keys())[0]
            c = api.Contracts.Futures.TMF[target_code]
            snap = api.snapshots([c])
            if snap and len(snap) > 0 and snap[0].close > 0:
                tmf_price = float(snap[0].close)
        except Exception:
            pass

    eff_price = (tmf_price + price_diff) if tmf_price is not None else None
    atm = (round(eff_price / 100.0) * 100) if eff_price is not None else None

    return jsonify({
        'status': 'ok',
        'price_diff': price_diff,
        'wing_width': wing_width,
        'tmf_price': tmf_price,
        'effective_price': eff_price,
        'atm_strike': atm,
        'config': cfg
    })


@dash_app.route('/api/tmf/save_config', methods=['POST'])
def api_save_tmf_config():
    payload = request.get_json(silent=True) or {}
    if not check_dashboard_auth(payload):
        return jsonify({"status": "error", "message": "密碼錯誤，拒絕儲存設定"}), 401
    
    price_diff = payload.get("price_diff")
    wing_width = payload.get("wing_width")
    ok, msg, updated_cfg = save_tmf_op_config(price_diff=price_diff, wing_width=wing_width)
    if ok:
        return jsonify({
            "status": "ok",
            "message": "台指 TMF 設定已成功儲存至 trade/.env",
            "config": updated_cfg
        })
    else:
        return jsonify({"status": "error", "message": msg}), 500


@dash_app.route('/api/tmf/run_open_shioaji', methods=['POST'])
def api_run_open_shioaji():
    import subprocess, sys
    if not TMF_SIM_LOCK.acquire(blocking=False):
        return jsonify({"status": "error", "message": "目前已有 open_Shioaji 模擬工作正在執行中，請稍候。"})

    try:
        script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "open_Shioaji.py")
        if not os.path.exists(script_path):
            return jsonify({"status": "error", "message": f"找不到腳本: {script_path}"})

        # 調用 open_Shioaji.py 進行模擬試算
        proc = subprocess.run(
            [sys.executable, "-u", script_path, "--dry-run", "--no-line"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            text=True,
            timeout=60,
            encoding="utf-8",
            errors="replace"
        )
        full_output = (proc.stdout or "")
        if proc.stderr:
            full_output += ("\n[STDERR]\n" + proc.stderr)

        if proc.returncode == 0:
            return jsonify({
                "status": "ok",
                "message": "open_Shioaji.py 模擬試算完成！",
                "output": full_output
            })
        else:
            return jsonify({
                "status": "error",
                "message": f"執行失敗 (代碼 {proc.returncode})",
                "output": full_output
            })
    except subprocess.TimeoutExpired:
        return jsonify({"status": "error", "message": "open_Shioaji 執行逾時 (超過 60 秒)"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})
    finally:
        TMF_SIM_LOCK.release()

def start_dashboard_server():
    """在背景執行緒啟動 Flask 監控面板，不影響主邏輯。"""
    from waitress import serve as _serve
    print(f"📱 手機監控面板已啟動：http://0.0.0.0:{DASHBOARD_PORT}/dashboard")
    _serve(dash_app, host="0.0.0.0", port=DASHBOARD_PORT, threads=4)


# ==============================================================================
# 🌟 Futures Code Helper
# ==============================================================================
def get_futures_code(prefix: str = "TMF") -> str:
    now = datetime.datetime.now()
    month_map = "ABCDEFGHIJKL"
    month_calendar = calendar.monthcalendar(now.year, now.month)

    # Taiwan monthly futures generally expire on the 3rd Wednesday.
    third_wednesday = [week[calendar.WEDNESDAY] for week in month_calendar if week[calendar.WEDNESDAY] != 0][2]

    if now.day > third_wednesday or (now.day == third_wednesday and now.hour >= 11):
        target_month, target_year = now.month + 1, now.year
        if target_month > 12:
            target_month, target_year = 1, target_year + 1
        return f"{prefix}{month_map[target_month - 1]}{str(target_year)[-1]}"
    return f"{prefix}{month_map[now.month - 1]}{str(now.year)[-1]}"


# ==============================================================================
# 🌟 Auto Delta Hedge Sender (直接下單至 IBKR / 永豐 Shioaji，不再透過 main.py / Webhook)
# ==============================================================================
def trigger_delta_hedge(action: str, current_price: float, symbol: str, qty: int | float) -> bool:
    """
    Delta 對沖自動下單：
    - 國外期貨 (MES, MNQ, M2K, XC, MJY, M6E, MHG, MNG, MCL, MGC 等): 直接透過本地 IB (ib.placeOrder) 下單
    - 國內期貨 (TMF 台指期貨): 直接透過永豐證券 Shioaji (api.place_order) 下單
    - 依據 SEND_WEBHOOK 開關控制：True 實際送出委託，False 僅在終端機列印測試訊號
    - 下單後送出 LINE 推播通知
    """
    global api
    action = (action or "").upper()
    sym_upper = (symbol or "").upper()
    qty_int = max(1, int(round(float(qty))))

    try:
        # -------------------------------------------------------------
        # 1. 國內期貨：台指期貨 (TMF) 透過 Shioaji 直接下單
        # -------------------------------------------------------------
        if sym_upper == "TMF":
            print(f"[{symbol} 自動對沖系統] 🚨 偵測到 Delta 偏移！準備執行 TMF 對沖下單...")
            print(f" -> 動作: {action} {qty_int}口 {symbol} @ 市價\n")

            if not SEND_WEBHOOK:
                print(f"[{symbol} 自動對沖系統] 🧪 SEND_WEBHOOK=False，目前為測試模式，未實際送出。")
                return True

            if api is None:
                api = init_shioaji()

            if not api or not getattr(api, 'futopt_account', None):
                print(f"[{symbol} 自動對沖系統] ❌ Shioaji 未連線或未找到期貨帳戶 (futopt_account)，無法下單！")
                return False

            target_code = get_futures_code("TMF")
            contract = None
            try:
                if hasattr(api, 'Contracts') and hasattr(api.Contracts, 'Futures') and hasattr(api.Contracts.Futures, 'TMF'):
                    contract = api.Contracts.Futures.TMF.get(target_code)
            except Exception as c_err:
                print(f"[{symbol} 自動對沖系統] ⚠️ 搜尋 TMF 合約異常: {c_err}")

            if not contract:
                print(f"[{symbol} 自動對沖系統] ❌ 找不到 TMF 近月期貨合約: {target_code}")
                return False

            sj_action = sj.constant.Action.Buy if action == "BUY" else sj.constant.Action.Sell
            order = api.Order(
                action=sj_action,
                price=0,
                quantity=qty_int,
                price_type=sj.constant.FuturesPriceType.MKT,
                order_type=sj.constant.OrderType.IOC,
                octype=sj.constant.FuturesOCType.Auto,
                account=api.futopt_account,
            )

            print(f"[{symbol} 自動對沖系統] 🚀 正在直接送出 Shioaji 委託: {target_code} {action} {qty_int}口...")
            trade = api.place_order(contract, order)

            # 等待 Shioaji 委託狀態更新 (IOC 通常非常快速)
            end_time = time.time() + 3
            while trade.status.status.name in ['PendingSubmit', 'Submitted', 'PreSubmitted'] and time.time() < end_time:
                time.sleep(0.1)

            sj_status = getattr(trade.status.status, 'name', str(trade.status.status))
            deals = getattr(trade.status, 'deals', []) or []
            filled_qty = sum(getattr(d, 'quantity', 0) for d in deals)
            remain_qty = qty_int - filled_qty
            avg_price = (sum(d.price * d.quantity for d in deals) / filled_qty) if filled_qty > 0 else 0.0

            if sj_status == 'Filled' or (filled_qty == qty_int):
                msg = f'全數成交 {filled_qty}口 @ {avg_price:.2f}'
                result_status = 'success'
            elif sj_status in ('Cancelled', 'Inactive', 'Failed') and filled_qty > 0:
                msg = f'部分成交 {filled_qty}/{qty_int}口 @ {avg_price:.2f}，剩餘{remain_qty}口因 IOC 取消'
                result_status = 'partial'
            else:
                msg = f'完全未成交，狀態: {sj_status}'
                result_status = 'cancelled'

            print(f"[{symbol} 自動對沖系統] 📢 Shioaji 對沖結果: {msg}")

            res_msg = f'[{symbol} 自動對沖] {action} {qty_int}口 {target_code}: {msg}'
            try:
                payload_data = {
                    "strategy": "delta_hedge",
                    "symbol": symbol,
                    "contract": target_code,
                    "action": action,
                    "quantity": qty_int,
                    "price": current_price,
                    "avg_fill_price": avg_price,
                    "status": result_status,
                }
                send_trade_notification(symbol, res_msg, payload_data)
            except Exception as notify_err:
                print(f"⚠️ LINE 推播發送失敗: {notify_err}")

            return result_status in ('success', 'partial')

        # -------------------------------------------------------------
        # 2. 國外期貨：IBKR 期貨 (MES, MNQ, M2K, XC, MJY, M6E 等) 直接下單
        # -------------------------------------------------------------
        if not ib or not ib.isConnected():
            print(f"[{symbol} 自動對沖系統] ❌ IB 未連線，無法執行對沖下單！")
            return False

        target_contract = get_target_future_contract(ib, symbol)
        if not target_contract:
            print(f"[{symbol} 自動對沖系統] ❌ 找不到 {symbol} 的有效近月期貨合約！")
            return False

        sym_disp = getattr(target_contract, 'localSymbol', target_contract.symbol)
        print(f"[{symbol} 自動對沖系統] 🎯 鎖定最近可下單期貨合約: {sym_disp} (到期日: {target_contract.lastTradeDateOrContractMonth}, conId: {target_contract.conId})")

        print(f"[{symbol} 自動對沖系統] 🚨 偵測到 Delta 偏移！準備執行 IB 對沖下單...")
        print(f" -> 動作: {action} {qty_int}口 {sym_disp} @ {current_price}\n")

        if not SEND_WEBHOOK:
            print(f"[{symbol} 自動對沖系統] 🧪 SEND_WEBHOOK=False，目前為測試模式，未實際送出。")
            return True

        order = MarketOrder(action, qty_int)
        order.outsideRth = True
        order.algoStrategy = 'Adaptive'
        order.algoParams = [TagValue('adaptivePriority', 'Patient')]
        order.tif = 'GTC'
        if TARGET_ACCOUNT:
            order.account = TARGET_ACCOUNT

        print(f"[{symbol} 自動對沖系統] 🚀 正在直接送出本地 IB 委託: {action} {qty_int}口 {sym_disp} (Adaptive Patient)...")
        trade = ib.placeOrder(target_contract, order)

        # 等待委託確認 (Adaptive Algo 等待至多 5 秒)
        end_time = time.time() + 5
        while not trade.isDone() and time.time() < end_time:
            ib.sleep(0.5)

        ib_status = trade.orderStatus.status
        filled_qty = trade.orderStatus.filled
        remain_qty = trade.orderStatus.remaining
        avg_price = trade.orderStatus.avgFillPrice

        error_msg = ""
        for log_entry in trade.log:
            if getattr(log_entry, 'errorCode', 0) != 0 or 'Error' in getattr(log_entry, 'message', '') or 'rejected' in getattr(log_entry, 'message', '').lower():
                error_msg = getattr(log_entry, 'message', '').replace('<br>', ' ')
                break

        if ib_status == 'Filled':
            msg = f'全數成交 {filled_qty}口 @ {avg_price}'
            result_status = 'success'
        elif ib_status in ('Submitted', 'PreSubmitted'):
            msg = f'委託已送出 (Adaptive Algo)，目前狀態: {ib_status}，已成交 {filled_qty}口'
            result_status = 'submitted'
        elif ib_status in ('Cancelled', 'Inactive') and filled_qty > 0:
            msg = f'部分成交 {filled_qty}/{int(filled_qty + remain_qty)}口 @ {avg_price}，剩餘{remain_qty}口因故取消'
            result_status = 'partial'
        else:
            if error_msg:
                msg = f'完全未成交，發生錯誤: {error_msg}'
                if 'near-expiration' in error_msg.lower() or 'physical delivery' in error_msg.lower() or '201' in error_msg:
                    act_sym = FUTURE_SYMBOL_ALIAS.get(symbol.upper(), symbol.upper())
                    ex = FUTURE_EXCHANGE_MAP.get(act_sym, "CME")
                    FUTURE_CONTRACT_CACHE.pop(f"{act_sym}_{ex}", None)
                    print(f"⚠️ [{symbol} 自動對沖系統] 合約 {sym_disp} 因 IBKR 臨期/實物交割風控政策被拒單，已自快取中清除！")
            else:
                msg = f'完全未成交，狀態: {ib_status}（市場未開盤或流動性不足）'
            result_status = 'cancelled'

        print(f"[{symbol} 自動對沖系統] 📢 IB 下單結果: {msg}")

        res_msg = f'[{symbol} 自動對沖] {action} {qty_int}口 {sym_disp}: {msg}'
        try:
            payload_data = {
                "strategy": "delta_hedge",
                "symbol": symbol,
                "contract": sym_disp,
                "action": action,
                "quantity": qty_int,
                "price": current_price,
                "avg_fill_price": avg_price,
                "status": result_status,
                "conId": getattr(target_contract, 'conId', ''),
                "expiry": getattr(target_contract, 'lastTradeDateOrContractMonth', ''),
            }
            send_trade_notification(symbol, res_msg, payload_data)
        except Exception as notify_err:
            print(f"⚠️ LINE 推播發送失敗: {notify_err}")

        return result_status in ('success', 'submitted', 'partial')

    except Exception as e:
        print(f"[{symbol} 自動對沖系統] ❌ 下單過程發生異常: {e}")
        return False


# ==============================================================================
# Greeks: Stock/ETF Options use Black-Scholes
# ==============================================================================
def calculate_stock_option_greeks(
    S: float,
    K: float,
    T: float,
    r: float,
    option_price: float,
    option_type: str,
    q: float = 0.0,
) -> tuple[float, float, float]:
    """Return delta, theta per day, gamma for stock/ETF options."""
    option_type = (option_type or "").upper()
    if T <= 0 or S <= 0 or K <= 0 or option_price <= 0 or option_type not in ("C", "P"):
        return 0.0, 0.0, 0.0

    def bs_price(sigma: float) -> float:
        d1 = (math.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)

        if option_type == "C":
            return S * math.exp(-q * T) * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * math.exp(-q * T) * norm.cdf(-d1)

    try:
        iv = optimize.brentq(lambda x: bs_price(x) - option_price, 0.001, 10.0)
    except Exception:
        try:
            res = optimize.minimize(
                lambda x: abs(bs_price(float(x[0])) - option_price),
                [1.0],
                bounds=[(0.001, 25.0)],
            )
            iv = float(res.x[0])
        except Exception:
            iv = 0.30

    d1 = (math.log(S / K) + (r - q + 0.5 * iv**2) * T) / (iv * math.sqrt(T))
    d2 = d1 - iv * math.sqrt(T)

    if option_type == "C":
        delta = math.exp(-q * T) * norm.cdf(d1)
        theta_year = (
            -(S * math.exp(-q * T) * norm.pdf(d1) * iv) / (2 * math.sqrt(T))
            - r * K * math.exp(-r * T) * norm.cdf(d2)
            + q * S * math.exp(-q * T) * norm.cdf(d1)
        )
    else:
        delta = math.exp(-q * T) * (norm.cdf(d1) - 1.0)
        theta_year = (
            -(S * math.exp(-q * T) * norm.pdf(d1) * iv) / (2 * math.sqrt(T))
            + r * K * math.exp(-r * T) * norm.cdf(-d2)
            - q * S * math.exp(-q * T) * norm.cdf(-d1)
        )

    gamma = math.exp(-q * T) * norm.pdf(d1) / (S * iv * math.sqrt(T))
    theta_day = theta_year / 365.0
    return float(delta), float(theta_day), float(gamma)


# ==============================================================================
# Greeks: Futures Options use Black-76
# ==============================================================================
def calculate_futures_option_greeks(
    F: float,
    K: float,
    T: float,
    r: float,
    option_price: float,
    option_type: str,
) -> tuple[float, float, float]:
    """Return delta, theta per day, gamma for futures options."""
    option_type = (option_type or "").upper()
    if T <= 0 or F <= 0 or K <= 0 or option_price <= 0 or option_type not in ("C", "P"):
        return 0.0, 0.0, 0.0

    def black76_price(sigma: float) -> float:
        d1 = (math.log(F / K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)

        if option_type == "C":
            return math.exp(-r * T) * (F * norm.cdf(d1) - K * norm.cdf(d2))
        return math.exp(-r * T) * (K * norm.cdf(-d2) - F * norm.cdf(-d1))

    try:
        iv = optimize.brentq(lambda x: black76_price(x) - option_price, 0.001, 10.0)
    except Exception:
        try:
            res = optimize.minimize(
                lambda x: abs(black76_price(float(x[0])) - option_price),
                [1.0],
                bounds=[(0.001, 25.0)],
            )
            iv = float(res.x[0])
        except Exception:
            iv = 0.30

    d1 = (math.log(F / K) + 0.5 * iv**2 * T) / (iv * math.sqrt(T))
    d2 = d1 - iv * math.sqrt(T)

    if option_type == "C":
        delta = math.exp(-r * T) * norm.cdf(d1)
        theta_year = math.exp(-r * T) * (
            -(F * norm.pdf(d1) * iv) / (2 * math.sqrt(T))
            + r * (F * norm.cdf(d1) - K * norm.cdf(d2))
        )
    else:
        delta = -math.exp(-r * T) * norm.cdf(-d1)
        theta_year = math.exp(-r * T) * (
            -(F * norm.pdf(d1) * iv) / (2 * math.sqrt(T))
            + r * (K * norm.cdf(-d2) - F * norm.cdf(-d1))
        )

    gamma = math.exp(-r * T) * norm.pdf(d1) / (F * iv * math.sqrt(T))
    theta_day = theta_year / 365.0
    return float(delta), float(theta_day), float(gamma)


# ==============================================================================
# Greeks: Prefer IB API option Greeks for IB option/FOP positions
# ==============================================================================
def _finite_number(value) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except Exception:
        return False


def _valid_option_computation(comp) -> bool:
    """IB OptionComputation must have usable delta/gamma/theta."""
    if comp is None:
        return False
    return (
        _finite_number(getattr(comp, "delta", None))
        and _finite_number(getattr(comp, "gamma", None))
        and _finite_number(getattr(comp, "theta", None))
    )


def _mid_option_computation(bid_comp, ask_comp):
    """Create simple midpoint Greeks from bid/ask computations when both exist."""
    if not (_valid_option_computation(bid_comp) and _valid_option_computation(ask_comp)):
        return None
    return {
        "delta": (float(bid_comp.delta) + float(ask_comp.delta)) / 2.0,
        "gamma": (float(bid_comp.gamma) + float(ask_comp.gamma)) / 2.0,
        "theta": (float(bid_comp.theta) + float(ask_comp.theta)) / 2.0,
        "vega": (float(getattr(bid_comp, "vega", 0.0) or 0.0) + float(getattr(ask_comp, "vega", 0.0) or 0.0)) / 2.0,
        "impliedVol": (float(getattr(bid_comp, "impliedVol", 0.0) or 0.0) + float(getattr(ask_comp, "impliedVol", 0.0) or 0.0)) / 2.0,
    }


def get_ib_option_greeks(ib_client: IB, contract: Contract, wait_seconds: float = 1.5):
    """
    Ask IB/TWS directly for option Greeks.

    Returns:
        (delta, theta, gamma, source)

    If IB does not return usable option Greeks, returns None.
    Caller should set mute flag and skip hedging for that group.
    """
    ticker = None
    try:
        ticker = ib_client.reqMktData(contract, "", False, False)
        ib_client.sleep(wait_seconds)

        candidates = []
        if _valid_option_computation(getattr(ticker, "modelGreeks", None)):
            candidates.append(("IB modelGreeks", ticker.modelGreeks))

        mid = _mid_option_computation(getattr(ticker, "bidGreeks", None), getattr(ticker, "askGreeks", None))
        if mid is not None:
            candidates.append(("IB mid(bid/ask)Greeks", mid))

        for source_name, comp in [
            ("IB lastGreeks", getattr(ticker, "lastGreeks", None)),
            ("IB bidGreeks", getattr(ticker, "bidGreeks", None)),
            ("IB askGreeks", getattr(ticker, "askGreeks", None)),
        ]:
            if _valid_option_computation(comp):
                candidates.append((source_name, comp))

        if not candidates:
            return None

        source, comp = candidates[0]
        if isinstance(comp, dict):
            delta = float(comp["delta"])
            gamma = float(comp["gamma"])
            theta = float(comp["theta"])
            und_price = float(comp.get("undPrice") or 0.0)
        else:
            delta = float(comp.delta)
            gamma = float(comp.gamma)
            theta = float(comp.theta)
            und_price = float(getattr(comp, "undPrice", 0.0) or 0.0)

        if not (_finite_number(delta) and _finite_number(gamma) and _finite_number(theta)):
            return None

        return delta, theta, gamma, und_price, source

    except Exception as e:
        print(f"⚠️ IB Greeks 讀取失敗: {getattr(contract, 'localSymbol', contract.symbol)}, error={e}")
        return None
    finally:
        if ticker is not None:
            try:
                ib_client.cancelMktData(contract)
            except Exception:
                pass


# ==============================================================================
# Hedge evaluation
# ==============================================================================
def evaluate_and_trigger_hedge(
    group_name: str,
    total_delta: float,
    total_gamma: float,
    underlying_price: float,
    upper_threshold: float,
    lower_threshold: float,
    hedge_symbol: str,
    hedge_qty: int | float,
    last_hedge_times_dict: dict[str, float],
    cooldown_seconds: int,
) -> None:
    if underlying_price is None or underlying_price <= 0:
        underlying_price = 0.0
        print(f"ℹ️ [{group_name}] 庫存無期貨部位 (市價為 0)，下單時將由交易核心向 IB 即時取得市價...")

    current_ts = time.time()

    # ✅ Important change: use abs(total_gamma), otherwise short gamma becomes fake 0.01.
    gamma_per_100 = max(abs(total_gamma), 0.01)
    hedge_up_points = (abs(upper_threshold) / gamma_per_100) * 100.0
    hedge_down_points = (abs(lower_threshold) / gamma_per_100) * 100.0
    last_time = last_hedge_times_dict.get(group_name, 0)

    now = datetime.datetime.now()
    weekday = now.weekday()
    current_time_str = now.strftime('%H:%M')

    is_weekend_closed = (
        (weekday == 5 and current_time_str > "05:00")
        or (weekday == 6)
        or (weekday == 0 and current_time_str < "06:00")
    )
    is_daily_closed = "04:59" < current_time_str < "07:50"

    if is_weekend_closed or is_daily_closed:
        print(f"⏳ [{group_name}] 沒開盤！暫停對沖。")
        return

    if (current_ts - last_time) > cooldown_seconds:
        if total_delta > upper_threshold:
            print(
                f"\n🔥 [{group_name} 觸發] 總 Delta ({total_delta:.2f}) > 觸發上限 {upper_threshold:.2f} "
                f"(單邊門檻約 {hedge_up_points:.0f} 點)，執行【賣出】對沖！"
            )
            success = trigger_delta_hedge("SELL", underlying_price, symbol=hedge_symbol, qty=hedge_qty)
            if success:
                last_hedge_times_dict[group_name] = current_ts
        elif total_delta < lower_threshold:
            print(
                f"\n🔥 [{group_name} 觸發] 總 Delta ({total_delta:.2f}) < 觸發下限 {lower_threshold:.2f} "
                f"(單邊門檻約 {hedge_down_points:.0f} 點)，執行【買進】對沖！"
            )
            success = trigger_delta_hedge("BUY", underlying_price, symbol=hedge_symbol, qty=hedge_qty)
            if success:
                last_hedge_times_dict[group_name] = current_ts
    else:
        if total_delta > upper_threshold or total_delta < lower_threshold:
            rem_time = int(cooldown_seconds - (current_ts - last_time))
            print(f"⏳ [{group_name} 自動對沖] Delta 已達標 ({total_delta:.2f})，系統冷卻中... 剩餘 {rem_time} 秒")


# ==============================================================================
# Other tools and parsers
# ==============================================================================
def parse_tx_opt_code(code: str):
    if len(code) != 10 or not code.startswith("TX"):
        return None
    try:
        strike = int(code[3:8])
        month_char, year_char = code[8].upper(), int(code[9])
        if 'A' <= month_char <= 'L':
            cp = 'C'
            month = ord(month_char) - ord('A') + 1
        elif 'M' <= month_char <= 'X':
            cp = 'P'
            month = ord(month_char) - ord('M') + 1
        else:
            return None

        current_year = datetime.datetime.now().year
        year = (current_year // 10) * 10 + year_char
        if year < current_year:
            year += 10

        month_calendar = calendar.monthcalendar(year, month)
        week_indicator = code[2].upper()

        if week_indicator in ['U', 'V', 'X', 'Y', 'Z']:
            target_days = [week[calendar.FRIDAY] for week in month_calendar if week[calendar.FRIDAY] != 0]
            week_idx = {'U': 0, 'V': 1, 'X': 2, 'Y': 3, 'Z': 4}.get(week_indicator, 0)
        else:
            target_days = [week[calendar.WEDNESDAY] for week in month_calendar if week[calendar.WEDNESDAY] != 0]
            if week_indicator == 'O':
                week_idx = 2
            elif week_indicator.isdigit():
                week_idx = int(week_indicator) - 1
            else:
                week_idx = 2

        if not target_days:
            return None
        if week_idx >= len(target_days):
            week_idx = len(target_days) - 1

        expiry_date = datetime.date(year, month, target_days[week_idx])
        return strike, cp, max((expiry_date - datetime.date.today()).days, 0.001)
    except Exception:
        return None


def auto_exit():
    time.sleep(RESTART_INTERVAL)
    os._exit(0)


def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')


def format_price(price, decimals: int = 2) -> str:
    return "N/A" if price is None else f"{price:.{decimals}f}"



def get_account_details(ib_client: IB) -> dict[str, str]:
    try:
        summary = ib_client.accountSummary()
        net_liq = next((i for i in summary if i.tag == 'NetLiquidation' and i.account == TARGET_ACCOUNT), None)
        avail = next((i for i in summary if i.tag == 'AvailableFunds' and i.account == TARGET_ACCOUNT), None)
        return {
            'net_liq': f"{net_liq.value} {net_liq.currency}" if net_liq else "N/A",
            'avail': f"{avail.value} {avail.currency}" if avail else "N/A",
        }
    except Exception:
        return {'net_liq': "N/A", 'avail': "N/A"}


def get_recent_executions(ib_client: IB):
    return sorted(ib_client.fills(), key=lambda x: x.execution.time, reverse=False)


def get_positions_with_pnl(ib_client: IB, ticker_decimals_map: dict[str, int]):
    ib_client.reqPositions()
    ib_client.sleep(0.5)
    my_positions = [p for p in ib_client.positions() if p.account == TARGET_ACCOUNT]
    if not my_positions:
        return []

    ib_client.qualifyContracts(*[p.contract for p in my_positions])
    tickers = ib_client.reqTickers(*[p.contract for p in my_positions])
    results = []

    for p in my_positions:
        ticker = next((t for t in tickers if t.contract.conId == p.contract.conId), None)
        market_price = 0.0
        if ticker:
            for pr in [ticker.marketPrice(), ticker.last, ticker.close]:
                if pr == pr and pr > 0:
                    market_price = float(pr)
                    break

        multiplier = float(p.contract.multiplier) if p.contract.multiplier else 1.0
        avg_cost_unit = p.avgCost / multiplier if multiplier != 0 else p.avgCost

        base_symbol = p.contract.symbol
        local_symbol = p.contract.localSymbol if p.contract.localSymbol else base_symbol

        is_agri_cents = base_symbol in ['XC', 'YC', 'ZC', 'ZW', 'YW', 'XW', 'ZS', 'YK', 'XK'] or 'OZC' in local_symbol
        if is_agri_cents:
            market_price_usd = market_price / 100.0
            avg_cost_disp = avg_cost_unit * 100.0
        else:
            market_price_usd = market_price
            avg_cost_disp = avg_cost_unit

        pnl = (market_price_usd - avg_cost_unit) * p.position * multiplier if market_price_usd > 0 and avg_cost_unit > 0 else 0.0
        total_market_value = abs(p.position * market_price_usd * multiplier)

        import re
        decimals = ticker_decimals_map.get(base_symbol, 1)
        if local_symbol:
            if any(s in local_symbol for s in ["MJY", "M6E", "MHG", "MNG"]) or re.search(r"\s+[CP]\d+", local_symbol):
                decimals = 6

        expiry_str = p.contract.lastTradeDateOrContractMonth or ''
        dte_num = None
        if expiry_str:
            try:
                s_exp = str(expiry_str).replace('-', '').replace('/', '').strip()
                if len(s_exp) == 8:
                    exp_d = datetime.date(int(s_exp[:4]), int(s_exp[4:6]), int(s_exp[6:8]))
                    dte_num = (exp_d - datetime.date.today()).days
            except Exception:
                pass

        results.append({
            'symbol': base_symbol,
            'localSymbol': local_symbol,
            'position': p.position,
            'avgCost': avg_cost_disp,
            'marketPrice': market_price,
            'pnl': pnl,
            'totalCost': total_market_value,
            'decimals': decimals,
            'multiplier': multiplier,
            'secType': p.contract.secType,
            'strike': p.contract.strike,
            'right': p.contract.right,
            'expiry': p.contract.lastTradeDateOrContractMonth,
            'contract': p.contract,
            'conId': p.contract.conId,
            'dte': dte_num,
        })

    return results


# ==============================================================================
# Shioaji login
# ==============================================================================
def init_shioaji():
    global api, TXO_OPTIONS_BY_DATE
    if not SHIOAJI_API_KEY or not SHIOAJI_SECRET_KEY:
        print("⚠️ Shioaji API key/secret 未設定，跳過登入")
        return None

    api = sj.Shioaji()
    api.login(SHIOAJI_API_KEY, SHIOAJI_SECRET_KEY)

    if SHIOAJI_CA_PATH and SHIOAJI_CA_PASSWD:
        api.activate_ca(ca_path=SHIOAJI_CA_PATH, ca_passwd=SHIOAJI_CA_PASSWD)

    try:
        by_date = {}
        target_cats = ['TX1', 'TX2', 'TX3', 'TX4', 'TX5', 'TXO', 'TXU', 'TXV', 'TXX', 'TXY', 'TXZ']
        all_tx_cats = sorted(list(set(target_cats + [a for a in dir(api.Contracts.Options) if a.startswith('TX')])))
        for cat in all_tx_cats:
            if hasattr(api.Contracts.Options, cat):
                for c in getattr(api.Contracts.Options, cat):
                    d = c.delivery_date
                    if d not in by_date:
                        by_date[d] = []
                    by_date[d].append(c)
        TXO_OPTIONS_BY_DATE = by_date
    except Exception as e:
        print(f"快取選擇權合約失敗: {e}")

    return api


# ==============================================================================
# Main loop
# ==============================================================================
def main():
    threading.Thread(target=auto_exit, daemon=True).start()
    threading.Thread(target=start_dashboard_server, daemon=True).start()
    ticker_decimals_map = {}

    init_shioaji()

    while True:
        try:
            now_dt = datetime.datetime.now()

            if not ib.isConnected():
                print(f"--- 正在連線 IB TWS ({IB_HOST}:{IB_PORT})... ---")
                ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, timeout=15)
                ib.reqMarketDataType(4)

                '''
                for item in WATCH_LIST:
                    c = Contract(
                        symbol=item['symbol'],
                        secType=item['secType'],
                        exchange=item['exchange'],
                        currency=item['currency'],
                        lastTradeDateOrContractMonth=item.get('expiry', ''),
                    )
                    ib.qualifyContracts(c)
                    ib.reqMktData(c, '', False, False)
                    ticker_decimals_map[item['symbol']] = item.get('decimals', 2)
                '''
                print(">>> IB 連接成功！")

            acc_info = get_account_details(ib)
            recent_fills = get_recent_executions(ib)
            portfolio_data = get_positions_with_pnl(ib, ticker_decimals_map)
            orders = ib.reqAllOpenOrders()

            #clear_screen()

            # --- Orders ---
            dashboard_orders = []
            print(f"\n--- [ 有效掛單 (Orders: {len(orders)}) ] ---")
            if not orders:
                print("目前無有效掛單。")
            else:
                for t in orders:
                    c, o = t.contract, t.order
                    price_text = format_price(o.lmtPrice, 6) if getattr(o, 'lmtPrice', 0) and o.lmtPrice > 0 else 'MKT'
                    print(f"{c.localSymbol if c.localSymbol else c.symbol:<20} {o.action:<6} {o.totalQuantity:<6} {price_text:<15} {t.orderStatus.status:<10}")
                    dashboard_orders.append({
                        "symbol": c.localSymbol if c.localSymbol else c.symbol,
                        "action": o.action,
                        "quantity": o.totalQuantity,
                        "price": price_text,
                        "status": t.orderStatus.status,
                    })

            # --- Recent fills ---
            dashboard_fills = []
            print(f"\n--- [ 最近 10 筆成交 (IB) ] ---")
            if not recent_fills:
                print("目前無成交紀錄。")
            else:
                print(f"{'時間':<10} {'商品':<20} {'動作':<6} {'價格':<10} {'平倉損益':<10}")
                print("-" * 75)
                for f in recent_fills[-10:]:
                    c, e, r = f.contract, f.execution, f.commissionReport
                    rpnl = r.realizedPNL if r and r.realizedPNL != 1.7976931348623157e+308 else 0.0
                    disp_sym = c.localSymbol if c.localSymbol else c.symbol
                    print(f"{e.time.strftime('%H:%M:%S'):<10} {disp_sym:<20} {e.side:<6} {e.price:<10} {f'{rpnl:+.2f}' if rpnl != 0 else '-':<10}")
                    dashboard_fills.append({
                        "time": e.time.strftime('%H:%M:%S'),
                        "symbol": disp_sym,
                        "side": e.side,
                        "price": e.price,
                        "realized_pnl": rpnl,
                    })

            # --- Positions and hedge ---
            dashboard_groups = []
            print(f"\n--- [ 持倉損益與 IB 總風險 (Positions: {len(portfolio_data)}) ] ---")

            if portfolio_data:
                grouped_data = {k: [] for k in HEDGE_CONFIG.keys()}
                grouped_data['未分類(Other)'] = []

                group_underlying = {k: 0.0 for k in HEDGE_CONFIG.keys()}
                group_mute_flag = {k: False for k in HEDGE_CONFIG.keys()}
                stock_prices = {}

                # First pass: get true hedge underlying price.
                # ✅ Options are excluded so that option localSymbol will not overwrite the underlying price.
                for item in portfolio_data:
                    sym_disp = item['localSymbol'] if item['localSymbol'] else item['symbol']
                    base_symbol = item['symbol']
                    mkt_p = item['marketPrice']
                    sec = item.get('secType')

                    if mkt_p <= 0:
                        continue

                    if sec == 'STK':
                        stock_prices[base_symbol] = mkt_p

                    if sec in ['STK', 'FUT', 'CONTFUT', 'CRYPTO']:
                        for g_name, g_info in HEDGE_CONFIG.items():
                            hedge_sym = g_info['hedge_sym'].upper()
                            if base_symbol.upper() == hedge_sym or sym_disp.upper().startswith(hedge_sym):
                                group_underlying[g_name] = mkt_p
                                break

                group_greeks = {
                    k: {'delta': 0.0, 'gamma': 0.0, 'theta': 0.0, 'underlying_price': group_underlying[k]}
                    for k in HEDGE_CONFIG.keys()
                }

                # Second pass: calculate greeks.
                for item in portfolio_data:
                    sym_disp = item['localSymbol'] if item['localSymbol'] else item['symbol']
                    item['_disp'] = sym_disp
                    sec_type = item.get('secType', '')
                    qty = float(item['position'])

                    my_group_name = next(
                        (g for g, info in HEDGE_CONFIG.items() if sym_disp.upper().startswith(info['symbols'])),
                        None,
                    )

                    # 針對 VIX 對沖，忽略選擇權 (KORU/SOXL options)
                    if my_group_name == 'VIX對沖' and sec_type in ['OPT', 'FOP']:
                        my_group_name = None

                    pos_delta, pos_theta, pos_gamma = 0.0, 0.0, 0.0

                    try:
                        if my_group_name:
                            group_info = HEDGE_CONFIG[my_group_name]
                            target_micro_mult = float(group_info['micro_mult'])
                            is_value_hedge = bool(group_info.get('hedge_by_value', False))
                            contract_multiplier = float(item.get('multiplier', 1.0))
                            micro_ratio = contract_multiplier / target_micro_mult if target_micro_mult else 1.0

                            if sec_type in ['FUT', 'CONTFUT']:
                                mkt_p = float(item.get('marketPrice') or 0.0)
                                if mkt_p <= 0 and qty != 0:
                                    group_mute_flag[my_group_name] = True
                                    print(f"🎯 [{my_group_name}] 🔕 {sym_disp} 缺乏期貨現價，安全鎖啟動！")
                                if sym_disp.upper().startswith(group_info['hedge_sym'].upper()):
                                    pos_delta = 1.0 * qty
                                else:
                                    pos_delta = 1.0 * qty * micro_ratio

                            elif sec_type in ['STK', 'CRYPTO']:
                                mkt_p = float(item.get('marketPrice') or 0.0)
                                if mkt_p <= 0 and qty != 0:
                                    group_mute_flag[my_group_name] = True
                                    print(f"🎯 [{my_group_name}] 🔕 {sym_disp} 缺乏現價報價，安全鎖啟動！")
                                elif is_value_hedge:
                                    future_price = group_underlying.get(my_group_name, 0.0)
                                    if future_price > 0:
                                        future_notional = future_price * target_micro_mult
                                        pos_delta = (qty * mkt_p) / future_notional
                                    else:
                                        group_mute_flag[my_group_name] = True
                                        print(f"🎯 [{my_group_name}] 🔕 缺乏對沖標的 ({group_info['hedge_sym']}) 報價，安全鎖啟動！")
                                else:
                                    pos_delta = 1.0 * qty * micro_ratio

                            elif sec_type in ['FOP', 'OPT']:
                                strike = float(item.get('strike', 0))
                                opt_mkt_price = float(item['marketPrice'])
                                option_contract = item.get('contract')

                                if opt_mkt_price <= 0 or strike <= 0 or option_contract is None:
                                    group_mute_flag[my_group_name] = True
                                    print(f"🎯 [{my_group_name}] 🔕 {sym_disp} 缺乏期權現價或合約資料，安全鎖啟動！")
                                else:
                                    ib_greeks = get_ib_option_greeks(
                                        ib_client=ib,
                                        contract=option_contract,
                                        wait_seconds=IB_GREEKS_WAIT_SECONDS,
                                    )

                                    if ib_greeks is None:
                                        group_mute_flag[my_group_name] = True
                                        print(f"🎯 [{my_group_name}] 🔕 {sym_disp} 缺乏 IB Greeks，安全鎖啟動，強制靜音暫停對沖！")
                                    else:
                                        d, t, g, und_p, greek_source = ib_greeks
                                        item['greek_source'] = greek_source

                                        if und_p > 0:
                                            if group_underlying.get(my_group_name, 0.0) <= 0:
                                                group_underlying[my_group_name] = und_p
                                            group_greeks[my_group_name]['underlying_price'] = group_underlying[my_group_name]

                                        if is_value_hedge and sec_type == 'OPT':
                                            # Convert stock/ETF option delta into micro-futures equivalent by notional value.
                                            S = stock_prices.get(item['symbol'], 0.0)
                                            future_price = group_underlying.get(my_group_name, 0.0)
                                            if S > 0 and future_price > 0:
                                                future_notional = future_price * target_micro_mult
                                                pos_delta = (d * qty * contract_multiplier * S) / future_notional
                                                pos_theta = t * qty * contract_multiplier
                                                pos_gamma = (g * qty * contract_multiplier * S) / future_notional * 100.0
                                            else:
                                                group_mute_flag[my_group_name] = True
                                                print(f"🎯 [{my_group_name}] 🔕 {sym_disp} 缺乏標的/對沖標的報價，安全鎖啟動！")
                                        else:
                                            pos_delta = d * qty * micro_ratio
                                            pos_theta = t * qty * contract_multiplier
                                            pos_gamma = g * qty * micro_ratio * 100.0
                        else:
                            if sec_type in ['FOP', 'OPT']:
                                strike = float(item.get('strike', 0))
                                opt_mkt_price = float(item['marketPrice'])
                                option_contract = item.get('contract')
                                contract_multiplier = float(item.get('multiplier', 1.0))
                                
                                if opt_mkt_price > 0 and strike > 0 and option_contract:
                                    ib_greeks = get_ib_option_greeks(
                                        ib_client=ib,
                                        contract=option_contract,
                                        wait_seconds=IB_GREEKS_WAIT_SECONDS,
                                    )
                                    if ib_greeks:
                                        d, t, g, und_p, greek_source = ib_greeks
                                        item['greek_source'] = greek_source
                                        pos_delta = d * qty * contract_multiplier
                                        pos_theta = t * qty * contract_multiplier
                                        pos_gamma = g * qty * contract_multiplier
                    except Exception as e:
                        print(f"⚠️ Greek 計算失敗: {sym_disp}, error={e}")

                    item['disp_delta'] = f"{pos_delta:.5f}" if abs(pos_delta) > 0.00001 else "0.0000"
                    item['disp_theta'] = f"{pos_theta:.0f}" if abs(pos_theta) > 0.00001 else "0"
                    item['disp_gamma'] = f"{pos_gamma:.5f}" if abs(pos_gamma) > 0.00001 else "0.0000"

                    if my_group_name:
                        grouped_data[my_group_name].append(item)
                        group_greeks[my_group_name]['delta'] += pos_delta
                        group_greeks[my_group_name]['gamma'] += pos_gamma
                        group_greeks[my_group_name]['theta'] += pos_theta
                    else:
                        grouped_data['未分類(Other)'].append(item)

                print(f"{'商品':<20} {'部位':<6} {'成本':<10} {'現價':<10} {'損益(P&L)':<13} {'總價值(USD)':<10} {'微期Δ':<8} {'expiry':<10} {'微期θ':<8} {'微期γ':<8}")
                print("-" * 115)

                dashboard_groups = []

                is_first_group = True
                for g_name, items in grouped_data.items():
                    if not items:
                        continue

                    if g_name == '未分類(Other)':
                        # 未分類(Other) 用 DTE 由小排到大，無到期日(如現貨股票/現金)排在最後
                        def _get_sort_dte(x):
                            d = x.get('dte')
                            if d is not None:
                                try:
                                    return (0, float(d))
                                except Exception:
                                    pass
                            exp = x.get('expiry') or (x.get('contract') and getattr(x['contract'], 'lastTradeDateOrContractMonth', None))
                            if exp:
                                try:
                                    s_exp = str(exp).replace('-', '').replace('/', '').strip()
                                    if len(s_exp) >= 8:
                                        exp_d = datetime.date(int(s_exp[:4]), int(s_exp[4:6]), int(s_exp[6:8]))
                                        return (0, float((exp_d - datetime.date.today()).days))
                                except Exception:
                                    pass
                            return (1, float('inf'))
                        items.sort(key=lambda x: (_get_sort_dte(x), len(x['_disp']), x['_disp']))
                    else:
                        items.sort(key=lambda x: (len(x['_disp']), x['_disp']))
                    if not is_first_group:
                        #print()
                        pass
                    is_first_group = False

                    group_positions_snapshot = []
                    for item in items:
                        c_str = format_price(item['avgCost'], item['decimals'])
                        m_str = format_price(item['marketPrice'], item['decimals'])
                        p_str = f"{item['pnl']:+,.2f}"
                        v_str = f"{item['totalCost']:,.1f}"
                        print(
                            f"{item['_disp']:<20} {item['position']:<8.1f} {c_str:<12} {m_str:<12} "
                            f"{p_str:<15} {v_str:<12} {item['disp_delta']:<10} {item.get('expiry', ''):<10} "
                            f"{item['disp_theta']:<10} {item['disp_gamma']:<10}"
                        )
                        group_positions_snapshot.append({
                            "symbol": item['_disp'],
                            "position": item['position'],
                            "avg_cost": item['avgCost'],
                            "market_price": item['marketPrice'],
                            "decimals": item.get('decimals', 2),
                            "pnl": item['pnl'],
                            "total_value": item['totalCost'],
                            "delta": float(item['disp_delta']),
                            "theta": float(item['disp_theta']),
                            "gamma": float(item['disp_gamma']),
                            "expiry": item.get('expiry', ''),
                            "dte": item.get('dte'),
                        })

                    ref_points = None
                    if g_name != '未分類(Other)':
                        if group_mute_flag.get(g_name, False):
                            print(f"🎯 [{g_name}] 🔕 缺乏報價，安全鎖啟動，強制靜音！")
                        elif abs(group_greeks[g_name]['delta']) > 0.001 or abs(group_greeks[g_name]['gamma']) > 0.001:
                            u_th = HEDGE_CONFIG[g_name]['upper_threshold']
                            l_th = HEDGE_CONFIG[g_name]['lower_threshold']
                            ref_points = (max(abs(u_th), abs(l_th)) / max(abs(group_greeks[g_name]['gamma']), 0.01)) * 100.0
                            print(
                                f"🎯 [{g_name}] 單邊估計={ref_points:.0f}點 "
                                f"🎯上限={u_th:.2f}，下限={l_th:.2f} | "
                                f"當前 {HEDGE_CONFIG[g_name]['hedge_sym']} Δ={group_greeks[g_name]['delta']:.2f} θ={group_greeks[g_name]['theta']:.0f}"
                            )

                        total_pnl = sum(p['pnl'] for p in group_positions_snapshot if p.get('pnl') is not None)
                        dashboard_groups.append({
                            "name": g_name,
                            "hedge_sym": HEDGE_CONFIG[g_name]['hedge_sym'],
                            "total_delta": group_greeks[g_name]['delta'],
                            "total_gamma": group_greeks[g_name]['gamma'],
                            "total_theta": group_greeks[g_name]['theta'],
                            "total_pnl": total_pnl,
                            "upper_threshold": HEDGE_CONFIG[g_name]['upper_threshold'],
                            "lower_threshold": HEDGE_CONFIG[g_name]['lower_threshold'],
                            "ref_points": ref_points,
                            "mute": group_mute_flag.get(g_name, False),
                            "closed": False,
                            "positions": group_positions_snapshot,
                        })
                    else:
                        total_pnl = sum(p['pnl'] for p in group_positions_snapshot if p.get('pnl') is not None)
                        dashboard_groups.append({
                            "name": g_name,
                            "hedge_sym": "-",
                            "total_delta": sum(p['delta'] for p in group_positions_snapshot),
                            "total_gamma": sum(p['gamma'] for p in group_positions_snapshot),
                            "total_theta": sum(p['theta'] for p in group_positions_snapshot),
                            "total_pnl": total_pnl,
                            "upper_threshold": None,
                            "lower_threshold": None,
                            "ref_points": None,
                            "mute": False,
                            "closed": False,
                            "positions": group_positions_snapshot,
                        })

                print("-" * 115)

                # Execute hedge
                for g_name, greeks in group_greeks.items():
                    if group_mute_flag.get(g_name, False):
                        continue
                    if abs(greeks['delta']) > 0.001 or abs(greeks['gamma']) > 0.001:
                        evaluate_and_trigger_hedge(
                            group_name=g_name,
                            total_delta=greeks['delta'],
                            total_gamma=greeks['gamma'],
                            underlying_price=greeks['underlying_price'],
                            upper_threshold=HEDGE_CONFIG[g_name]['upper_threshold'],
                            lower_threshold=HEDGE_CONFIG[g_name]['lower_threshold'],
                            hedge_symbol=HEDGE_CONFIG[g_name]['hedge_sym'],
                            hedge_qty=HEDGE_CONFIG[g_name].get('hedge_qty', 1),
                            last_hedge_times_dict=last_hedge_times,
                            cooldown_seconds=HEDGE_COOLDOWN_SECONDS,
                        )

                print("-" * 115)

                # -------------------------------------------------------------
                # ⚡ VXM 波動率期貨守門程序
                # -------------------------------------------------------------
                try:
                    evaluate_and_run_vxm(ib, execute_order=True)
                except Exception as vxm_ex:
                    print(f"⚠️ VXM 守門程序異常: {vxm_ex}")

                print("-" * 115)

            # Account status
            dashboard_account = {"net_liq": acc_info.get('net_liq', ''), "avail": acc_info.get('avail', '')}
            try:
                net_liq_value = int(float(acc_info['net_liq'].split()[0]))
                avail_value = int(float(acc_info['avail'].split()[0]))
                print(f"🏦IB淨值: {net_liq_value:,}  💰可用金: {avail_value:,}")
                dashboard_account["net_liq"] = net_liq_value
                dashboard_account["avail"] = avail_value
            except Exception:
                print(f"🏦IB淨值: {acc_info['net_liq']}  💰可用金: {acc_info['avail']}")

            # Shioaji / Taiwan futures section
            dashboard_shioaji = {}
            if api is not None:
                try:
                    margin = api.margin(api.futopt_account)
                    if margin:
                        print(f"永豐權益: {int(margin.equity_amount):,}  💰可出金: {int(margin.available_margin):,}")
                        dashboard_shioaji["equity"] = int(margin.equity_amount)
                        dashboard_shioaji["available"] = int(margin.available_margin)

                    target_code = get_futures_code("TMF")
                    contract = api.Contracts.Futures.TMF[target_code]
                    snapshots_stk = api.snapshots([contract])
                    underlying_price = snapshots_stk[0].close
                    global LATEST_TMF_PRICE; LATEST_TMF_PRICE = float(underlying_price)
                    positions = api.list_positions(api.futopt_account)

                    total_portfolio_delta_tmf = 0.0
                    total_portfolio_gamma_tmf = 0.0
                    total_portfolio_theta_tmf = 0.0
                    tmf_mute_flag = False

                    if positions:
                        df_list = []
                        for p in positions:
                            qty = p.quantity if p.direction == sj.constant.Action.Buy else -p.quantity
                            last_price = getattr(p, 'last_price', getattr(p, 'price', 0))
                            delta = 0.0
                            position_delta_tmf = 0.0
                            gamma = 0.0
                            position_gamma_tmf = 0.0
                            theta = 0.0
                            position_theta_tmf = 0.0

                            is_future = p.code.startswith(("TXF", "MTX", "TMF", "MXF"))
                            if is_future:
                                if p.code.startswith("TXF"):
                                    ratio = 20.0
                                elif p.code.startswith(("MTX", "MXF")):
                                    ratio = 5.0
                                elif p.code.startswith("TMF"):
                                    ratio = 1.0
                                else:
                                    ratio = 1.0

                                delta = 1.0
                                position_delta_tmf = delta * qty * ratio
                                total_portfolio_delta_tmf += position_delta_tmf
                            else:
                                opt_info = parse_tx_opt_code(p.code)
                                if opt_info:
                                    strike, cp, dte = opt_info
                                    T = dte / 365.0
                                    if last_price > 0:
                                        try:
                                            tmf_diff = get_tmf_price_diff()
                                            eff_f = underlying_price + tmf_diff
                                            delta, theta, gamma = calculate_futures_option_greeks(
                                                F=eff_f,
                                                K=strike,
                                                T=T,
                                                r=0.01,
                                                option_price=last_price,
                                                option_type=cp,
                                            )
                                            ratio = 5.0
                                            position_delta_tmf = delta * qty * ratio
                                            position_gamma_tmf = gamma * qty * ratio * 100.0
                                            position_theta_tmf = theta * qty * 50.0
                                            total_portfolio_delta_tmf += position_delta_tmf
                                            total_portfolio_gamma_tmf += position_gamma_tmf
                                            total_portfolio_theta_tmf += position_theta_tmf
                                        except Exception:
                                            delta = 0.0
                                    else:
                                        tmf_mute_flag = True

                            sj_dte = None
                            if not is_future:
                                opt_info = parse_tx_opt_code(p.code)
                                if opt_info:
                                    sj_dte = int(round(opt_info[2]))

                            df_list.append({
                                "code": p.code,
                                "direction": p.direction.name,
                                "qty": p.quantity,
                                "now": format_price(last_price, 0),
                                "pnl": format_price(p.pnl, 0),
                                "Δ": f"{position_delta_tmf:.5f}",
                                "γ": f"{position_gamma_tmf:.5f}",
                                "θ": f"{position_theta_tmf:.0f}",
                                "dte": sj_dte,
                            })

                        print(pd.DataFrame(df_list).to_string(index=False))
                        tmf_config = HEDGE_CONFIG['台指(TMF)']
                        u_th = tmf_config['upper_threshold']
                        l_th = tmf_config['lower_threshold']
                        current_time_str = datetime.datetime.now().strftime('%H:%M')
                        is_tw_closed = ("04:59" < current_time_str < "08:46" or "13:44" < current_time_str < "15:01")

                        tmf_ref_points = None
                        if tmf_mute_flag:
                            print("🎯 [台指(TMF)] 🔕 缺乏期權現價，安全鎖啟動，強制靜音暫停對沖！")
                        elif is_tw_closed:
                            print("⏳ [台指(TMF)] 沒開盤！暫停對沖。")
                        else:
                            tmf_ref_points = (max(abs(u_th), abs(l_th)) / max(abs(total_portfolio_gamma_tmf), 0.01)) * 100.0
                            print(
                                f"🎯 [台指(TMF)] 單邊門檻估計={tmf_ref_points:.0f}點 "
                                f"🎯上限={u_th:.2f}，下限={l_th:.2f} "
                                f"🎯當前 TMF Δ={total_portfolio_delta_tmf:.2f} θ={total_portfolio_theta_tmf:.0f}"
                            )
                            evaluate_and_trigger_hedge(
                                group_name='台指(TMF)',
                                total_delta=total_portfolio_delta_tmf,
                                total_gamma=total_portfolio_gamma_tmf,
                                underlying_price=underlying_price,
                                upper_threshold=u_th,
                                lower_threshold=l_th,
                                hedge_symbol=tmf_config['hedge_sym'],
                                hedge_qty=tmf_config.get('hedge_qty', 1),
                                last_hedge_times_dict=last_hedge_times,
                                cooldown_seconds=HEDGE_COOLDOWN_SECONDS,
                            )

                        tmf_total_pnl = sum(float(str(d.get("pnl", 0)).replace(",", "")) for d in df_list if d.get("pnl"))
                        dashboard_groups.append({
                            "name": "台指(TMF)",
                            "hedge_sym": tmf_config['hedge_sym'],
                            "total_delta": total_portfolio_delta_tmf,
                            "total_gamma": total_portfolio_gamma_tmf,
                            "total_theta": total_portfolio_theta_tmf,
                            "total_pnl": tmf_total_pnl,
                            "upper_threshold": u_th,
                            "lower_threshold": l_th,
                            "ref_points": tmf_ref_points,
                            "mute": tmf_mute_flag,
                            "closed": is_tw_closed,
                            "positions": [
                                {
                                    "symbol": d["code"],
                                    "position": d["qty"] if d["direction"] == "Buy" else -d["qty"],
                                    "avg_cost": None,
                                    "market_price": d["now"],
                                    "decimals": 0,
                                    "pnl": d["pnl"],
                                    "total_value": None,
                                    "delta": float(d["Δ"]),
                                    "theta": float(d["θ"]),
                                    "gamma": float(d["γ"]),
                                    "expiry": "",
                                    "dte": d.get("dte"),
                                }
                                for d in df_list
                            ],
                        })
                    else:
                        tmf_config = HEDGE_CONFIG.get('台指(TMF)', {})
                        if tmf_config:
                            dashboard_groups.append({
                                "name": "台指(TMF)",
                                "hedge_sym": tmf_config.get('hedge_sym', 'TMF'),
                                "total_delta": 0.0,
                                "total_gamma": 0.0,
                                "total_theta": 0.0,
                                "total_pnl": 0.0,
                                "upper_threshold": tmf_config.get('upper_threshold', 1.0),
                                "lower_threshold": tmf_config.get('lower_threshold', -0.5),
                                "ref_points": None,
                                "mute": False,
                                "closed": True,
                                "positions": [],
                            })
                except Exception as e:
                    print(f"⚠️ 永豐/台指區段略過: {e}")

            # --- 彙整並更新手機面板快照 ---
            total_all_theta = 0.0
            for g in dashboard_groups:
                if g['name'] != '台指(TMF)':
                    total_all_theta += g.get('total_theta', 0.0)
            dashboard_account["total_theta"] = total_all_theta
            
            opt_pos = []
            for p in (portfolio_data or []):
                if p.get('secType') in ['OPT', 'FOP']:
                    action = 'BUY' if p.get('position', 0) < 0 else 'SELL'
                    opt_pos.append({
                        'conId': p.get('conId', getattr(p.get('contract'), 'conId', 0)),
                        'symbol': p.get('symbol', ''),
                        'localSymbol': p.get('localSymbol', ''),
                        'position': p.get('position', 0),
                        'marketPrice': p.get('marketPrice', 0.0),
                        'action': action
                    })

            update_snapshot({
                "updated_at": now_dt.strftime('%Y-%m-%d %H:%M:%S'),
                "account": dashboard_account,
                "shioaji": dashboard_shioaji,
                "orders": dashboard_orders,
                "fills": dashboard_fills,
                "groups": dashboard_groups,
                "options_positions": opt_pos,
                "send_webhook": SEND_WEBHOOK,
                "vxm": VXM_CURRENT_DATA,
            })

            print(f"最後更新: {now_dt.strftime('%H:%M:%S')} | 下次更新: {REFRESH_SECONDS}秒後")
            ib.sleep(REFRESH_SECONDS)

        except KeyboardInterrupt:
            print("\n程式手動中斷。")
            if ib.isConnected():
                ib.disconnect()
            break
        except Exception as e:
            print(f"發生錯誤: {e}")
            if ib.isConnected():
                ib.disconnect()
            time.sleep(10)


if __name__ == '__main__':
    main()
