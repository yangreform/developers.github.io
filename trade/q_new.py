
import os
import datetime
import time
import threading
import calendar
import random
import math
import sys
import json
import glob

import pandas as pd
import requests
from scipy.stats import norm
import scipy.optimize as optimize
from dotenv import load_dotenv, find_dotenv, set_key

from ib_insync import *
import shioaji as sj

# ==============================================================================
#  Load .env
# ==============================================================================
load_dotenv()

# 璈Ｘ踵寧貉撖怠瑼獢嚗璅 q.py 銝瘨憭
DOTENV_PATH = find_dotenv()
if not DOTENV_PATH:
    DOTENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def persist_env_var(key: str, value) -> bool:
    """璈Ｘ踵寧詨神 .env嚗霈 q.py 敺閮敺憭望芸啗郎嚗銝銝剜瑞撘"""
    try:
        set_key(DOTENV_PATH, key, str(value))
        return True
    except Exception as e:
        print(f"儭 ⊥撖怠 .env ({key}={value}): {e}")
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
# 儭 Login and settings
# ==============================================================================
IB_PORT = env_int("IB_PORT", 4001)
IB_HOST = env_str("IB_HOST", "127.0.0.1")
IB_CLIENT_ID = env_int("IB_CLIENT_ID", random.randint(1, 9999))
TARGET_ACCOUNT = env_str("IB_TARGET_ACCOUNT", required=True)
REFRESH_SECONDS = env_int("REFRESH_SECONDS", 300)
RESTART_INTERVAL = env_int("RESTART_INTERVAL", 3600)
IB_GREEKS_WAIT_SECONDS = env_float("IB_GREEKS_WAIT_SECONDS", 1.5)

#  璈折Ｘ輯身摰
DASHBOARD_PORT = env_int("DASHBOARD_PORT", 5800)
DASHBOARD_PASSWORD = env_str("DASHBOARD_PASSWORD", required=False)  # 蝛 = 銝撽霅撖蝣潘撱箄降典蝬脣找蝙剁
BARCHART_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Barchart")

# ------------------------------------------------------------------------------
#  HEDGE_CONFIG / SEND_WEBHOOK 摰典 .env 霈
# 璈Ｘ踵寥潭游撖怠 .env  HEDGE_CONFIG_JSON嚗 q.py 敺銋銝瘨憭
# ------------------------------------------------------------------------------
HEDGE_COOLDOWN_SECONDS = 60 * 5


def serialize_hedge_config(config: dict) -> str:
    """ HEDGE_CONFIG 頧臭誑撖恍 .env 株 JSON 摮銝莎symbols  list嚗"""
    plain = {name: {**info, "symbols": list(info["symbols"])} for name, info in config.items()}
    return json.dumps(plain, ensure_ascii=False)


def load_hedge_config() -> dict:
    """敺 .env  HEDGE_CONFIG_JSON 霈閮剖嚗 .env 芾身摰粹航炊"""
    raw = os.getenv("HEDGE_CONFIG_JSON", "")
    if not raw:
        raise RuntimeError(
            " .env 蝻箏 HEDGE_CONFIG_JSON嚗\n"
            "隢 .env 銝剖 HEDGE_CONFIG_JSON='...' 敺"
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"HEDGE_CONFIG_JSON 澆航炊嚗隢瑼Ｘ .env: {e}")

    # symbols 銝摰閬 tuple嚗蝔撘嗡唳寧 str.startswith(tuple) 斗瑞黎蝯
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

ib = IB()
api = None

# ==============================================================================
#  璈折Ｘ (Flask Dashboard)
# ==============================================================================
from flask import Flask, request, jsonify, Response

dash_app = Flask(__name__)
dash_ib = IB()
SNAPSHOT_LOCK = threading.Lock()
LATEST_SNAPSHOT = {
    "updated_at": None,
    "account": {},
    "shioaji": {},
    "orders": [],
    "fills": [],
    "groups": [],
    "send_webhook": SEND_WEBHOOK,
    "note": "撠芸敺隞颱鞈嚗隢蝔...",
}


def update_snapshot(new_data: dict) -> None:
    """瑁蝺摰典唳湔唳啣翰改靘 Dashboard 霈"""
    with SNAPSHOT_LOCK:
        LATEST_SNAPSHOT.clear()
        LATEST_SNAPSHOT.update(new_data)


def get_snapshot() -> dict:
    with SNAPSHOT_LOCK:
        return dict(LATEST_SNAPSHOT)


def check_dashboard_auth(payload: dict) -> bool:
    """交閮剖 DASHBOARD_PASSWORD嚗閬瘙 payload 批葆甇蝣箏蝣潭賭耨孵詻"""
    if not DASHBOARD_PASSWORD:
        return True
    return payload.get("password") == DASHBOARD_PASSWORD


DASHBOARD_HTML = """

<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>撠????Ｘ</title>
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

        
        
        .tabs { overflow: hidden; border-bottom: 1px solid #30363d; margin-bottom: 20px; }
        .tabs button { background-color: inherit; float: left; border: none; outline: none; cursor: pointer; padding: 10px 20px; transition: 0.3s; color: #8b949e; font-size: 16px; border-bottom: 2px solid transparent; }
        .tabs button:hover { color: #c9d1d9; }
        .tabs button.active { color: #58a6ff; border-bottom: 2px solid #58a6ff; }
        .tabcontent { display: none; }
        table { width: 100%; border-collapse: collapse; margin-top: 15px; font-size: 14px; }
        th, td { border: 1px solid #30363d; padding: 8px; text-align: left; }
        th { background-color: #161b22; color: #c9d1d9; }
        tr:nth-child(even) { background-color: #0d1117; }
        tr:nth-child(odd) { background-color: #161b22; }
        button.action-btn { background-color: #238636; color: white; border: none; padding: 6px 12px; border-radius: 6px; cursor: pointer; }
        button.action-btn:hover { background-color: #2ea043; }
        button.danger-btn { background-color: #da3633; color: white; border: none; padding: 6px 12px; border-radius: 6px; cursor: pointer; }
        button.danger-btn:hover { background-color: #f85149; }
        .loading { color: #8b949e; font-style: italic; }
    
</style>
</head>
<body>

<div class="tabs">
    <button class="tablinks active" onclick="openTab(event, 'overview')">Overview (Delta Hedge)</button>
    <button class="tablinks" onclick="openTab(event, \'barchart\')">Barchart (Bull Put)</button>
    <button class="tablinks" onclick="openTab(event, 'portfolio')">Portfolio (撟喳?</button>
</div>

<div id="overview" class="tabcontent" style="display:block;">

  <h1>?? 撠????Ｘ</h1>
  <div class="updated" id="updated">頛銝?..</div>
  <div class="card">
    <div class="switch-row">
      <div>
        <h2 style="margin:0;">? ?芸?撠??</h2>
        <div class="webhook-desc">??敺??菜葫??Delta ?宏?芣??啣閮?嚗??祕????/div>
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
</div>

    
    
    <div id="barchart" class="tabcontent">
        <h3>Barchart Bull Put Spread</h3>
        <button class="action-btn" onclick="loadBarchartData()">Refresh Data</button>
        <div id="barchart_content" class="loading">Loading...</div>
    </div>

    <div id="portfolio" class="tabcontent">
        <h3>?桀? Options ?其?</h3>
        <button class="action-btn" onclick="loadPortfolio()">Refresh Portfolio</button>
        <div id="portfolio_content" class="loading">Loading...</div>
    </div>

    


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
      pw = prompt("隢撓?亦?批?蝣潘?") || "";
      localStorage.setItem(PASSWORD_KEY, pw);
      return submitThreshold(groupName);
    }
    if (data.status === "ok") {
      showToast("??撌脫??" + groupName);
    } else {
      showToast("??" + (data.message || "?湔憭望?"));
    }
  } catch (e) {
    showToast("?????憭望?");
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
      pw = prompt("隢撓?亦?批?蝣潘?") || "";
      localStorage.setItem(PASSWORD_KEY, pw);
      webhookToggleBusy = false;
      return toggleWebhook(enabled);
    }
    const data = await resp.json();
    if (data.status === "ok") {
      showToast(data.send_webhook ? "???芸??撌脤??? : "?妒 ?芸??撌脤?????箄???");
    } else {
      toggleEl.checked = !enabled;
      showToast("??" + (data.message || "?湔憭望?"));
    }
  } catch (e) {
    toggleEl.checked = !enabled;
    showToast("?????憭望?");
  } finally {
    webhookToggleBusy = false;
  }
}

function renderPositions(rows) {
  if (!rows || !rows.length) return "";
  let html = '<table><tr><th>??</th><th>?其?</th><th>?曉</th><th>??</th><th>?</th><th>庛</th><th>擗</th></tr>';
  for (const r of rows) {
    html += `<tr>
      <td>${r.symbol}</td>
      <td class="${cls(r.position)}">${fmt(r.position,1)}</td>
      <td>${fmt(r.market_price, r.decimals ?? 2)}</td>
      <td class="${cls(r.pnl)}">${fmt(r.pnl,2)}</td>
      <td>${fmt(r.delta,4)}</td>
      <td>${fmt(r.theta,0)}</td>
      <td>${r.dte !== undefined && r.dte !== "" ? r.dte : (r.expiry || "")}</td>
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
    ${g.closed ? '<div class="muted">???芷??歹??怠?撠?</div>' : ''}
    ${g.mute
      ? `<div class="row"><span class="muted">?? 蝻箔??勗嚗??券???</span></span></div>`
      : `<div class="row"><span>?桀? ? ${fmt(g.total_delta,3)}</span><span>?桀? 庛 ${fmt(g.total_theta,0)}</span><span>?桅?隡啗?暺 ${fmt(g.ref_points,0)}</span></div>`
    }
    ${renderPositions(g.positions)}
    <div class="form-row">
      <input type="number" step="0.1" id="u_${g.name}" placeholder="銝? (?桀? ${fmt(g.upper_threshold,2)})">
      <input type="number" step="0.1" id="l_${g.name}" placeholder="銝? (?桀? ${fmt(g.lower_threshold,2)})">
      <button onclick="submitThreshold('${g.name}')">?</button>
    </div>
  </div>`;
}

async function refresh() {
  try {
    const resp = await fetch("/api/snapshot");
    const data = await resp.json();
    document.getElementById("updated").textContent = "?敺?? " + (data.updated_at || "-");

    const toggleEl = document.getElementById("webhookToggle");
    if (!webhookToggleBusy && data.send_webhook !== undefined) {
      toggleEl.checked = !!data.send_webhook;
    }

    const acc = data.account || {};
    const shioaji = data.shioaji || {};
    let accHtml = "<h2>撣單</h2>";
    accHtml += `<div class="row"><span>IB 瘛典?/span><span>${acc.net_liq ?? "-"}</span></div>`;
    accHtml += `<div class="row"><span>IB ?舐??/span><span>${acc.avail ?? "-"}</span></div>`;
    accHtml += `<div class="row"><span>?典董??庛 ?蜇</span><span>${fmt(acc.total_theta, 0)}</span></div>`;
    if (shioaji && shioaji.equity !== undefined) {
      accHtml += `<div class="row"><span>瘞貉?甈?</span><span>${fmt(shioaji.equity,0)}</span></div>`;
      accHtml += `<div class="row"><span>瘞貉??臬??/span><span>${fmt(shioaji.available,0)}</span></div>`;
    }
    document.getElementById("account").innerHTML = accHtml;

    let groupsHtml = "";
    for (const g of (data.groups || [])) {
      groupsHtml += renderGroup(g);
    }
    document.getElementById("groups").innerHTML = groupsHtml || '<div class="card">?桀??⊥?????/div>';
  } catch (e) {
    document.getElementById("updated").textContent = "?? 霈?仃???岫銝?..";
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
            
            if(tabName === 'portfolio') {
                loadPortfolio();
            } else {
                if(tabName === 'barchart') loadBarchartData(); else loadData(tabName);
            }
        }

        async function loadData(type) {
            document.getElementById(type + '_content').innerHTML = "Loading data... (IBKR ???航?閬嗾??嚗???蝑?)";
            try {
                const response = await fetch('/api/option/data?type=' + type);
                const data = await response.json();
                
                if (data.status === 'error') {
                    document.getElementById(type + '_content').innerHTML = `<span style="color:red;">Error: ${data.message}</span>`;
                    return;
                }
                
                if (!data.data || data.data.length === 0) {
                    document.getElementById(type + '_content').innerHTML = "No candidates found.";
                    return;
                }

                let html = '<table><tr>';
                const keys = Object.keys(data.data[0]);
                for (let k of keys) {
                    html += `<th>${k}</th>`;
                }
                html += '<th>Action</th></tr>';

                data.data.forEach(row => {
                    html += '<tr>';
                    for (let k of keys) {
                        html += `<td>${row[k]}</td>`;
                    }
                    html += `<td><button class="action-btn" onclick="trade('${row.Symbol}', '${type}')">Trade</button></td>`;
                    html += '</tr>';
                });
                html += '</table>';
                document.getElementById(type + '_content').innerHTML = html;

            } catch (err) {
                document.getElementById(type + '_content').innerHTML = `<span style="color:red;">Failed to fetch data: ${err}</span>`;
            }
        }

        async function loadPortfolio() {
            document.getElementById('portfolio_content').innerHTML = "Loading portfolio...";
            try {
                const response = await fetch('/api/option/portfolio');
                const data = await response.json();
                
                if (data.status === 'error') {
                    document.getElementById('portfolio_content').innerHTML = `<span style="color:red;">Error: ${data.message}</span>`;
                    return;
                }
                
                if (!data.positions || data.positions.length === 0) {
                    document.getElementById('portfolio_content').innerHTML = "No options positions found.";
                    return;
                }

                let html = '<table><tr><th>Symbol</th><th>Local Symbol</th><th>Position</th><th>Market Price</th><th>Action</th></tr>';
                data.positions.forEach(p => {
                    html += `<tr>
                        <td>${p.symbol}</td>
                        <td>${p.localSymbol}</td>
                        <td style="color:${p.position > 0 ? '#2ea043' : '#f85149'}">${p.position}</td>
                        <td>${p.marketPrice}</td>
                        <td><button class="danger-btn" onclick="closePosition('${p.conId}', '${p.action}', ${Math.abs(p.position)})">Close (MKT)</button></td>
                    </tr>`;
                });
                html += '</table>';
                
                const txoResp = await fetch('/api/option/txo_strangles');
                const txoData = await txoResp.json();
                let txoHtml = '<h3>Shioaji TXO ATM Strangles (Delta ~0.5 / -0.5)</h3>';
                if(txoData.status === 'ok' && txoData.data && txoData.data.length > 0) {
                    txoHtml += '<table><tr><th>Symbol</th><th>Delivery Date</th><th>ATM Strike</th><th>Action</th></tr>';
                    txoData.data.forEach(t => {
                        txoHtml += `<tr>
                            <td>${t.symbol}</td>
                            <td>${t.delivery_date}</td>
                            <td>${t.atm_strike}</td>
                            <td><button class="primary-btn" onclick="tradeTxoStrangle('${t.call_code}', '${t.put_code}')">銝 (MKT)</button></td>
                        </tr>`;
                    });
                    txoHtml += '</table>';
                } else {
                    txoHtml += '<p>No Shioaji TXO data found or not connected.</p>';
                }
                document.getElementById('portfolio_content').innerHTML = html + txoHtml;


            } catch (err) {
                document.getElementById('portfolio_content').innerHTML = `<span style="color:red;">Failed to fetch portfolio: ${err}</span>`;
            }
        }

        async function trade(symbol, type) {
            if(!confirm(`蝣箏?閬 ${symbol} ?撣?桀?嚗)) return;
            try {
                const response = await fetch('/api/option/trade', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ symbol: symbol, strategy: type })
                });
                const result = await response.json();
                alert(result.message);
            } catch (err) {
                alert("Trade failed: " + err);
            }
        }

        async function closePosition(conId, action, qty) {
            if(!confirm(`蝣箏?閬撟喳 (${action} ${qty}) ?? (撠蝙??Adaptive Patient 撣??`)) return;
            try {
                const response = await fetch('/api/option/close', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ conId: conId, action: action, quantity: qty })
                });
                const result = await response.json();
                alert(result.message);
                loadPortfolio();
            } catch (err) {
                alert("Close failed: " + err);
            }
        }
        
        window.onload = () => { loadData('iv_rank'); };
    

async function loadBarchartData() {
    document.getElementById('barchart_content').innerHTML = '<div class="loading">Fetching data...</div>';
    try {
        const response = await fetch('/api/option/data_bull_put?t=' + Date.now());
        const data = await response.json();
        
        if (data.status === 'error') {
            document.getElementById('barchart_content').innerHTML = `<span style="color:red;">Error: ${data.message}</span>`;
            return;
        }
        
        if (!data.data || data.data.length === 0) {
            document.getElementById('barchart_content').innerHTML = "No candidates found.";
            return;
        }

        let html = '<div style="overflow-x:auto;"><table><tr><th>Symbol</th><th>Exp Date</th><th>Leg1 Strike</th><th>Leg2 Strike</th><th>Max Profit</th><th>Action</th></tr>';
        data.data.forEach(row => {
            html += `<tr>
                <td>${row.Symbol}</td>
                <td>${row.Exp_Date}</td>
                <td>${row.Leg1_Strike}</td>
                <td>${row.Leg2_Strike}</td>
                <td>${row.Max_Profit}</td>`;
                
            if (row.is_valid) {
                html += `<td><button class="btn-trade" onclick="placeTrade('${row.Symbol}', '${row.Leg1_conId}', '${row.Leg2_conId}', '${row.Max_Profit}')">銝</button></td>`;
            } else {
                html += `<td><span style="color:red; font-weight:bold;">?曆???/span></td>`;
            }
            html += `</tr>`;
        });
        html += '</table></div>';
        document.getElementById('barchart_content').innerHTML = html;
        
    } catch (err) {
        document.getElementById('barchart_content').innerHTML = `<span style="color:red;">Failed to fetch: ${err}</span>`;
    }
}

async function placeTrade(symbol, leg1_conId, leg2_conId, max_profit) {
    if(!confirm(`蝣箏?閬 ${symbol} 銝 Bull Put Spread ?? (??${max_profit})`)) return;
    try {
        const response = await fetch('/api/option/trade_bull_put', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ Symbol: symbol, Leg1_conId: leg1_conId, Leg2_conId: leg2_conId, Max_Profit: max_profit })
        });
        const data = await response.json();
        alert(data.message);
    } catch (err) {
        alert("銝隢?憭望?: " + err);
    }
}

</script>
</body>
</html>

"""


