import re

with open('docs/index.html', 'r', encoding='utf-8') as f:
    html = f.read()

# 1. Fix Chart.getChart
html = html.replace("let existingChart = Chart.getChart(ctx);", "let existingChart = Chart.getChart('macroChart');")

# 2. Fix the `d is not defined` error for all 3 tables
# The bad string currently in the file:
bad_tr = '''<tr data-row="1" style="cursor:pointer; transition: background 0.2s;" onclick="window.location.href='heatmap.html?project=' + encodeURIComponent(d.project)" onmouseover="this.style.background='var(--hover)'" onmouseout="this.style.background=''">'''

# We will replace all occurrences with a unique placeholder, then manually restore them
pieces = html.split(bad_tr)
if len(pieces) == 4: # Meaning 3 occurrences
    # piece 0 is before HDB table
    # piece 1 is between HDB and URA table
    # piece 2 is between URA and Rental table
    # piece 3 is after Rental table
    
    hdb_tr = '<tr data-row="1">'
    rental_tr = '<tr data-row="1">'
    # URA table has 'd.project' properly interpolated in JS
    ura_tr = """<tr data-row="1" style="cursor:pointer; transition: background 0.2s;" onclick="window.location.href='heatmap.html?project=' + encodeURIComponent(d.project || d.label)" onmouseover="this.style.background='var(--hover)'" onmouseout="this.style.background=''">"""
    
    html = pieces[0] + hdb_tr + pieces[1] + ura_tr + pieces[2] + rental_tr + pieces[3]

# 3. Reorder tabs and add emojis
old_tabs = '''<div class="tabs">
      <button class="tab-btn active" data-tab="tab-hdb">HDB 組屋（依市鎮）</button>
      <button class="tab-btn" data-tab="tab-ura">URA 私宅（樓盤列表）</button>
      <button class="tab-btn" data-tab="tab-rental">HDB 租賃</button>
    </div>'''

new_tabs = '''<div class="tabs">
      <button class="tab-btn active" data-tab="tab-ura">🏙️ URA 私宅（樓盤列表）</button>
      <button class="tab-btn" data-tab="tab-hdb">🏠 HDB 組屋（依市鎮）</button>
      <button class="tab-btn" data-tab="tab-rental">📄 HDB 租賃</button>
    </div>'''
html = html.replace(old_tabs, new_tabs)

# Also need to reorder the panels and update active class
# Find the panels
panel_hdb = re.search(r'<div id="tab-hdb".*?</div>\s*</div>\s*</div>', html, re.DOTALL)
panel_ura = re.search(r'<div id="tab-ura".*?</div>\s*</div>\s*</div>', html, re.DOTALL)
panel_rental = re.search(r'<div id="tab-rental".*?</div>\s*</div>\s*</div>', html, re.DOTALL)

if panel_hdb and panel_ura and panel_rental:
    # Remove old active classes and set URA to active
    hdb_str = panel_hdb.group(0).replace('class="tab-panel active"', 'class="tab-panel"')
    ura_str = panel_ura.group(0).replace('class="tab-panel"', 'class="tab-panel active"')
    rental_str = panel_rental.group(0).replace('class="tab-panel active"', 'class="tab-panel"')
    
    # Replace the whole sequence of panels
    panels_start = html.find('<div id="tab-hdb"')
    panels_end = html.find('<div id="tab-rental"') + len(panel_rental.group(0))
    
    new_panels = ura_str + '\n    ' + hdb_str + '\n    ' + rental_str
    html = html[:panels_start] + new_panels + html[panels_end:]

# 4. Add CAGR table logic to JS
# We find where macroChartInstance is created and append the table code
chart_creation = "macroChartInstance = new Chart(ctx, {"
cagr_js = """
    // === Add CAGR Table ===
    let cagrTable = document.getElementById('cagrTable');
    if (!cagrTable) {
      cagrTable = document.createElement('div');
      cagrTable.id = 'cagrTable';
      cagrTable.style.cssText = 'margin-top:15px; padding:15px; background:var(--navy); border-radius:8px; border:1px solid var(--border); overflow-x:auto;';
      chartContainer.appendChild(cagrTable);
    }
    
    function calcCagr(startVal, endVal, years) {
      if(years <= 0 || !startVal) return '0.0%';
      const rate = (Math.pow(endVal / startVal, 1 / years) - 1) * 100;
      return (rate > 0 ? '+' : '') + rate.toFixed(1) + '%';
    }
    
    const yearsDiff = eYear - sYear;
    const ccrCagr = calcCagr(100.0, ccrData[ccrData.length-1], yearsDiff);
    const rcrCagr = calcCagr(100.0, rcrData[rcrData.length-1], yearsDiff);
    const ocrCagr = calcCagr(100.0, ocrData[ocrData.length-1], yearsDiff);
    const fdCagr = calcCagr(100.0, fdData[fdData.length-1], yearsDiff);
    const cpiCagr = calcCagr(100.0, cpiData[cpiData.length-1], yearsDiff);
    
    cagrTable.innerHTML = `
      <h4 style="margin:0 0 10px 0; color:var(--text); font-size:14px;">區間平均年化複合增長率 (CAGR): ${sYear} ~ ${eYear}</h4>
      <table style="width:100%; text-align:center; font-size:13px; border-collapse:collapse;">
        <tr style="border-bottom:1px solid var(--border); color:var(--muted);">
          <th style="padding:5px;">CCR (核心中央區)</th>
          <th style="padding:5px;">RCR (中央區以外)</th>
          <th style="padding:5px;">OCR (中央區外圍)</th>
          <th style="padding:5px;">定存利率</th>
          <th style="padding:5px;">通貨膨脹</th>
        </tr>
        <tr>
          <td style="padding:8px; font-weight:bold; color:#a0aec0;">${ccrCagr}</td>
          <td style="padding:8px; font-weight:bold; color:#f0c84a;">${rcrCagr}</td>
          <td style="padding:8px; font-weight:bold; color:#9f7aea;">${ocrCagr}</td>
          <td style="padding:8px; font-weight:bold; color:#63b3ed;">${fdCagr}</td>
          <td style="padding:8px; font-weight:bold; color:#fc8181;">${cpiCagr}</td>
        </tr>
      </table>
    `;

    macroChartInstance = new Chart(ctx, {"""
    
html = html.replace(chart_creation, cagr_js)

with open('docs/index.html', 'w', encoding='utf-8') as f:
    f.write(html)
print("Updated index.html successfully.")
