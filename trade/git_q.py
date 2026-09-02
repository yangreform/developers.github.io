
import os
import datetime
import time
import threading
import calendar
import random
import math
import sys
import json

import pandas as pd
import requests
from scipy.stats import norm
import scipy.optimize as optimize
from dotenv import load_dotenv, find_dotenv, set_key

from ib_insync import *
import shioaji as sj

# ==============================================================================
# 🔐 Load .env
# ==============================================================================
load_dotenv()

# 手機面板改的參數要寫回這個檔案，這樣重開 q.py 才不會消失
DOTENV_PATH = find_dotenv()
if not DOTENV_PATH:
    DOTENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


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
RESTART_INTERVAL = env_int("RESTART_INTERVAL", 3600)
IB_GREEKS_WAIT_SECONDS = env_float("IB_GREEKS_WAIT_SECONDS", 1.5)

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
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>對沖監控面板</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 12px 12px 80px;
    background: #0d1117; color: #e6edf3;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 13px;
  }
  h1 { font-size: 16px; margin: 4px 0 12px; }
  .updated { color: #8b949e; font-size: 11px; margin-bottom: 12px; }
  .card {
    background: #161b22; border: 1px solid #30363d; border-radius: 10px;
    padding: 12px; margin-bottom: 12px;
  }
  .card h2 { font-size: 14px; margin: 0 0 8px; color: #58a6ff; }
  .row { display: flex; justify-content: space-between; padding: 3px 0; font-size: 12px; }
  .row span:first-child { color: #8b949e; }
  table { width: 100%; border-collapse: collapse; font-size: 11px; }
  th, td { text-align: right; padding: 4px 3px; border-bottom: 1px solid #21262d; white-space: nowrap; }
  th:first-child, td:first-child { text-align: left; }
  th { color: #8b949e; font-weight: 500; }
  .pos { color: #f85149; }
  .neg { color: #3fb950; }
  .muted { color: #d29922; font-size: 11px; margin-top: 6px; }
  .group-title { display:flex; justify-content:space-between; align-items:center; }
  .badge { font-size: 10px; padding: 2px 6px; border-radius: 6px; background:#21262d; color:#8b949e; }
  .form-row { display:flex; gap:6px; margin-top:8px; }
  input[type=number], input[type=password] {
    flex: 1; background:#0d1117; border:1px solid #30363d; color:#e6edf3;
    border-radius:6px; padding:8px; font-size:12px; width:100%;
  }
  button {
    background:#238636; color:#fff; border:none; border-radius:6px;
    padding:8px 12px; font-size:12px;
  }
  button:active { background:#2ea043; }
  .toast {
    position:fixed; bottom:16px; left:50%; transform:translateX(-50%);
    background:#238636; color:#fff; padding:8px 16px; border-radius:8px;
    font-size:12px; opacity:0; transition:opacity .3s; pointer-events:none;
  }
  .toast.show { opacity:1; }
  .refresh-note { position:fixed; top:8px; right:12px; font-size:10px; color:#8b949e; }
  .switch-row { display:flex; justify-content:space-between; align-items:center; }
  .switch { position:relative; display:inline-block; width:46px; height:26px; flex-shrink:0; }
  .switch input { opacity:0; width:0; height:0; }
  .slider {
    position:absolute; cursor:pointer; inset:0;
    background:#30363d; transition:.2s; border-radius:26px;
  }
  .slider:before {
    position:absolute; content:""; height:20px; width:20px; left:3px; bottom:3px;
    background:#e6edf3; transition:.2s; border-radius:50%;
  }
  .switch input:checked + .slider { background:#238636; }
  .switch input:checked + .slider:before { transform:translateX(20px); }
  .webhook-desc { font-size:11px; color:#8b949e; margin-top:4px; }
</style>
</head>
<body>
  <h1>📊 對沖監控面板</h1>
  <div class="updated" id="updated">載入中...</div>
  <div class="card">
    <div class="switch-row">
      <div>
        <h2 style="margin:0;">🚨 自動對沖送單</h2>
        <div class="webhook-desc">關閉後，偵測到 Delta 偏移只會印出訊號，不會實際下單</div>
      </div>
      <label class="switch">
        <input type="checkbox" id="webhookToggle" onchange="toggleWebhook(this.checked)">
        <span class="slider"></span>
      </label>
    </div>
  </div>
  <div id="groups"></div>
  <div id="account" class="card"></div>
  <div class="toast" id="toast"></div>

<script>
const PASSWORD_KEY = "dashboard_pw";

function fmt(n, d=2) {
  if (n === null || n === undefined || isNaN(n)) return "-";
  return Number(n).toLocaleString(undefined, {minimumFractionDigits:d, maximumFractionDigits:d});
}
function cls(n) { return Number(n) > 0 ? "pos" : (Number(n) < 0 ? "neg" : ""); }

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
      pw = prompt("請輸入監控密碼：") || "";
      localStorage.setItem(PASSWORD_KEY, pw);
      return submitThreshold(groupName);
    }
    if (data.status === "ok") {
      showToast("✅ 已更新 " + groupName);
    } else {
      showToast("❌ " + (data.message || "更新失敗"));
    }
  } catch (e) {
    showToast("❌ 連線失敗");
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
      pw = prompt("請輸入監控密碼：") || "";
      localStorage.setItem(PASSWORD_KEY, pw);
      webhookToggleBusy = false;
      return toggleWebhook(enabled);
    }
    const data = await resp.json();
    if (data.status === "ok") {
      showToast(data.send_webhook ? "✅ 自動送單已開啟" : "🧪 自動送單已關閉（僅印出訊號）");
    } else {
      toggleEl.checked = !enabled;
      showToast("❌ " + (data.message || "更新失敗"));
    }
  } catch (e) {
    toggleEl.checked = !enabled;
    showToast("❌ 連線失敗");
  } finally {
    webhookToggleBusy = false;
  }
}

function renderPositions(rows) {
  if (!rows || !rows.length) return "";
  let html = '<table><tr><th>商品</th><th>部位</th><th>現價</th><th>損益</th><th>Δ</th><th>θ</th><th>γ</th></tr>';
  for (const r of rows) {
    html += `<tr>
      <td>${r.symbol}</td>
      <td class="${cls(r.position)}">${fmt(r.position,1)}</td>
      <td>${fmt(r.market_price, r.decimals ?? 2)}</td>
      <td class="${cls(r.pnl)}">${fmt(r.pnl,2)}</td>
      <td>${fmt(r.delta,4)}</td>
      <td>${fmt(r.theta,0)}</td>
      <td>${fmt(r.gamma,4)}</td>
    </tr>`;
  }
  html += "</table>";
  return html;
}

function renderGroup(g) {
  return `
  <div class="card">
    <div class="group-title">
      <h2>${g.name}</h2>
      <span class="badge">${g.hedge_sym}</span>
    </div>
    ${g.closed ? '<div class="muted">⏳ 未開盤，暫停對沖</div>' : ''}
    ${g.mute
      ? `<div class="row"><span class="muted">🔕 缺乏報價，安全鎖啟動</span></span></div>`
      : `<div class="row"><span>目前 Δ ${fmt(g.total_delta,3)}</span><span>目前 θ ${fmt(g.total_theta,0)}</span><span>單邊估計點數 ${fmt(g.ref_points,0)}</span></div>`
    }
    ${renderPositions(g.positions)}
    <div class="form-row">
      <input type="number" step="0.1" id="u_${g.name}" placeholder="上限 (目前 ${fmt(g.upper_threshold,2)})">
      <input type="number" step="0.1" id="l_${g.name}" placeholder="下限 (目前 ${fmt(g.lower_threshold,2)})">
      <button onclick="submitThreshold('${g.name}')">送出</button>
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
    let accHtml = "<h2>帳戶</h2>";
    accHtml += `<div class="row"><span>IB 淨值</span><span>${acc.net_liq ?? "-"}</span></div>`;
    accHtml += `<div class="row"><span>IB 可用金</span><span>${acc.avail ?? "-"}</span></div>`;
    accHtml += `<div class="row"><span>全帳戶 θ 加總</span><span>${fmt(acc.total_theta, 0)}</span></div>`;
    if (shioaji && shioaji.equity !== undefined) {
      accHtml += `<div class="row"><span>永豐權益</span><span>${fmt(shioaji.equity,0)}</span></div>`;
      accHtml += `<div class="row"><span>永豐可出金</span><span>${fmt(shioaji.available,0)}</span></div>`;
    }
    document.getElementById("account").innerHTML = accHtml;

    let groupsHtml = "";
    for (const g of (data.groups || [])) {
      groupsHtml += renderGroup(g);
    }
    document.getElementById("groups").innerHTML = groupsHtml || '<div class="card">目前無持倉資料</div>';
  } catch (e) {
    document.getElementById("updated").textContent = "⚠️ 讀取失敗，重試中...";
  }
}

refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>
"""


@dash_app.route("/dashboard", methods=["GET"])
def dashboard_page():
    return Response(DASHBOARD_HTML, mimetype="text/html")


@dash_app.route("/api/snapshot", methods=["GET"])
def api_snapshot():
    return jsonify(get_snapshot())


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


def start_dashboard_server():
    """在背景執行緒啟動 Flask 監控面板，不影響主邏輯。"""
    from waitress import serve as _serve
    print(f"📱 手機監控面板已啟動：http://0.0.0.0:{DASHBOARD_PORT}/dashboard")
    _serve(dash_app, host="0.0.0.0", port=DASHBOARD_PORT, threads=4)


# ==============================================================================
# 🌟 Auto Delta Hedge Sender
# ==============================================================================
def trigger_delta_hedge(action: str, current_price: float, symbol: str, qty: int | float) -> bool:
    if not WEBHOOK_URL or not WEBHOOK_PASSPHRASE:
        print(f"[{symbol} 自動對沖系統] ⚠️ WEBHOOK_URL 或 WEBHOOK_PASSPHRASE 未設定，只印出訊號不送單。")
        print(f" -> 動作: {action} {qty} 單位 {symbol} @ {current_price}\n")
        return False

    payload = {
        "passphrase": WEBHOOK_PASSPHRASE,
        "symbol": symbol,
        "action": action,
        "quantity": str(qty),
        "price": str(current_price),
        "strategy_name": "delta_hedge",
    }

    try:
        print(f"[{symbol} 自動對沖系統] 🚨 偵測到 Delta 偏移！準備發送 Webhook 訊號...")
        print(f" -> 動作: {action} {qty} 單位 {symbol} @ {current_price}\n")

        if SEND_WEBHOOK:
            response = requests.post(WEBHOOK_URL, json=payload, timeout=10)
            if response.status_code == 200:
                print(f"[{symbol} 自動對沖系統] ✅ Webhook 傳送成功: {response.text}")
                return True
            print(f"[{symbol} 自動對沖系統] ❌ Webhook 傳送失敗, 狀態碼: {response.status_code}, body={response.text}")
            return False

        print(f"[{symbol} 自動對沖系統] 🧪 SEND_WEBHOOK=False，目前為測試模式，未實際送出。")
        return True

    except Exception as e:
        print(f"[{symbol} 自動對沖系統] ❌ Webhook 請求發生異常: {e}")
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
        else:
            delta = float(comp.delta)
            gamma = float(comp.gamma)
            theta = float(comp.theta)

        if not (_finite_number(delta) and _finite_number(gamma) and _finite_number(theta)):
            return None

        return delta, theta, gamma, source

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
    if underlying_price <= 0:
        return

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
        })

    return results


# ==============================================================================
# Shioaji login
# ==============================================================================
def init_shioaji():
    global api
    if not SHIOAJI_API_KEY or not SHIOAJI_SECRET_KEY:
        print("⚠️ Shioaji API key/secret 未設定，略過永豐登入。")
        return None

    api = sj.Shioaji()
    api.login(SHIOAJI_API_KEY, SHIOAJI_SECRET_KEY)

    if SHIOAJI_CA_PATH and SHIOAJI_CA_PASSWD:
        api.activate_ca(ca_path=SHIOAJI_CA_PATH, ca_passwd=SHIOAJI_CA_PASSWD)
    else:
        print("⚠️ Shioaji CA path/password 未設定，若需下單請補上。")

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
                                        d, t, g, greek_source = ib_greeks
                                        item['greek_source'] = greek_source

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
                                        d, t, g, greek_source = ib_greeks
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

                        dashboard_groups.append({
                            "name": g_name,
                            "hedge_sym": HEDGE_CONFIG[g_name]['hedge_sym'],
                            "total_delta": group_greeks[g_name]['delta'],
                            "total_gamma": group_greeks[g_name]['gamma'],
                            "total_theta": group_greeks[g_name]['theta'],
                            "upper_threshold": HEDGE_CONFIG[g_name]['upper_threshold'],
                            "lower_threshold": HEDGE_CONFIG[g_name]['lower_threshold'],
                            "ref_points": ref_points,
                            "mute": group_mute_flag.get(g_name, False),
                            "closed": False,
                            "positions": group_positions_snapshot,
                        })
                    else:
                        dashboard_groups.append({
                            "name": g_name,
                            "hedge_sym": "-",
                            "total_delta": sum(p['delta'] for p in group_positions_snapshot),
                            "total_gamma": sum(p['gamma'] for p in group_positions_snapshot),
                            "total_theta": sum(p['theta'] for p in group_positions_snapshot),
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
                                            delta, theta, gamma = calculate_futures_option_greeks(
                                                F=underlying_price,
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

                            df_list.append({
                                "code": p.code,
                                "direction": p.direction.name,
                                "qty": p.quantity,
                                "now": format_price(last_price, 0),
                                "pnl": format_price(p.pnl, 0),
                                "Δ": f"{position_delta_tmf:.5f}",
                                "γ": f"{position_gamma_tmf:.5f}",
                                "θ": f"{position_theta_tmf:.0f}",
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

                        dashboard_groups.append({
                            "name": "台指(TMF)",
                            "hedge_sym": tmf_config['hedge_sym'],
                            "total_delta": total_portfolio_delta_tmf,
                            "total_gamma": total_portfolio_gamma_tmf,
                            "total_theta": total_portfolio_theta_tmf,
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
                                }
                                for d in df_list
                            ],
                        })
                except Exception as e:
                    print(f"⚠️ 永豐/台指區段略過: {e}")

            # --- 彙整並更新手機面板快照 ---
            total_all_theta = 0.0
            for g in dashboard_groups:
                if g['name'] != '台指(TMF)':
                    total_all_theta += g.get('total_theta', 0.0)
            dashboard_account["total_theta"] = total_all_theta
            
            update_snapshot({
                "updated_at": now_dt.strftime('%Y-%m-%d %H:%M:%S'),
                "account": dashboard_account,
                "shioaji": dashboard_shioaji,
                "orders": dashboard_orders,
                "fills": dashboard_fills,
                "groups": dashboard_groups,
                "send_webhook": SEND_WEBHOOK,
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