def get_latest_csv():

    csv_files = glob.glob(os.path.join(BARCHART_DIR, "*.csv"))
    if not csv_files: return None
    return max(csv_files, key=os.path.getctime)

def standardize_columns(df):
    cols = list(df.columns)
    mapping = {}
    for c in cols:
        cl = c.lower().strip().replace(' ', '_').replace('%', '')
        if 'iv_rank' in cl or 'implied_volatility_rank' in cl: mapping[c] = 'IV_Rank'
        elif 'options_volume' in cl or 'opt_vol' in cl: mapping[c] = 'Options_Volume'
        elif 'open_interest' in cl or 'open_int' in cl: mapping[c] = 'Open_Interest'
        elif 'volume' in cl and 'options' not in cl: mapping[c] = 'Volume'
        elif 'symbol' in cl: mapping[c] = 'Symbol'
    return df.rename(columns=mapping)

def parse_numeric(val):
    if isinstance(val, str):
        val = val.replace(',', '').replace('%', '').strip()
        if val in ('', 'NA', 'N/A'): return 0.0
    try:
        return float(val)
    except:
        return 0.0

def connect_dash_ib():
    if not dash_ib.isConnected():
        try:
            # 雿輻 CLIENT_ID + 1 靘踹 option.py 銝餌摨銵蝒
            dash_ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID + 1)
        except:
            pass



@dash_app.route('/api_option/data')
def get_dash_data():
    file_path = get_latest_csv()
    if not file_path:
        return jsonify({'status': 'error', 'message': f' {BARCHART_DIR} 曆 CSV 瑼獢'})
    
    req_type = request.args.get('type', 'iv_rank')
    
    try:
        df = pd.read_csv(file_path)
        df = standardize_columns(df)
        
        for col in ['IV_Rank', 'Options_Volume', 'Volume', 'Open_Interest']:
            if col in df.columns:
                df[col] = df[col].apply(parse_numeric)

        if req_type == 'iv_rank':
            if 'IV_Rank' not in df.columns or 'Options_Volume' not in df.columns:
                return jsonify({'status': 'error', 'message': 'CSV 蝻箏 IV Rank  Options Volume 甈雿'})
            ic_df = df[(df['IV_Rank'] > 50) & (df['Options_Volume'] > 10000)].copy()
            ic_df = ic_df.sort_values(by='IV_Rank', ascending=False).head(50)
            return jsonify({'status': 'ok', 'data': ic_df.fillna('').to_dict(orient='records')})
            
        elif req_type == 'skew':
            if 'Volume' not in df.columns or 'Open_Interest' not in df.columns:
                return jsonify({'status': 'error', 'message': 'CSV 蝻箏 Volume  Open Interest 甈雿'})
            
            uoa_df = df[df['Volume'] > (df['Open_Interest'] * 5)].copy()
            uoa_df['UOA_Ratio'] = uoa_df['Volume'] / (uoa_df['Open_Interest'] + 0.001)
            uoa_df['UOA_Ratio'] = uoa_df['UOA_Ratio'].round(2)
            top_10 = uoa_df.sort_values(by='UOA_Ratio', ascending=False).head(10).to_dict(orient='records')
            
            connect_dash_ib()
            results = []
            if dash_ib.isConnected():
                for row in top_10:
                    sym = str(row.get('Symbol', '')).strip()
                    if not sym: continue
                    
                    stk = Stock(sym, 'SMART', 'USD')
                    try:
                        dash_ib.qualifyContracts(stk)
                        chains = dash_ib.reqSecDefOptParams(stk.symbol, '', stk.secType, stk.conId)
                        if chains:
                            chain = next((c for c in chains if c.exchange == 'SMART'), chains[0])
                            strikes = chain.strikes
                            expirations = chain.expirations
                            if strikes and expirations:
                                exp = sorted(expirations)[min(1, len(expirations)-1)]
                                [ticker] = dash_ib.reqTickers(stk)
                                dash_ib.sleep(1) # wait for ticker
                                current_price = ticker.marketPrice() if ticker else 0.0
                                
                                put_strike = next((s for s in reversed(strikes) if s < current_price * 0.9), strikes[0])
                                call_strike = next((s for s in strikes if s > current_price * 1.1), strikes[-1])
                                
                                put_contract = Option(sym, exp, put_strike, 'P', 'SMART')
                                call_contract = Option(sym, exp, call_strike, 'C', 'SMART')
                                dash_ib.qualifyContracts(put_contract, call_contract)
                                
                                [p_tick, c_tick] = dash_ib.reqTickers(put_contract, call_contract)
                                dash_ib.sleep(2) # 蝯 IB 銝暺勗急郭
                                put_iv = p_tick.impliedVolatility if p_tick and p_tick.impliedVolatility else 0.0
                                call_iv = c_tick.impliedVolatility if c_tick and c_tick.impliedVolatility else 0.0
                                
                                row['Put_IV'] = f"{put_iv*100:.2f}%" if put_iv else "N/A"
                                row['Call_IV'] = f"{call_iv*100:.2f}%" if call_iv else "N/A"
                                
                                if put_iv > call_iv * 1.5 and put_iv > 0:
                                    row['Skew_Signal'] = "摨 (撱箄降鞈 Call)"
                                elif call_iv > put_iv * 1.5 and call_iv > 0:
                                    row['Skew_Signal'] = "頠蝛 FOMO (撱箄降鞈 Put)"
                                else:
                                    row['Skew_Signal'] = "甇撣"
                            else:
                                row['Skew_Signal'] = "∪悼蝝寡"
                        else:
                            row['Skew_Signal'] = "⊥甈"
                    except Exception as e:
                        row['Skew_Signal'] = "Error"
                        
                    results.append(row)
            else:
                for row in top_10:
                    row['Skew_Signal'] = "IBKR 芷蝺"
                    results.append(row)
                    
            return jsonify({'status': 'ok', 'data': results})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@dash_app.route('/api_option/portfolio')
