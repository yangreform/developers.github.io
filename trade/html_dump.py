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
                document.getElementById('portfolio_content').innerHTML = html;

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