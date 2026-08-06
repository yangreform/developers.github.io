import re

with open('docs/index.html', 'r', encoding='utf-8') as f:
    html = f.read()

# Fix the `d is not defined` error for all 3 tables
bad_tr = '''<tr data-row="1" style="cursor:pointer; transition: background 0.2s;" onclick="window.location.href='heatmap.html?project=' + encodeURIComponent(d.project)" onmouseover="this.style.background='var(--hover)'" onmouseout="this.style.background=''">'''

pieces = html.split(bad_tr)
if len(pieces) == 4:
    hdb_tr = '<tr data-row="1">'
    rental_tr = '<tr data-row="1">'
    ura_tr = """<tr data-row="1" style="cursor:pointer; transition: background 0.2s;" onclick="window.location.href='heatmap.html?project=' + encodeURIComponent(d.project || d.label)" onmouseover="this.style.background='var(--hover)'" onmouseout="this.style.background=''">"""
    html = pieces[0] + hdb_tr + pieces[1] + ura_tr + pieces[2] + rental_tr + pieces[3]

# Reorder tabs
old_tabs = '''<div class="tabs">
      <button class="tab-btn active" data-tab="hdb-tab">&#127968; HDB 組屋（依市鎮）</button>
      <button class="tab-btn" data-tab="ura-tab">&#127961;&#65039; URA 私宅（樓盤列表）</button>
      <button class="tab-btn" data-tab="rental-tab">&#128196; HDB 租賃</button>
    </div>'''
new_tabs = '''<div class="tabs">
      <button class="tab-btn active" data-tab="ura-tab">&#127961;&#65039; URA 私宅（樓盤列表）</button>
      <button class="tab-btn" data-tab="hdb-tab">&#127968; HDB 組屋（依市鎮）</button>
      <button class="tab-btn" data-tab="rental-tab">&#128196; HDB 租賃</button>
    </div>'''
html = html.replace(old_tabs, new_tabs)

# Reorder panels
panel_hdb = re.search(r'<div id="hdb-tab".*?</div>\s*</div>\s*</div>', html, re.DOTALL)
panel_ura = re.search(r'<div id="ura-tab".*?</div>\s*</div>\s*</div>', html, re.DOTALL)
panel_rental = re.search(r'<div id="rental-tab".*?</div>\s*</div>\s*</div>', html, re.DOTALL)

if panel_hdb and panel_ura and panel_rental:
    hdb_str = panel_hdb.group(0).replace('class="tab-panel active"', 'class="tab-panel"')
    ura_str = panel_ura.group(0).replace('class="tab-panel"', 'class="tab-panel active"')
    rental_str = panel_rental.group(0).replace('class="tab-panel active"', 'class="tab-panel"')
    
    panels_start = html.find('<div id="hdb-tab"')
    panels_end = html.find('<div id="rental-tab"') + len(panel_rental.group(0))
    
    new_panels = ura_str + '\n    ' + hdb_str + '\n    ' + rental_str
    html = html[:panels_start] + new_panels + html[panels_end:]

with open('docs/index.html', 'w', encoding='utf-8') as f:
    f.write(html)
print("Tabs and onclick bugs fixed.")