def get_dash_portfolio():
    connect_dash_ib()
    if not dash_ib.isConnected():
        return jsonify({'status': 'error', 'message': 'IBKR connection failed.'})
            
    positions = []
    for p in dash_ib.portfolio():
        if p.contract.secType in ['OPT', 'FOP']:
            action = 'BUY' if p.position < 0 else 'SELL'
            positions.append({
                'conId': p.contract.conId,
                'symbol': p.contract.symbol,
                'localSymbol': p.contract.localSymbol,
                'position': p.position,
                'marketPrice': p.marketPrice,
                'action': action
            })
            
    return jsonify({'status': 'ok', 'positions': positions})

@dash_app.route('/api_option/close', methods=['POST'])
def close_dash_position():
    payload = request.get_json()
    conId = payload.get('conId')
    action = payload.get('action')
    qty = payload.get('quantity')
    
    connect_dash_ib()
    if not dash_ib.isConnected():
        return jsonify({'status': 'error', 'message': 'IBKR connection failed.'})
            
    contract = Contract(conId=int(conId))
    try:
        dash_ib.qualifyContracts(contract)
        
        order = MarketOrder(action, float(qty))
        order.tif = 'DAY'
        order.algoStrategy = 'Adaptive'
        order.algoParams = [TagValue('adaptivePriority', 'Patient')]
        
        trade = dash_ib.placeOrder(contract, order)
        return jsonify({'status': 'ok', 'message': f'撌脤箏像: {action} {qty}'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@dash_app.route('/api_option/trade', methods=['POST'])
def place_dash_trade():
    payload = request.get_json()
    symbol = payload.get('symbol')
    strategy = payload.get('strategy')
    
    return jsonify({'status': 'ok', 'message': f' {symbol} 憪閮格'})

def start_dashboard_server():
    print(" Barchart Dashboard  Port 5900...")
    serve(dash_app, host='0.0.0.0', port=5900)



def get_latest_bull_put_csv():
    import glob
    csv_files = glob.glob(os.path.join(BARCHART_DIR, "bull-put-spread-option-screener-bull-put-adv*.csv"))
    if not csv_files: return None
    return max(csv_files, key=os.path.getctime)

def parse_numeric(val):
    if isinstance(val, str):
        val = val.replace(',', '').replace('%', '').strip()
        if val in ('', 'NA', 'N/A'): return 0.0
    try:
        return float(val)
    except:
        return 0.0
    try:
        return float(val)
    except:
        return 0.0

def connect_dash_ib():
    if not dash_ib.isConnected():
        try:
            # ?頛舀??CLIENT_ID + 1 ?謏?頩??? option.py ?豲???塗???
            dash_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(2000, 2999))
        except:
            pass



@dash_app.route('/api/option/data_bull_put')
def get_dash_data_bull_put():
    file_path = get_latest_bull_put_csv()
    if not file_path:
        return jsonify({'status': 'error', 'message': f'?豲????? bull-put-spread-option-screener-bull-put-adv CSV ?瞉??'})
    
    try:
        import pandas as pd
        df = pd.read_csv(file_path)
        df = df.dropna(subset=['Exp Date']) # Remove Barchart footer
        
        options = []
        for _, row in df.iterrows():
            symbol = row['Symbol']
            exp = str(row['Exp Date']).replace('-', '')
            leg1 = float(row['Leg1 Strike'])
            leg2 = float(row['Leg2 Strike'])
            options.append(Option(symbol, exp, leg1, 'P', 'SMART'))
            options.append(Option(symbol, exp, leg2, 'P', 'SMART'))

        # Create isolated IB instance for thread safety
        import random
        from ib_insync import IB
        import asyncio
        # ALWAYS create a new event loop for this thread to prevent hanging on reuse
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        local_ib = IB()
        try:
            local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(3000, 4999), timeout=10)
            qualified = local_ib.qualifyContracts(*options)
        finally:
            local_ib.disconnect()
            
        valid_conids = { (c.symbol, c.lastTradeDateOrContractMonth, c.strike, c.right): c.conId for c in qualified }
        
        results = []
        for _, row in df.iterrows():
            symbol = row['Symbol']
            exp = str(row['Exp Date']).replace('-', '')
            leg1 = float(row['Leg1 Strike'])
            leg2 = float(row['Leg2 Strike'])
            max_profit = float(str(row['Max Profit']).replace('$',''))
            
            # Check if both legs qualified
            key1 = (symbol, exp, leg1, 'P')
            key2 = (symbol, exp, leg2, 'P')
            
            is_valid = False
            con1, con2 = None, None
            if key1 in valid_conids and key2 in valid_conids:
                is_valid = True
                con1 = valid_conids[key1]
                con2 = valid_conids[key2]
                
            results.append({
                'Symbol': symbol,
                'Exp_Date': str(row['Exp Date']),
                'Leg1_Strike': leg1,
                'Leg2_Strike': leg2,
                'Max_Profit': max_profit,
                'is_valid': is_valid,
                'Leg1_conId': con1,
                'Leg2_conId': con2
            })
            
        return jsonify({'status': 'ok', 'data': results})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@dash_app.route('/api/option/portfolio')
def get_dash_portfolio():
    import random
    from ib_insync import IB
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    local_ib = IB()
    try:
        local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(7000, 7999), timeout=10)
        positions = []
        for p in local_ib.portfolio():
            if p.contract.secType in ['OPT', 'FOP']:
                action = 'BUY' if p.position < 0 else 'SELL'
                positions.append({
                    'conId': p.contract.conId,
                    'symbol': p.contract.symbol,
                    'localSymbol': p.contract.localSymbol,
                    'position': p.position,
                    'marketPrice': p.marketPrice,
                    'action': action
                })
        return jsonify({'status': 'ok', 'positions': positions})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})
    finally:
        local_ib.disconnect()

@dash_app.route('/api/option/close', methods=['POST'])
def close_dash_position():
    payload = request.get_json()
    conId = payload.get('conId')
    action = payload.get('action')
    qty = payload.get('quantity')
    
    import random
    from ib_insync import IB, Contract, MarketOrder, TagValue
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    local_ib = IB()
    try:
        local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(8000, 8999), timeout=10)
        contract = Contract(conId=int(conId))
        local_ib.qualifyContracts(contract)
        
        order = MarketOrder(action, float(qty))
        order.tif = 'DAY'
        order.algoStrategy = 'Adaptive'
        order.algoParams = [TagValue('adaptivePriority', 'Patient')]
        
        trade = local_ib.placeOrder(contract, order)
        local_ib.sleep(1)
        return jsonify({'status': 'ok', 'message': f'Order placed: {action} {qty}'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})
    finally:
        local_ib.disconnect()

@dash_app.route('/api/option/trade_bull_put', methods=['POST'])
def place_dash_trade_bull_put():
    payload = request.get_json()
    symbol = payload.get('Symbol')
    leg1_conId = payload.get('Leg1_conId')
    leg2_conId = payload.get('Leg2_conId')
    credit = float(payload.get('Max_Profit'))
    
    if not leg1_conId or not leg2_conId:
         return jsonify({'status': 'error', 'message': 'Invalid conIds for legs.'})
         
    try:
        from ib_insync import Contract, ComboLeg, LimitOrder
        # Build BAG
        contract = Contract()
        contract.symbol = symbol
        contract.secType = 'BAG'
        contract.currency = 'USD'
        contract.exchange = 'SMART'

        # To SELL the spread, the leg definitions must be reversed relative to the final execution
        l1 = ComboLeg(conId=int(leg1_conId), ratio=1, action='BUY', exchange='SMART')   # SELL order * BUY leg = SELL leg1
        l2 = ComboLeg(conId=int(leg2_conId), ratio=1, action='SELL', exchange='SMART')  # SELL order * SELL leg = BUY leg2
        contract.comboLegs = [l1, l2]

                # Isolated connection for placing order
        import random
        from ib_insync import IB
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        local_ib = IB()
        try:
            local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(5000, 6999), timeout=10)
            
            # Request market data to get ASK price
            local_ib.qualifyContracts(contract)
            ticker = local_ib.reqMktData(contract, "", True, False)
            
            # Wait up to 2 seconds for data
            limit_price = credit
            import math
            for _ in range(20):
                local_ib.sleep(0.1)
                if ticker.bid and not math.isnan(ticker.bid) and ticker.bid > 0:
                    limit_price = ticker.bid
                    break
            
            limit_price = round(limit_price, 2)
            
            # Parent limit order
            parent = LimitOrder('SELL', 1, limit_price)
            parent.transmit = False
            
            # Child limit order
            take_profit = LimitOrder('BUY', 1, round(limit_price * 0.25, 2))
            take_profit.transmit = True

            parent.orderId = local_ib.client.getReqId()
            take_profit.parentId = parent.orderId
            local_ib.placeOrder(contract, parent)
            local_ib.placeOrder(contract, take_profit)
            # Give TWS time to process the orders before disconnecting
            local_ib.sleep(1)
        finally:
            local_ib.disconnect()

        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'?豲?雓堆??: {e}'})

def start_dashboard_server():
    print("?鞈? Barchart Dashboard ??Port 5900...")
    serve(dash_app, host='0.0.0.0', port=5900)






@dash_app.route('/api/option/txo_strangles')
def get_txo_strangles():
    if api is None:
        return jsonify({'status': 'error', 'message': 'Shioaji not connected'})
    try:
        import shioaji as sj
        target_code = list(api.Contracts.Futures.TMF.keys())[0]
        c = api.Contracts.Futures.TMF[target_code]
        snap = api.snapshots([c])[0]
        underlying = snap.close
        atm_strike = round(underlying / 50) * 50
        
        all_opts = []
        for cat in ['TX1', 'TX2', 'TX4', 'TX5', 'TXO']:
            if hasattr(api.Contracts.Options, cat):
                for opt in getattr(api.Contracts.Options, cat):
                    all_opts.append(opt)
                    
        by_date = {}
        for opt in all_opts:
            if opt.delivery_date not in by_date:
                by_date[opt.delivery_date] = []
            by_date[opt.delivery_date].append(opt)
            
        sorted_dates = sorted(list(by_date.keys()))
        results = []
        for d in sorted_dates[:2]:
            opts = by_date[d]
            calls = [o for o in opts if o.option_right == 'Call' and o.strike_price == atm_strike]
            puts = [o for o in opts if o.option_right == 'Put' and o.strike_price == atm_strike]
            if calls and puts:
                results.append({
                    'delivery_date': d,
                    'atm_strike': atm_strike,
                    'call_code': calls[0].code,
                    'put_code': puts[0].code,
                    'symbol': f"TXO {d} ATM Strangle ({atm_strike})"
                })
        return jsonify({'status': 'ok', 'data': results})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@dash_app.route('/api/option/trade_txo_strangle', methods=['POST'])
