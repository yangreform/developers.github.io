import re

with open('docs/index.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Update Tabs HTML
tabs_html_old = '''<button class="tab-btn active" data-tab="ura-tab">&#127961;&#65039; URA 私宅（楼盘列表）</button>
      <button class="tab-btn" data-tab="hdb-tab">&#127968; HDB 组屋（依市镇）</button>
      <button class="tab-btn" data-tab="rental-tab">&#128196; HDB 租赁</button>'''

tabs_html_new = '''<button class="tab-btn active" data-tab="ura-tab">&#127961;&#65039; 🏙️ 私宅：依楼盘</button>
      <button class="tab-btn" data-tab="dev-tab">&#127968; 🏠 私宅：依开发商</button>'''

content = content.replace(tabs_html_old, tabs_html_new)

# 2. Add Developer Tab content and remove HDB/Rental tabs
# We'll locate the <div id="ura-tab" class="tab-panel active">...</div>
# And replace everything after it up to the footer with the new dev-tab.
# Using regex to find the end of ura-tab and start of hdb-tab.

dev_tab_html = '''
    <!-- 开发商统计分页 -->
    <div id="dev-tab" class="tab-panel">
      <div class="search-bar">
        <input class="search-input" id="dev-search" placeholder="&#128269; 搜寻开发商名称..." oninput="filterTable('dev-table','dev-search','dev-count')">
        <span class="result-count" id="dev-count"></span>
      </div>
      <div class="table-container">
        <table id="dev-table">
          <thead>
            <tr>
              <th>#</th>
              <th>开发商</th>
              <th style="cursor:pointer;" onclick="sortDevTable('total_projects')">总楼盘数 &#8693;</th>
              <th style="cursor:pointer;" onclick="sortDevTable('avg_price')">最新均价 (S$) &#8693;</th>
              <th style="cursor:pointer;" onclick="sortDevTable('avg_cagr')">年均涨幅 &#8693;</th>
            </tr>
          </thead>
          <tbody id="dev-tbody">
            <tr><td colspan="5" class="no-data">载入中...</td></tr>
          </tbody>
        </table>
      </div>
    </div>
'''

# Find the HDB and Rental tab sections and remove them
content = re.sub(r'<div id="hdb-tab" class="tab-panel">.*?</script>\s*</div>', '', content, flags=re.DOTALL)
content = re.sub(r'<div id="rental-tab" class="tab-panel">.*?</script>\s*</div>', dev_tab_html, content, flags=re.DOTALL)

# Also remove HDB logic from JS
# "loadHdb();"
content = content.replace("loadHdb();", "loadDevelopers();")
content = content.replace("loadRentals();", "")

# Remove the loadHdb and loadRentals functions
content = re.sub(r'async function loadHdb\(\).*?\}\s*\}', '', content, flags=re.DOTALL)
content = re.sub(r'async function loadRentals\(\).*?\}\s*\}', '', content, flags=re.DOTALL)

# Inject loadDevelopers and sortDevTable
dev_js = '''
    let devDataFull = [];
    let devSortCol = 'total_projects';
    let devSortAsc = false;

    async function loadDevelopers() {
      try {
        var resp = await fetch(API_BASE + '/api/developer_stats', FETCH_OPTS);
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        var json = await resp.json();
        
        if (json.status === 'success' && json.data) {
          devDataFull = json.data;
          renderDevTable();
        } else {
          throw new Error(json.message || "Unknown error");
        }
      } catch (e) {
        document.getElementById('dev-tbody').innerHTML = '<tr><td colspan="5" class="no-data">无法载入：' + e.message + '</td></tr>';
      }
    }

    function sortDevTable(col) {
      if (devSortCol === col) {
        devSortAsc = !devSortAsc; // toggle
      } else {
        devSortCol = col;
        devSortAsc = (col === 'total_projects') ? false : false; // default desc
      }
      
      devDataFull.sort((a, b) => {
        let valA = a[col];
        let valB = b[col];
        if (valA < valB) return devSortAsc ? -1 : 1;
        if (valA > valB) return devSortAsc ? 1 : -1;
        return 0;
      });
      
      renderDevTable();
    }

    function renderDevTable() {
      var tbody = document.getElementById('dev-tbody');
      var html = '';
      
      devDataFull.forEach(function(d, index) {
        var cagrColor = d.avg_cagr >= 0 ? 'color-up' : 'color-down';
        var cagrArrow = d.avg_cagr >= 0 ? '&#9650;' : '&#9660;';
        
        html += '<tr>';
        html += '<td>' + (index + 1) + '</td>';
        html += '<td><strong>' + d.developer + '</strong></td>';
        html += '<td>' + d.total_projects + '</td>';
        html += '<td>S$ ' + d.avg_price.toLocaleString() + '</td>';
        html += '<td class="' + cagrColor + '"><strong>' + cagrArrow + ' ' + Math.abs(d.avg_cagr).toFixed(1) + '% / 年</strong></td>';
        html += '</tr>';
      });
      
      tbody.innerHTML = html;
      document.getElementById('dev-count').textContent = devDataFull.length + ' 个开发商';
      
      // Re-apply filter if there's any text in search
      filterTable('dev-table','dev-search','dev-count');
    }
'''

# Add dev_js to the end of the script block (before </script> at the bottom)
content = content.replace("</script>\n</body>", dev_js + "\n</script>\n</body>")

with open('docs/index.html', 'w', encoding='utf-8') as f:
    f.write(content)

print("HTML Patched")