def trade_txo_strangle():
    payload = request.json
    call_code = payload.get('call')
    put_code = payload.get('put')
    if api is None:
        return jsonify({'status': 'error', 'message': 'Shioaji not connected'})
    try:
        import shioaji as sj
        call_contract = None
        put_contract = None
        for cat in ['TX1', 'TX2', 'TX4', 'TX5', 'TXO']:
            if hasattr(api.Contracts.Options, cat):
                for c in getattr(api.Contracts.Options, cat):
                    if c.code == call_code: call_contract = c
                    if c.code == put_code: put_contract = c
                    
        oc = api.Order(price=0, quantity=1, action=sj.constant.Action.Sell, price_type=sj.constant.FuturesPriceType.MKT, order_type=sj.constant.OrderType.IOC, octype=sj.constant.FuturesOCType.Auto)
        op = api.Order(price=0, quantity=1, action=sj.constant.Action.Sell, price_type=sj.constant.FuturesPriceType.MKT, order_type=sj.constant.OrderType.IOC, octype=sj.constant.FuturesOCType.Auto)
        
        if call_contract: api.place_order(call_contract, oc)
        if put_contract: api.place_order(put_contract, op)
        return jsonify({'status': 'ok', 'message': f'?蹓鳴 TXO ?謕賣蹇????})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


def get_latest_bull_put_csv():
    import glob
    csv_files = glob.glob(os.path.join(BARCHART_DIR, "bull-put-spread-option-screener-bull-put-adv*.csv"))
    if not csv_files: return None
    return max(csv_files, key=os.path.getctime)

def parse_numeric(val):
    if isinstance(val, str):
        val = val.replace(',', '').replace('%', '').strip()
        if val in ('', 'NA', 'N/A'): return 0.0
    try:
        return float(val)
    except:
        return 0.0
    try:
        return float(val)
    except:
        return 0.0

def connect_dash_ib():
    if not dash_ib.isConnected():
        try:
            # ?輯撒??CLIENT_ID + 1 ??蹓??? option.py ????制???
            dash_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(2000, 2999))
        except:
            pass



@dash_app.route('/api/option/data_bull_put')
def get_dash_data_bull_put():
    file_path = get_latest_bull_put_csv()
    if not file_path:
        return jsonify({'status': 'error', 'message': f'?????? bull-put-spread-option-screener-bull-put-adv CSV ?澗??'})
    
    try:
        import pandas as pd
        df = pd.read_csv(file_path)
        df = df.dropna(subset=['Exp Date']) # Remove Barchart footer
        
        options = []
        for _, row in df.iterrows():
            symbol = row['Symbol']
            exp = str(row['Exp Date']).replace('-', '')
            leg1 = float(row['Leg1 Strike'])
            leg2 = float(row['Leg2 Strike'])
            options.append(Option(symbol, exp, leg1, 'P', 'SMART'))
            options.append(Option(symbol, exp, leg2, 'P', 'SMART'))

        # Create isolated IB instance for thread safety
        import random
        from ib_insync import IB
        import asyncio
        # ALWAYS create a new event loop for this thread to prevent hanging on reuse
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        local_ib = IB()
        try:
            local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(3000, 4999), timeout=10)
            qualified = local_ib.qualifyContracts(*options)
        finally:
            local_ib.disconnect()
            
        valid_conids = { (c.symbol, c.lastTradeDateOrContractMonth, c.strike, c.right): c.conId for c in qualified }
        
        results = []
        for _, row in df.iterrows():
            symbol = row['Symbol']
            exp = str(row['Exp Date']).replace('-', '')
            leg1 = float(row['Leg1 Strike'])
            leg2 = float(row['Leg2 Strike'])
            max_profit = float(str(row['Max Profit']).replace('$',''))
            
            # Check if both legs qualified
            key1 = (symbol, exp, leg1, 'P')
            key2 = (symbol, exp, leg2, 'P')
            
            is_valid = False
            con1, con2 = None, None
            if key1 in valid_conids and key2 in valid_conids:
                is_valid = True
                con1 = valid_conids[key1]
                con2 = valid_conids[key2]
                
            results.append({
                'Symbol': symbol,
                'Exp_Date': str(row['Exp Date']),
                'Leg1_Strike': leg1,
                'Leg2_Strike': leg2,
                'Max_Profit': max_profit,
                'is_valid': is_valid,
                'Leg1_conId': con1,
                'Leg2_conId': con2
            })
            
        return jsonify({'status': 'ok', 'data': results})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@dash_app.route('/api/option/portfolio')
def get_dash_portfolio():
    import random
    from ib_insync import IB
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    local_ib = IB()
    try:
        local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(7000, 7999), timeout=10)
        positions = []
        for p in local_ib.portfolio():
            if p.contract.secType in ['OPT', 'FOP']:
                action = 'BUY' if p.position < 0 else 'SELL'
                positions.append({
                    'conId': p.contract.conId,
                    'symbol': p.contract.symbol,
                    'localSymbol': p.contract.localSymbol,
                    'position': p.position,
                    'marketPrice': p.marketPrice,
                    'action': action
                })
        return jsonify({'status': 'ok', 'positions': positions})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})
    finally:
        local_ib.disconnect()

@dash_app.route('/api/option/close', methods=['POST'])
def close_dash_position():
    payload = request.get_json()
    conId = payload.get('conId')
    action = payload.get('action')
    qty = payload.get('quantity')
    
    import random
    from ib_insync import IB, Contract, MarketOrder, TagValue
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    local_ib = IB()
    try:
        local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(8000, 8999), timeout=10)
        contract = Contract(conId=int(conId))
        local_ib.qualifyContracts(contract)
        
        order = MarketOrder(action, float(qty))
        order.tif = 'DAY'
        order.algoStrategy = 'Adaptive'
        order.algoParams = [TagValue('adaptivePriority', 'Patient')]
        
        trade = local_ib.placeOrder(contract, order)
        local_ib.sleep(1)
        return jsonify({'status': 'ok', 'message': f'Order placed: {action} {qty}'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})
    finally:
        local_ib.disconnect()

@dash_app.route('/api/option/trade_bull_put', methods=['POST'])
def place_dash_trade_bull_put():
    payload = request.get_json()
    symbol = payload.get('Symbol')
    leg1_conId = payload.get('Leg1_conId')
    leg2_conId = payload.get('Leg2_conId')
    credit = float(payload.get('Max_Profit'))
    
    if not leg1_conId or not leg2_conId:
         return jsonify({'status': 'error', 'message': 'Invalid conIds for legs.'})
         
    try:
        from ib_insync import Contract, ComboLeg, LimitOrder
        # Build BAG
        contract = Contract()
        contract.symbol = symbol
        contract.secType = 'BAG'
        contract.currency = 'USD'
        contract.exchange = 'SMART'

        # To SELL the spread, the leg definitions must be reversed relative to the final execution
        l1 = ComboLeg(conId=int(leg1_conId), ratio=1, action='BUY', exchange='SMART')   # SELL order * BUY leg = SELL leg1
        l2 = ComboLeg(conId=int(leg2_conId), ratio=1, action='SELL', exchange='SMART')  # SELL order * SELL leg = BUY leg2
        contract.comboLegs = [l1, l2]

                # Isolated connection for placing order
        import random
        from ib_insync import IB
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        local_ib = IB()
        try:
            local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(5000, 6999), timeout=10)
            
            # Request market data to get ASK price
            local_ib.qualifyContracts(contract)
            ticker = local_ib.reqMktData(contract, "", True, False)
            
            # Wait up to 2 seconds for data
            limit_price = credit
            import math
            for _ in range(20):
                local_ib.sleep(0.1)
                if ticker.bid and not math.isnan(ticker.bid) and ticker.bid > 0:
                    limit_price = ticker.bid
                    break
            
            limit_price = round(limit_price, 2)
            
            # Parent limit order
            parent = LimitOrder('SELL', 1, limit_price)
            parent.transmit = False
            
            # Child limit order
            take_profit = LimitOrder('BUY', 1, round(limit_price * 0.25, 2))
            take_profit.transmit = True

            parent.orderId = local_ib.client.getReqId()
            take_profit.parentId = parent.orderId
            local_ib.placeOrder(contract, parent)
            local_ib.placeOrder(contract, take_profit)
            # Give TWS time to process the orders before disconnecting
            local_ib.sleep(1)
        finally:
            local_ib.disconnect()

        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'??謘??: {e}'})

def start_dashboard_server():
    print("?賹? Barchart Dashboard ??Port 5900...")
    serve(dash_app, host='0.0.0.0', port=5900)






@dash_app.route('/api/option/txo_strangles')
def get_txo_strangles():
    if api is None:
        return jsonify({'status': 'error', 'message': 'Shioaji not connected'})
    try:
        import shioaji as sj
        target_code = list(api.Contracts.Futures.TMF.keys())[0]
        c = api.Contracts.Futures.TMF[target_code]
        snap = api.snapshots([c])[0]
        underlying = snap.close
        atm_strike = round(underlying / 50) * 50
        
        all_opts = []
        for cat in ['TX1', 'TX2', 'TX4', 'TX5', 'TXO']:
            if hasattr(api.Contracts.Options, cat):
                for opt in getattr(api.Contracts.Options, cat):
                    all_opts.append(opt)
                    
        by_date = {}
        for opt in all_opts:
            if opt.delivery_date not in by_date:
                by_date[opt.delivery_date] = []
            by_date[opt.delivery_date].append(opt)
            
        sorted_dates = sorted(list(by_date.keys()))
        results = []
        for d in sorted_dates[:2]:
            opts = by_date[d]
            calls = [o for o in opts if o.option_right == 'Call' and o.strike_price == atm_strike]
            puts = [o for o in opts if o.option_right == 'Put' and o.strike_price == atm_strike]
            if calls and puts:
                results.append({
                    'delivery_date': d,
                    'atm_strike': atm_strike,
                    'call_code': calls[0].code,
                    'put_code': puts[0].code,
                    'symbol': f"TXO {d} ATM Strangle ({atm_strike})"
                })
        return jsonify({'status': 'ok', 'data': results})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@dash_app.route('/api/option/trade_txo_strangle', methods=['POST'])
def trade_txo_strangle():
    payload = request.json
    call_code = payload.get('call')
    put_code = payload.get('put')
    if api is None:
        return jsonify({'status': 'error', 'message': 'Shioaji not connected'})
    try:
        import shioaji as sj
        call_contract = None
        put_contract = None
        for cat in ['TX1', 'TX2', 'TX4', 'TX5', 'TXO']:
            if hasattr(api.Contracts.Options, cat):
                for c in getattr(api.Contracts.Options, cat):
                    if c.code == call_code: call_contract = c
                    if c.code == put_code: put_contract = c
                    
        oc = api.Order(price=0, quantity=1, action=sj.constant.Action.Sell, price_type=sj.constant.FuturesPriceType.MKT, order_type=sj.constant.OrderType.IOC, octype=sj.constant.FuturesOCType.Auto)
        op = api.Order(price=0, quantity=1, action=sj.constant.Action.Sell, price_type=sj.constant.FuturesPriceType.MKT, order_type=sj.constant.OrderType.IOC, octype=sj.constant.FuturesOCType.Auto)
        
        if call_contract: api.place_order(call_contract, oc)
        if put_contract: api.place_order(put_contract, op)
        return jsonify({'status': 'ok', 'message': f'? TXO ?都撣?格???})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


def get_latest_bull_put_csv():
    import glob
    csv_files = glob.glob(os.path.join(BARCHART_DIR, "bull-put-spread-option-screener-bull-put-adv*.csv"))
    if not csv_files: return None
    return max(csv_files, key=os.path.getctime)

def parse_numeric(val):
    if isinstance(val, str):
        val = val.replace(',', '').replace('%', '').strip()
        if val in ('', 'NA', 'N/A'): return 0.0
    try:
        return float(val)
    except:
        return 0.0
    try:
        return float(val)
    except:
        return 0.0

def connect_dash_ib():
    if not dash_ib.isConnected():
        try:
            # ?輯撒??CLIENT_ID + 1 ??蹓??? option.py ????制???
            dash_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(2000, 2999))
        except:
            pass



@dash_app.route('/api/option/data_bull_put')
def get_dash_data_bull_put():
    file_path = get_latest_bull_put_csv()
    if not file_path:
        return jsonify({'status': 'error', 'message': f'?????? bull-put-spread-option-screener-bull-put-adv CSV ?澗??'})
    
    try:
        import pandas as pd
        df = pd.read_csv(file_path)
        df = df.dropna(subset=['Exp Date']) # Remove Barchart footer
        
        options = []
        for _, row in df.iterrows():
            symbol = row['Symbol']
            exp = str(row['Exp Date']).replace('-', '')
            leg1 = float(row['Leg1 Strike'])
            leg2 = float(row['Leg2 Strike'])
            options.append(Option(symbol, exp, leg1, 'P', 'SMART'))
            options.append(Option(symbol, exp, leg2, 'P', 'SMART'))

        # Create isolated IB instance for thread safety
        import random
        from ib_insync import IB
        import asyncio
        # ALWAYS create a new event loop for this thread to prevent hanging on reuse
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        local_ib = IB()
        try:
            local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(3000, 4999), timeout=10)
            qualified = local_ib.qualifyContracts(*options)
        finally:
            local_ib.disconnect()
            
        valid_conids = { (c.symbol, c.lastTradeDateOrContractMonth, c.strike, c.right): c.conId for c in qualified }
        
        results = []
        for _, row in df.iterrows():
            symbol = row['Symbol']
            exp = str(row['Exp Date']).replace('-', '')
            leg1 = float(row['Leg1 Strike'])
            leg2 = float(row['Leg2 Strike'])
            max_profit = float(str(row['Max Profit']).replace('$',''))
            
            # Check if both legs qualified
            key1 = (symbol, exp, leg1, 'P')
            key2 = (symbol, exp, leg2, 'P')
            
            is_valid = False
            con1, con2 = None, None
            if key1 in valid_conids and key2 in valid_conids:
                is_valid = True
                con1 = valid_conids[key1]
                con2 = valid_conids[key2]
                
            results.append({
                'Symbol': symbol,
                'Exp_Date': str(row['Exp Date']),
                'Leg1_Strike': leg1,
                'Leg2_Strike': leg2,
                'Max_Profit': max_profit,
                'is_valid': is_valid,
                'Leg1_conId': con1,
                'Leg2_conId': con2
            })
            
        return jsonify({'status': 'ok', 'data': results})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@dash_app.route('/api/option/portfolio')
def get_dash_portfolio():
    import random
    from ib_insync import IB
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    local_ib = IB()
    try:
        local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(7000, 7999), timeout=10)
        positions = []
        for p in local_ib.portfolio():
            if p.contract.secType in ['OPT', 'FOP']:
                action = 'BUY' if p.position < 0 else 'SELL'
                positions.append({
                    'conId': p.contract.conId,
                    'symbol': p.contract.symbol,
                    'localSymbol': p.contract.localSymbol,
                    'position': p.position,
                    'marketPrice': p.marketPrice,
                    'action': action
                })
        return jsonify({'status': 'ok', 'positions': positions})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})
    finally:
        local_ib.disconnect()

@dash_app.route('/api/option/close', methods=['POST'])
def close_dash_position():
    payload = request.get_json()
    conId = payload.get('conId')
    action = payload.get('action')
    qty = payload.get('quantity')
    
    import random
    from ib_insync import IB, Contract, MarketOrder, TagValue
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    local_ib = IB()
    try:
        local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(8000, 8999), timeout=10)
        contract = Contract(conId=int(conId))
        local_ib.qualifyContracts(contract)
        
        order = MarketOrder(action, float(qty))
        order.tif = 'DAY'
        order.algoStrategy = 'Adaptive'
        order.algoParams = [TagValue('adaptivePriority', 'Patient')]
        
        trade = local_ib.placeOrder(contract, order)
        local_ib.sleep(1)
        return jsonify({'status': 'ok', 'message': f'Order placed: {action} {qty}'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})
    finally:
        local_ib.disconnect()

@dash_app.route('/api/option/trade_bull_put', methods=['POST'])
def place_dash_trade_bull_put():
    payload = request.get_json()
    symbol = payload.get('Symbol')
    leg1_conId = payload.get('Leg1_conId')
    leg2_conId = payload.get('Leg2_conId')
    credit = float(payload.get('Max_Profit'))
    
    if not leg1_conId or not leg2_conId:
         return jsonify({'status': 'error', 'message': 'Invalid conIds for legs.'})
         
    try:
        from ib_insync import Contract, ComboLeg, LimitOrder
        # Build BAG
        contract = Contract()
        contract.symbol = symbol
        contract.secType = 'BAG'
        contract.currency = 'USD'
        contract.exchange = 'SMART'

        # To SELL the spread, the leg definitions must be reversed relative to the final execution
        l1 = ComboLeg(conId=int(leg1_conId), ratio=1, action='BUY', exchange='SMART')   # SELL order * BUY leg = SELL leg1
        l2 = ComboLeg(conId=int(leg2_conId), ratio=1, action='SELL', exchange='SMART')  # SELL order * SELL leg = BUY leg2
        contract.comboLegs = [l1, l2]

                # Isolated connection for placing order
        import random
        from ib_insync import IB
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        local_ib = IB()
        try:
            local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(5000, 6999), timeout=10)
            
            # Request market data to get ASK price
            local_ib.qualifyContracts(contract)
            ticker = local_ib.reqMktData(contract, "", True, False)
            
            # Wait up to 2 seconds for data
            limit_price = credit
            import math
            for _ in range(20):
                local_ib.sleep(0.1)
                if ticker.bid and not math.isnan(ticker.bid) and ticker.bid > 0:
                    limit_price = ticker.bid
                    break
            
            limit_price = round(limit_price, 2)
            
            # Parent limit order
            parent = LimitOrder('SELL', 1, limit_price)
            parent.transmit = False
            
            # Child limit order
            take_profit = LimitOrder('BUY', 1, round(limit_price * 0.25, 2))
            take_profit.transmit = True

            parent.orderId = local_ib.client.getReqId()
            take_profit.parentId = parent.orderId
            local_ib.placeOrder(contract, parent)
            local_ib.placeOrder(contract, take_profit)
            # Give TWS time to process the orders before disconnecting
            local_ib.sleep(1)
        finally:
            local_ib.disconnect()

        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'??謘??: {e}'})

def start_dashboard_server():
    print("?賹? Barchart Dashboard ??Port 5900...")
    serve(dash_app, host='0.0.0.0', port=5900)






@dash_app.route('/api/option/txo_strangles')
def get_txo_strangles():
    if api is None:
        return jsonify({'status': 'error', 'message': 'Shioaji not connected'})
    try:
        import shioaji as sj
        target_code = list(api.Contracts.Futures.TMF.keys())[0]
        c = api.Contracts.Futures.TMF[target_code]
        snap = api.snapshots([c])[0]
        underlying = snap.close
        atm_strike = round(underlying / 50) * 50
        
        all_opts = []
        for cat in ['TX1', 'TX2', 'TX4', 'TX5', 'TXO']:
            if hasattr(api.Contracts.Options, cat):
                for opt in getattr(api.Contracts.Options, cat):
                    all_opts.append(opt)
                    
        by_date = {}
        for opt in all_opts:
            if opt.delivery_date not in by_date:
                by_date[opt.delivery_date] = []
            by_date[opt.delivery_date].append(opt)
            
        sorted_dates = sorted(list(by_date.keys()))
        results = []
        for d in sorted_dates[:2]:
            opts = by_date[d]
            calls = [o for o in opts if o.option_right == 'Call' and o.strike_price == atm_strike]
            puts = [o for o in opts if o.option_right == 'Put' and o.strike_price == atm_strike]
            if calls and puts:
                results.append({
                    'delivery_date': d,
                    'atm_strike': atm_strike,
                    'call_code': calls[0].code,
                    'put_code': puts[0].code,
                    'symbol': f"TXO {d} ATM Strangle ({atm_strike})"
                })
        return jsonify({'status': 'ok', 'data': results})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@dash_app.route('/api/option/trade_txo_strangle', methods=['POST'])
def trade_txo_strangle():
    payload = request.json
    call_code = payload.get('call')
    put_code = payload.get('put')
    if api is None:
        return jsonify({'status': 'error', 'message': 'Shioaji not connected'})
    try:
        import shioaji as sj
        call_contract = None
        put_contract = None
        for cat in ['TX1', 'TX2', 'TX4', 'TX5', 'TXO']:
            if hasattr(api.Contracts.Options, cat):
                for c in getattr(api.Contracts.Options, cat):
                    if c.code == call_code: call_contract = c
                    if c.code == put_code: put_contract = c
                    
        oc = api.Order(price=0, quantity=1, action=sj.constant.Action.Sell, price_type=sj.constant.FuturesPriceType.MKT, order_type=sj.constant.OrderType.IOC, octype=sj.constant.FuturesOCType.Auto)
        op = api.Order(price=0, quantity=1, action=sj.constant.Action.Sell, price_type=sj.constant.FuturesPriceType.MKT, order_type=sj.constant.OrderType.IOC, octype=sj.constant.FuturesOCType.Auto)
        
        if call_contract: api.place_order(call_contract, oc)
        if put_contract: api.place_order(put_contract, op)
        return jsonify({'status': 'ok', 'message': 'TXO Strangle Order Sent Successfully!'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


def get_latest_bull_put_csv():
    import glob
    csv_files = glob.glob(os.path.join(BARCHART_DIR, "bull-put-spread-option-screener-bull-put-adv*.csv"))
    if not csv_files: return None
    return max(csv_files, key=os.path.getctime)

def parse_numeric(val):
    if isinstance(val, str):
        val = val.replace(',', '').replace('%', '').strip()
        if val in ('', 'NA', 'N/A'): return 0.0
    try:
        return float(val)
    except:
        return 0.0
    try:
        return float(val)
    except:
        return 0.0

def connect_dash_ib():
    if not dash_ib.isConnected():
        try:
            # ?輯撒??CLIENT_ID + 1 ??蹓??? option.py ????制???
            dash_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(2000, 2999))
        except:
            pass



@dash_app.route('/api/option/data_bull_put')
def get_dash_data_bull_put():
    file_path = get_latest_bull_put_csv()
    if not file_path:
        return jsonify({'status': 'error', 'message': f'?????? bull-put-spread-option-screener-bull-put-adv CSV ?澗??'})
    
    try:
        import pandas as pd
        df = pd.read_csv(file_path)
        df = df.dropna(subset=['Exp Date']) # Remove Barchart footer
        
        options = []
        for _, row in df.iterrows():
            symbol = row['Symbol']
            exp = str(row['Exp Date']).replace('-', '')
            leg1 = float(row['Leg1 Strike'])
            leg2 = float(row['Leg2 Strike'])
            options.append(Option(symbol, exp, leg1, 'P', 'SMART'))
            options.append(Option(symbol, exp, leg2, 'P', 'SMART'))

        # Create isolated IB instance for thread safety
        import random
        from ib_insync import IB
        import asyncio
        # ALWAYS create a new event loop for this thread to prevent hanging on reuse
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        local_ib = IB()
        try:
            local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(3000, 4999), timeout=10)
            qualified = local_ib.qualifyContracts(*options)
        finally:
            local_ib.disconnect()
            
        valid_conids = { (c.symbol, c.lastTradeDateOrContractMonth, c.strike, c.right): c.conId for c in qualified }
        
        results = []
        for _, row in df.iterrows():
            symbol = row['Symbol']
            exp = str(row['Exp Date']).replace('-', '')
            leg1 = float(row['Leg1 Strike'])
            leg2 = float(row['Leg2 Strike'])
            max_profit = float(str(row['Max Profit']).replace('$',''))
            
            # Check if both legs qualified
            key1 = (symbol, exp, leg1, 'P')
            key2 = (symbol, exp, leg2, 'P')
            
            is_valid = False
            con1, con2 = None, None
            if key1 in valid_conids and key2 in valid_conids:
                is_valid = True
                con1 = valid_conids[key1]
                con2 = valid_conids[key2]
                
            results.append({
                'Symbol': symbol,
                'Exp_Date': str(row['Exp Date']),
                'Leg1_Strike': leg1,
                'Leg2_Strike': leg2,
                'Max_Profit': max_profit,
                'is_valid': is_valid,
                'Leg1_conId': con1,
                'Leg2_conId': con2
            })
            
        return jsonify({'status': 'ok', 'data': results})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@dash_app.route('/api/option/portfolio')
def get_dash_portfolio():
    import random
    from ib_insync import IB
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    local_ib = IB()
    try:
        local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(7000, 7999), timeout=10)
        positions = []
        for p in local_ib.portfolio():
            if p.contract.secType in ['OPT', 'FOP']:
                action = 'BUY' if p.position < 0 else 'SELL'
                positions.append({
                    'conId': p.contract.conId,
                    'symbol': p.contract.symbol,
                    'localSymbol': p.contract.localSymbol,
                    'position': p.position,
                    'marketPrice': p.marketPrice,
                    'action': action
                })
        return jsonify({'status': 'ok', 'positions': positions})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})
    finally:
        local_ib.disconnect()

@dash_app.route('/api/option/close', methods=['POST'])
def close_dash_position():
    payload = request.get_json()
    conId = payload.get('conId')
    action = payload.get('action')
    qty = payload.get('quantity')
    
    import random
    from ib_insync import IB, Contract, MarketOrder, TagValue
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    local_ib = IB()
    try:
        local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(8000, 8999), timeout=10)
        contract = Contract(conId=int(conId))
        local_ib.qualifyContracts(contract)
        
        order = MarketOrder(action, float(qty))
        order.tif = 'DAY'
        order.algoStrategy = 'Adaptive'
        order.algoParams = [TagValue('adaptivePriority', 'Patient')]
        
        trade = local_ib.placeOrder(contract, order)
        local_ib.sleep(1)
        return jsonify({'status': 'ok', 'message': f'Order placed: {action} {qty}'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})
    finally:
        local_ib.disconnect()

@dash_app.route('/api/option/trade_bull_put', methods=['POST'])
def place_dash_trade_bull_put():
    payload = request.get_json()
    symbol = payload.get('Symbol')
    leg1_conId = payload.get('Leg1_conId')
    leg2_conId = payload.get('Leg2_conId')
    credit = float(payload.get('Max_Profit'))
    
    if not leg1_conId or not leg2_conId:
         return jsonify({'status': 'error', 'message': 'Invalid conIds for legs.'})
         
    try:
        from ib_insync import Contract, ComboLeg, LimitOrder
        # Build BAG
        contract = Contract()
        contract.symbol = symbol
        contract.secType = 'BAG'
        contract.currency = 'USD'
        contract.exchange = 'SMART'

        # To SELL the spread, the leg definitions must be reversed relative to the final execution
        l1 = ComboLeg(conId=int(leg1_conId), ratio=1, action='BUY', exchange='SMART')   # SELL order * BUY leg = SELL leg1
        l2 = ComboLeg(conId=int(leg2_conId), ratio=1, action='SELL', exchange='SMART')  # SELL order * SELL leg = BUY leg2
        contract.comboLegs = [l1, l2]

                # Isolated connection for placing order
        import random
        from ib_insync import IB
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        local_ib = IB()
        try:
            local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(5000, 6999), timeout=10)
            
            # Request market data to get ASK price
            local_ib.qualifyContracts(contract)
            ticker = local_ib.reqMktData(contract, "", True, False)
            
            # Wait up to 2 seconds for data
            limit_price = credit
            import math
            for _ in range(20):
                local_ib.sleep(0.1)
                if ticker.bid and not math.isnan(ticker.bid) and ticker.bid > 0:
                    limit_price = ticker.bid
                    break
            
            limit_price = round(limit_price, 2)
            
            # Parent limit order
            parent = LimitOrder('SELL', 1, limit_price)
            parent.transmit = False
            
            # Child limit order
            take_profit = LimitOrder('BUY', 1, round(limit_price * 0.25, 2))
            take_profit.transmit = True

            parent.orderId = local_ib.client.getReqId()
            take_profit.parentId = parent.orderId
            local_ib.placeOrder(contract, parent)
            local_ib.placeOrder(contract, take_profit)
            # Give TWS time to process the orders before disconnecting
            local_ib.sleep(1)
        finally:
            local_ib.disconnect()

        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'??謘??: {e}'})

def start_dashboard_server():
    print("?賹? Barchart Dashboard ??Port 5900...")
    serve(dash_app, host='0.0.0.0', port=5900)






@dash_app.route('/api/option/txo_strangles')
def get_txo_strangles():
    if api is None:
        return jsonify({'status': 'error', 'message': 'Shioaji not connected'})
    try:
        import shioaji as sj
        target_code = list(api.Contracts.Futures.TMF.keys())[0]
        c = api.Contracts.Futures.TMF[target_code]
        snap = api.snapshots([c])[0]
        underlying = snap.close
        atm_strike = round(underlying / 50) * 50
        
        all_opts = []
        for cat in ['TX1', 'TX2', 'TX4', 'TX5', 'TXO']:
            if hasattr(api.Contracts.Options, cat):
                for opt in getattr(api.Contracts.Options, cat):
                    all_opts.append(opt)
                    
        by_date = {}
        for opt in all_opts:
            if opt.delivery_date not in by_date:
                by_date[opt.delivery_date] = []
            by_date[opt.delivery_date].append(opt)
            
        sorted_dates = sorted(list(by_date.keys()))
        results = []
        for d in sorted_dates[:2]:
            opts = by_date[d]
            calls = [o for o in opts if o.option_right == 'Call' and o.strike_price == atm_strike]
            puts = [o for o in opts if o.option_right == 'Put' and o.strike_price == atm_strike]
            if calls and puts:
                results.append({
                    'delivery_date': d,
                    'atm_strike': atm_strike,
                    'call_code': calls[0].code,
                    'put_code': puts[0].code,
                    'symbol': f"TXO {d} ATM Strangle ({atm_strike})"
                })
        return jsonify({'status': 'ok', 'data': results})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@dash_app.route('/api/option/trade_txo_strangle', methods=['POST'])
def trade_txo_strangle():
    payload = request.json
    call_code = payload.get('call')
    put_code = payload.get('put')
    if api is None:
        return jsonify({'status': 'error', 'message': 'Shioaji not connected'})
    try:
        import shioaji as sj
        call_contract = None
        put_contract = None
        for cat in ['TX1', 'TX2', 'TX4', 'TX5', 'TXO']:
            if hasattr(api.Contracts.Options, cat):
                for c in getattr(api.Contracts.Options, cat):
                    if c.code == call_code: call_contract = c
                    if c.code == put_code: put_contract = c
                    
        oc = api.Order(price=0, quantity=1, action=sj.constant.Action.Sell, price_type=sj.constant.FuturesPriceType.MKT, order_type=sj.constant.OrderType.IOC, octype=sj.constant.FuturesOCType.Auto)
        op = api.Order(price=0, quantity=1, action=sj.constant.Action.Sell, price_type=sj.constant.FuturesPriceType.MKT, order_type=sj.constant.OrderType.IOC, octype=sj.constant.FuturesOCType.Auto)
        
        if call_contract: api.place_order(call_contract, oc)
        if put_contract: api.place_order(put_contract, op)
        return jsonify({'status': 'ok', 'message': 'TXO Strangle Order Sent Successfully!'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


def get_latest_bull_put_csv():
    import glob
    csv_files = glob.glob(os.path.join(BARCHART_DIR, "bull-put-spread-option-screener-bull-put-adv*.csv"))
    if not csv_files: return None
    return max(csv_files, key=os.path.getctime)

def parse_numeric(val):
    if isinstance(val, str):
        val = val.replace(',', '').replace('%', '').strip()
        if val in ('', 'NA', 'N/A'): return 0.0
    try:
        return float(val)
    except:
        return 0.0
    try:
        return float(val)
    except:
        return 0.0

def connect_dash_ib():
    if not dash_ib.isConnected():
        try:
            # ?輯撒??CLIENT_ID + 1 ??蹓??? option.py ????制???
            dash_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(2000, 2999))
        except:
            pass



@dash_app.route('/api/option/data_bull_put')
def get_dash_data_bull_put():
    file_path = get_latest_bull_put_csv()
    if not file_path:
        return jsonify({'status': 'error', 'message': f'?????? bull-put-spread-option-screener-bull-put-adv CSV ?澗??'})
    
    try:
        import pandas as pd
        df = pd.read_csv(file_path)
        df = df.dropna(subset=['Exp Date']) # Remove Barchart footer
        
        options = []
        for _, row in df.iterrows():
            symbol = row['Symbol']
            exp = str(row['Exp Date']).replace('-', '')
            leg1 = float(row['Leg1 Strike'])
            leg2 = float(row['Leg2 Strike'])
            options.append(Option(symbol, exp, leg1, 'P', 'SMART'))
            options.append(Option(symbol, exp, leg2, 'P', 'SMART'))

        # Create isolated IB instance for thread safety
        import random
        from ib_insync import IB
        import asyncio
        # ALWAYS create a new event loop for this thread to prevent hanging on reuse
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        local_ib = IB()
        try:
            local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(3000, 4999), timeout=10)
            qualified = local_ib.qualifyContracts(*options)
        finally:
            local_ib.disconnect()
            
        valid_conids = { (c.symbol, c.lastTradeDateOrContractMonth, c.strike, c.right): c.conId for c in qualified }
        
        results = []
        for _, row in df.iterrows():
            symbol = row['Symbol']
            exp = str(row['Exp Date']).replace('-', '')
            leg1 = float(row['Leg1 Strike'])
            leg2 = float(row['Leg2 Strike'])
            max_profit = float(str(row['Max Profit']).replace('$',''))
            
            # Check if both legs qualified
            key1 = (symbol, exp, leg1, 'P')
            key2 = (symbol, exp, leg2, 'P')
            
            is_valid = False
            con1, con2 = None, None
            if key1 in valid_conids and key2 in valid_conids:
                is_valid = True
                con1 = valid_conids[key1]
                con2 = valid_conids[key2]
                
            results.append({
                'Symbol': symbol,
                'Exp_Date': str(row['Exp Date']),
                'Leg1_Strike': leg1,
                'Leg2_Strike': leg2,
                'Max_Profit': max_profit,
                'is_valid': is_valid,
                'Leg1_conId': con1,
                'Leg2_conId': con2
            })
            
        return jsonify({'status': 'ok', 'data': results})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@dash_app.route('/api/option/portfolio')
def get_dash_portfolio():
    import random
    from ib_insync import IB
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    local_ib = IB()
    try:
        local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(7000, 7999), timeout=10)
        positions = []
        for p in local_ib.portfolio():
            if p.contract.secType in ['OPT', 'FOP']:
                action = 'BUY' if p.position < 0 else 'SELL'
                positions.append({
                    'conId': p.contract.conId,
                    'symbol': p.contract.symbol,
                    'localSymbol': p.contract.localSymbol,
                    'position': p.position,
                    'marketPrice': p.marketPrice,
                    'action': action
                })
        return jsonify({'status': 'ok', 'positions': positions})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})
    finally:
        local_ib.disconnect()

@dash_app.route('/api/option/close', methods=['POST'])
def close_dash_position():
    payload = request.get_json()
    conId = payload.get('conId')
    action = payload.get('action')
    qty = payload.get('quantity')
    
    import random
    from ib_insync import IB, Contract, MarketOrder, TagValue
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    local_ib = IB()
    try:
        local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(8000, 8999), timeout=10)
        contract = Contract(conId=int(conId))
        local_ib.qualifyContracts(contract)
        
        order = MarketOrder(action, float(qty))
        order.tif = 'DAY'
        order.algoStrategy = 'Adaptive'
        order.algoParams = [TagValue('adaptivePriority', 'Patient')]
        
        trade = local_ib.placeOrder(contract, order)
        local_ib.sleep(1)
        return jsonify({'status': 'ok', 'message': f'Order placed: {action} {qty}'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})
    finally:
        local_ib.disconnect()

@dash_app.route('/api/option/trade_bull_put', methods=['POST'])
def place_dash_trade_bull_put():
    payload = request.get_json()
    symbol = payload.get('Symbol')
    leg1_conId = payload.get('Leg1_conId')
    leg2_conId = payload.get('Leg2_conId')
    credit = float(payload.get('Max_Profit'))
    
    if not leg1_conId or not leg2_conId:
         return jsonify({'status': 'error', 'message': 'Invalid conIds for legs.'})
         
    try:
        from ib_insync import Contract, ComboLeg, LimitOrder
        # Build BAG
        contract = Contract()
        contract.symbol = symbol
        contract.secType = 'BAG'
        contract.currency = 'USD'
        contract.exchange = 'SMART'

        # To SELL the spread, the leg definitions must be reversed relative to the final execution
        l1 = ComboLeg(conId=int(leg1_conId), ratio=1, action='BUY', exchange='SMART')   # SELL order * BUY leg = SELL leg1
        l2 = ComboLeg(conId=int(leg2_conId), ratio=1, action='SELL', exchange='SMART')  # SELL order * SELL leg = BUY leg2
        contract.comboLegs = [l1, l2]

                # Isolated connection for placing order
        import random
        from ib_insync import IB
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        local_ib = IB()
        try:
            local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(5000, 6999), timeout=10)
            
            # Request market data to get ASK price
            local_ib.qualifyContracts(contract)
            ticker = local_ib.reqMktData(contract, "", True, False)
            
            # Wait up to 2 seconds for data
            limit_price = credit
            import math
            for _ in range(20):
                local_ib.sleep(0.1)
                if ticker.bid and not math.isnan(ticker.bid) and ticker.bid > 0:
                    limit_price = ticker.bid
                    break
            
            limit_price = round(limit_price, 2)
            
            # Parent limit order
            parent = LimitOrder('SELL', 1, limit_price)
            parent.transmit = False
            
            # Child limit order
            take_profit = LimitOrder('BUY', 1, round(limit_price * 0.25, 2))
            take_profit.transmit = True

            parent.orderId = local_ib.client.getReqId()
            take_profit.parentId = parent.orderId
            local_ib.placeOrder(contract, parent)
            local_ib.placeOrder(contract, take_profit)
            # Give TWS time to process the orders before disconnecting
            local_ib.sleep(1)
        finally:
            local_ib.disconnect()

        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'??謘??: {e}'})

def start_dashboard_server():
    print("?賹? Barchart Dashboard ??Port 5900...")
    serve(dash_app, host='0.0.0.0', port=5900)






@dash_app.route('/api/option/txo_strangles')
def get_txo_strangles():
    if api is None:
        return jsonify({'status': 'error', 'message': 'Shioaji not connected'})
    try:
        import shioaji as sj
        target_code = list(api.Contracts.Futures.TMF.keys())[0]
        c = api.Contracts.Futures.TMF[target_code]
        snap = api.snapshots([c])[0]
        underlying = snap.close
        atm_strike = round(underlying / 50) * 50
        
        all_opts = []
        for cat in ['TX1', 'TX2', 'TX4', 'TX5', 'TXO']:
            if hasattr(api.Contracts.Options, cat):
                for opt in getattr(api.Contracts.Options, cat):
                    all_opts.append(opt)
                    
        by_date = {}
        for opt in all_opts:
            if opt.delivery_date not in by_date:
                by_date[opt.delivery_date] = []
            by_date[opt.delivery_date].append(opt)
            
        sorted_dates = sorted(list(by_date.keys()))
        results = []
        for d in sorted_dates[:2]:
            opts = by_date[d]
            calls = [o for o in opts if o.option_right == 'Call' and o.strike_price == atm_strike]
            puts = [o for o in opts if o.option_right == 'Put' and o.strike_price == atm_strike]
            if calls and puts:
                results.append({
                    'delivery_date': d,
                    'atm_strike': atm_strike,
                    'call_code': calls[0].code,
                    'put_code': puts[0].code,
                    'symbol': f"TXO {d} ATM Strangle ({atm_strike})"
                })
        return jsonify({'status': 'ok', 'data': results})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@dash_app.route('/api/option/trade_txo_strangle', methods=['POST'])
def trade_txo_strangle():
    payload = request.json
    call_code = payload.get('call')
    put_code = payload.get('put')
    if api is None:
        return jsonify({'status': 'error', 'message': 'Shioaji not connected'})
    try:
        import shioaji as sj
        call_contract = None
        put_contract = None
        for cat in ['TX1', 'TX2', 'TX4', 'TX5', 'TXO']:
            if hasattr(api.Contracts.Options, cat):
                for c in getattr(api.Contracts.Options, cat):
                    if c.code == call_code: call_contract = c
                    if c.code == put_code: put_contract = c
                    
        oc = api.Order(price=0, quantity=1, action=sj.constant.Action.Sell, price_type=sj.constant.FuturesPriceType.MKT, order_type=sj.constant.OrderType.IOC, octype=sj.constant.FuturesOCType.Auto)
        op = api.Order(price=0, quantity=1, action=sj.constant.Action.Sell, price_type=sj.constant.FuturesPriceType.MKT, order_type=sj.constant.OrderType.IOC, octype=sj.constant.FuturesOCType.Auto)
        
        if call_contract: api.place_order(call_contract, oc)
        if put_contract: api.place_order(put_contract, op)
        return jsonify({'status': 'ok', 'message': 'TXO Strangle Order Sent Successfully!'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


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
        return jsonify({"status": "error", "message": "撖蝣潮航炊"}), 401

    group = payload.get("group")
    if group not in HEDGE_CONFIG:
        return jsonify({"status": "error", "message": f"曆啁黎蝯: {group}"}), 400

    upper_raw = payload.get("upper")
    lower_raw = payload.get("lower")

    try:
        if upper_raw not in (None, ""):
            HEDGE_CONFIG[group]["upper_threshold"] = float(upper_raw)
        if lower_raw not in (None, ""):
            HEDGE_CONFIG[group]["lower_threshold"] = float(lower_raw)
    except ValueError:
        return jsonify({"status": "error", "message": "銝/銝敹舀詨"}), 400

    persist_env_var("HEDGE_CONFIG_JSON", serialize_hedge_config(HEDGE_CONFIG))

    print(
        f" [璈Ｘ瓢 {group} 瑼餃歇湔唬蒂撖怠 .env嚗"
        f"銝={HEDGE_CONFIG[group]['upper_threshold']}, "
        f"銝={HEDGE_CONFIG[group]['lower_threshold']}"
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
        return jsonify({"status": "error", "message": "撖蝣潮航炊"}), 401

    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"status": "error", "message": "enabled 敹 true/false"}), 400

    SEND_WEBHOOK = enabled
    persist_env_var("SEND_WEBHOOK", "true" if enabled else "false")
    print(f" [璈Ｘ瓢 芸撠瘝 (SEND_WEBHOOK) 撌脣: {SEND_WEBHOOK}嚗撌脣神 .env")

    return jsonify({"status": "ok", "send_webhook": SEND_WEBHOOK})


def start_dashboard_server():
    """刻臬瑁蝺 Flask 折Ｘ選銝敶梢蹂蜓頛胯"""
    from waitress import serve as _serve
    print(f" 璈折Ｘ踹歇嚗http://0.0.0.0:{DASHBOARD_PORT}/dashboard")
    _serve(dash_app, host="0.0.0.0", port=DASHBOARD_PORT, threads=4)


# ==============================================================================
#  Auto Delta Hedge Sender
# ==============================================================================
def trigger_delta_hedge(action: str, current_price: float, symbol: str, qty: int | float) -> bool:
    if not WEBHOOK_URL or not WEBHOOK_PASSPHRASE:
        print(f"[{symbol} 芸撠瘝蝟餌絞] 儭 WEBHOOK_URL  WEBHOOK_PASSPHRASE 芾身摰嚗芸啣箄銝柴")
        print(f" -> 雿: {action} {qty} 桐 {symbol} @ {current_price}\n")
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
        print(f"[{symbol} 芸撠瘝蝟餌絞]  菜葫 Delta 蝘鳴皞潮 Webhook 閮...")
        print(f" -> 雿: {action} {qty} 桐 {symbol} @ {current_price}\n")

        if SEND_WEBHOOK:
            response = requests.post(WEBHOOK_URL, json=payload, timeout=10)
            if response.status_code == 200:
                print(f"[{symbol} 芸撠瘝蝟餌絞]  Webhook 喲: {response.text}")
                return True
            print(f"[{symbol} 芸撠瘝蝟餌絞]  Webhook 喲憭望, 蝣: {response.status_code}, body={response.text}")
            return False

        print(f"[{symbol} 芸撠瘝蝟餌絞] 妒 SEND_WEBHOOK=False嚗桀箸葫閰行芋撘嚗芸祕箝")
        return True

    except Exception as e:
        print(f"[{symbol} 芸撠瘝蝟餌絞]  Webhook 隢瘙潛啣虜: {e}")
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
        "undPrice": (float(getattr(bid_comp, "undPrice", 0.0) or 0.0) + float(getattr(ask_comp, "undPrice", 0.0) or 0.0)) / 2.0,
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
            und_price = float(comp.get("undPrice", 0.0))
        else:
            delta = float(comp.delta)
            gamma = float(comp.gamma)
            theta = float(comp.theta)
            und_price = float(getattr(comp, "undPrice", 0.0) or 0.0)

        if not (_finite_number(delta) and _finite_number(gamma) and _finite_number(theta)):
            return None

        return delta, theta, gamma, source, und_price

    except Exception as e:
        print(f"儭 IB Greeks 霈憭望: {getattr(contract, 'localSymbol', contract.symbol)}, error={e}")
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
    current_ts = time.time()

    #  Important change: use abs(total_gamma), otherwise short gamma becomes fake 0.01.
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
        print(f" [{group_name}] 瘝歹怠撠瘝")
        return

    if (current_ts - last_time) > cooldown_seconds:
        if total_delta > upper_threshold:
            print(
                f"\n [{group_name} 閫貊奭 蝮 Delta ({total_delta:.2f}) > 閫貊潔 {upper_threshold:.2f} "
                f"(桅瑼餌 {hedge_up_points:.0f} 暺)嚗瑁鞈箝撠瘝嚗"
            )
            success = trigger_delta_hedge("SELL", underlying_price, symbol=hedge_symbol, qty=hedge_qty)
            if success:
                last_hedge_times_dict[group_name] = current_ts
        elif total_delta < lower_threshold:
            print(
                f"\n [{group_name} 閫貊奭 蝮 Delta ({total_delta:.2f}) < 閫貊潔 {lower_threshold:.2f} "
                f"(桅瑼餌 {hedge_down_points:.0f} 暺)嚗瑁鞎琿脯撠瘝嚗"
            )
            success = trigger_delta_hedge("BUY", underlying_price, symbol=hedge_symbol, qty=hedge_qty)
            if success:
                last_hedge_times_dict[group_name] = current_ts
    else:
        if total_delta > upper_threshold or total_delta < lower_threshold:
            rem_time = int(cooldown_seconds - (current_ts - last_time))
            print(f" [{group_name} 芸撠瘝] Delta 撌脤璅 ({total_delta:.2f})嚗蝟餌絞瑕颱葉... 拚 {rem_time} 蝘")


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
        print("儭 Shioaji API key/secret 芾身摰嚗仿瘞貉餃乓")
        return None

    api = sj.Shioaji()
    api.login(SHIOAJI_API_KEY, SHIOAJI_SECRET_KEY)

    if SHIOAJI_CA_PATH and SHIOAJI_CA_PASSWD:
        api.activate_ca(ca_path=SHIOAJI_CA_PATH, ca_passwd=SHIOAJI_CA_PASSWD)
    else:
        print("儭 Shioaji CA path/password 芾身摰嚗仿銝株鋆銝")

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
                print(f"--- 甇券蝺 IB TWS ({IB_HOST}:{IB_PORT})... ---")
                ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, timeout=15)
                ib.reqMarketDataType(4)

                print(">>> IB 交嚗")

            acc_info = get_account_details(ib)
            recent_fills = get_recent_executions(ib)
            portfolio_data = get_positions_with_pnl(ib, ticker_decimals_map)
            orders = ib.reqAllOpenOrders()

            #clear_screen()

            # --- Orders ---
            dashboard_orders = []
            print(f"\n--- [  (Orders: {len(orders)}) ] ---")
            if not orders:
                print("桀⊥柴")
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
            print(f"\n--- [ 餈 10 蝑鈭 (IB) ] ---")
            if not recent_fills:
                print("桀⊥鈭斤")
            else:
                print(f"{'':<10} {'':<20} {'雿':<6} {'寞':<10} {'撟喳':<10}")
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
            print(f"\n--- [  IB 蝮賡◢ (Positions: {len(portfolio_data)}) ] ---")

            if portfolio_data:
                grouped_data = {k: [] for k in HEDGE_CONFIG.keys()}
                grouped_data['芸憿(Other)'] = []

                group_underlying = {k: 0.0 for k in HEDGE_CONFIG.keys()}
                group_mute_flag = {k: False for k in HEDGE_CONFIG.keys()}
                stock_prices = {}

                # First pass: get true hedge underlying price.
                #  Options are excluded so that option localSymbol will not overwrite the underlying price.
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

                    # 撠 VIX 撠瘝嚗敹賜仿豢甈 (KORU/SOXL options)
                    if my_group_name == 'VIX撠瘝' and sec_type in ['OPT', 'FOP']:
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
                                    print(f" [{my_group_name}]  {sym_disp} 蝻箔鞎函曉對摰券嚗")
                                if sym_disp.upper().startswith(group_info['hedge_sym'].upper()):
                                    pos_delta = 1.0 * qty
                                else:
                                    pos_delta = 1.0 * qty * micro_ratio

                            elif sec_type in ['STK', 'CRYPTO']:
                                mkt_p = float(item.get('marketPrice') or 0.0)
                                if mkt_p <= 0 and qty != 0:
                                    group_mute_flag[my_group_name] = True
                                    print(f" [{my_group_name}]  {sym_disp} 蝻箔曉孵勗對摰券嚗")
                                elif is_value_hedge:
                                    future_price = group_underlying.get(my_group_name, 0.0)
                                    if future_price > 0:
                                        future_notional = future_price * target_micro_mult
                                        pos_delta = (qty * mkt_p) / future_notional
                                    else:
                                        group_mute_flag[my_group_name] = True
                                        print(f" [{my_group_name}]  蝻箔撠瘝璅 ({group_info['hedge_sym']}) 勗對摰券嚗")
                                else:
                                    pos_delta = 1.0 * qty * micro_ratio

                            elif sec_type in ['FOP', 'OPT']:
                                strike = float(item.get('strike', 0))
                                opt_mkt_price = float(item['marketPrice'])
                                option_contract = item.get('contract')

                                if opt_mkt_price <= 0 or strike <= 0 or option_contract is None:
                                    group_mute_flag[my_group_name] = True
                                    print(f" [{my_group_name}]  {sym_disp} 蝻箔甈曉寞蝝鞈嚗摰券嚗")
                                else:
                                    ib_greeks = get_ib_option_greeks(
                                        ib_client=ib,
                                        contract=option_contract,
                                        wait_seconds=IB_GREEKS_WAIT_SECONDS,
                                    )

                                    if ib_greeks is None:
                                        group_mute_flag[my_group_name] = True
                                        print(f" [{my_group_name}]  {sym_disp} 蝻箔 IB Greeks嚗摰券嚗撘瑕園單怠撠瘝嚗")
                                    else:
                                        d, t, g, greek_source, und_price = ib_greeks
                                        item['greek_source'] = greek_source
                                        if und_price > 0 and group_underlying.get(my_group_name, 0.0) <= 0:
                                            group_underlying[my_group_name] = und_price

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
                                                print(f" [{my_group_name}]  {sym_disp} 蝻箔璅/撠瘝璅勗對摰券嚗")
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
                                        d, t, g, greek_source, und_price = ib_greeks
                                        item['greek_source'] = greek_source
                                        pos_delta = d * qty * contract_multiplier
                                        pos_theta = t * qty * contract_multiplier
                                        pos_gamma = g * qty * contract_multiplier
                    except Exception as e:
                        print(f"儭 Greek 閮蝞憭望: {sym_disp}, error={e}")

                    item['disp_delta'] = f"{pos_delta:.5f}" if abs(pos_delta) > 0.00001 else "0.0000"
                    item['disp_theta'] = f"{pos_theta:.0f}" if abs(pos_theta) > 0.00001 else "0"
                    item['disp_gamma'] = f"{pos_gamma:.5f}" if abs(pos_gamma) > 0.00001 else "0.0000"

                    if my_group_name:
                        grouped_data[my_group_name].append(item)
                        group_greeks[my_group_name]['delta'] += pos_delta
                        group_greeks[my_group_name]['gamma'] += pos_gamma
                        group_greeks[my_group_name]['theta'] += pos_theta
                    else:
                        grouped_data['芸憿(Other)'].append(item)

                print(f"{'':<20} {'其':<6} {'':<10} {'曉':<10} {'(P&L)':<13} {'蝮賢孵(USD)':<10} {'敺格':<10} {'敺格庛':<10} {'唳':<10}")
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
                        dte_str = ""
                        expiry = item.get('expiry', '')
                        if expiry and len(expiry) >= 8:
                            try:
                                exp_date = datetime.datetime.strptime(expiry[:8], "%Y%m%d").date()
                                dte = (exp_date - datetime.datetime.now().date()).days
                                dte_str = str(dte)
                            except:
                                dte_str = expiry

                        c_str = format_price(item['avgCost'], item['decimals'])
                        m_str = format_price(item['marketPrice'], item['decimals'])
                        p_str = f"{item['pnl']:+,.2f}"
                        v_str = f"{item['totalCost']:,.1f}"
                        print(
                            f"{item['_disp']:<20} {item['position']:<8.1f} {c_str:<12} {m_str:<12} "
                            f"{p_str:<15} {v_str:<12} {item['disp_delta']:<10} {item['disp_theta']:<10} "
                            f"{dte_str:<10}"
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
                            "expiry": expiry,
                            "dte": dte_str,
                        })

                    ref_points = None
                    if g_name != '芸憿(Other)':
                        # 憒閰脩黎蝯其嚗雿蝻箔撠瘝璅勗對撘瑕嗅摰券
                        if group_underlying.get(g_name, 0.0) <= 0:
                            group_mute_flag[g_name] = True

                        if group_mute_flag.get(g_name, False):
                            print(f" [{g_name}]  蝻箔勗對摰券嚗撘瑕園喉")
                        elif abs(group_greeks[g_name]['delta']) > 0.001 or abs(group_greeks[g_name]['gamma']) > 0.001:
                            u_th = HEDGE_CONFIG[g_name]['upper_threshold']
                            l_th = HEDGE_CONFIG[g_name]['lower_threshold']
                            ref_points = (max(abs(u_th), abs(l_th)) / max(abs(group_greeks[g_name]['gamma']), 0.01)) * 100.0
                            print(
                                f" [{g_name}] 桅隡啗={ref_points:.0f}暺 "
                                f"臭={u_th:.2f}嚗銝={l_th:.2f} | "
                                f"嗅 {HEDGE_CONFIG[g_name]['hedge_sym']} ={group_greeks[g_name]['delta']:.2f} 庛={group_greeks[g_name]['theta']:.0f}"
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
                print(f"匈B瘛典: {net_liq_value:,}  啣舐券: {avail_value:,}")
                dashboard_account["net_liq"] = net_liq_value
                dashboard_account["avail"] = avail_value
            except Exception:
                print(f"匈B瘛典: {acc_info['net_liq']}  啣舐券: {acc_info['avail']}")

            # Shioaji / Taiwan futures section
            dashboard_shioaji = {}
            if api is not None:
                try:
                    margin = api.margin(api.futopt_account)
                    if margin:
                        print(f"瘞貉甈: {int(margin.equity_amount):,}  啣臬粹: {int(margin.available_margin):,}")
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
                                "": f"{position_delta_tmf:.5f}",
                                "帠": f"{position_gamma_tmf:.5f}",
                                "庛": f"{position_theta_tmf:.0f}",
                            })

                        print(pd.DataFrame(df_list).to_string(index=False))
                        tmf_config = HEDGE_CONFIG['唳(TMF)']
                        u_th = tmf_config['upper_threshold']
                        l_th = tmf_config['lower_threshold']
                        current_time_str = datetime.datetime.now().strftime('%H:%M')
                        is_tw_closed = ("04:59" < current_time_str < "08:46" or "13:44" < current_time_str < "15:01")

                        tmf_ref_points = None
                        if tmf_mute_flag:
                            print(" [唳(TMF)]  蝻箔甈曉對摰券嚗撘瑕園單怠撠瘝嚗")
                        elif is_tw_closed:
                            print(" [唳(TMF)] 瘝歹怠撠瘝")
                        else:
                            tmf_ref_points = (max(abs(u_th), abs(l_th)) / max(abs(total_portfolio_gamma_tmf), 0.01)) * 100.0
                            print(
                                f" [唳(TMF)] 桅瑼颱摯閮={tmf_ref_points:.0f}暺 "
                                f"臭={u_th:.2f}嚗銝={l_th:.2f} "
                                f"舐嗅 TMF ={total_portfolio_delta_tmf:.2f} 庛={total_portfolio_theta_tmf:.0f}"
                            )
                            evaluate_and_trigger_hedge(
                                group_name='唳(TMF)',
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
                            "name": "唳(TMF)",
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
                                    "delta": float(d[""]),
                                    "theta": float(d["庛"]),
                                    "gamma": float(d["帠"]),
                                    "expiry": "",
                                }
                                for d in df_list
                            ],
                        })
                except Exception as e:
                    print(f"儭 瘞貉/唳畾萇仿: {e}")

            # --- 敶港蒂湔唳璈Ｘ踹翰 ---
            total_all_theta = 0.0
            for g in dashboard_groups:
                if g['name'] != '唳(TMF)':
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

            print(f"敺湔: {now_dt.strftime('%H:%M:%S')} | 銝甈⊥湔: {REFRESH_SECONDS}蝘敺")
            ib.sleep(REFRESH_SECONDS)

        except KeyboardInterrupt:
            print("\n蝔撘銝剜瑯")
            if ib.isConnected():
                ib.disconnect()
            break
        except Exception as e:
            print(f"潛航炊: {e}")
            if ib.isConnected():
                ib.disconnect()
            time.sleep(10)


if __name__ == '__main__':
    main()
