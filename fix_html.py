import re

with open('docs/index.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Replace the entire Tabs and Panels section
# We'll use regex to find `<div class="tabs">` up to the `<div class="map-section">` which follows the tabs.
# Actually, it's safer to find the map section and replace everything between tabs and map section.
match = re.search(r'(<div class="tabs">.*?)(<div class="map-section">)', content, flags=re.DOTALL)
if match:
    old_tabs_block = match.group(1)
    
    new_tabs_block = '''<div class="tabs">
      <button class="tab-btn active" data-tab="ura-tab">&#127961;&#65039; 🏙️ 私宅：依楼盘</button>
      <button class="tab-btn" data-tab="dev-tab">&#127968; 🏠 私宅：依开发商</button>
    </div>

    <!-- URA Tab -->
    <div id="ura-tab" class="tab-panel active">
      <div class="search-bar">
        <input class="search-input" id="ura-search" placeholder="&#128269; 搜寻楼盘或街道名称..." oninput="filterTable('ura-table','ura-search','ura-count')">
        <span class="result-count" id="ura-count"></span>
      </div>
      <div class="table-wrap">
        <table id="ura-table">
          <thead>
            <tr>
              <th>#</th>
              <th>楼盘名称</th>
            <th style="width:120px; text-align:left">开发商</th>
              <th>邮递区号</th>
              <th>最新均价 (S$)</th>
              <th>均呎价 (S$)</th>
              <th>年均涨幅</th>
              <th>3年后预估价 (S$)</th>
              <th>数据开始年份</th>
            </tr>
          </thead>
          <tbody id="ura-tbody">
            <tr><td colspan="9" class="no-data">载入中...</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- DEV Tab -->
    <div id="dev-tab" class="tab-panel">
      <div class="search-bar">
        <input class="search-input" id="dev-search" placeholder="&#128269; 搜寻开发商名称..." oninput="filterTable('dev-table','dev-search','dev-count')">
        <span class="result-count" id="dev-count"></span>
      </div>
      <div class="table-wrap">
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
    content = content.replace(old_tabs_block, new_tabs_block)


# 2. Fix JS errors:
# Remove loadHDBData() and loadRentalData() from window.addEventListener('load')
content = content.replace("loadHDBData();", "loadDevelopers();")
content = content.replace("loadRentalData();", "")

# Remove the old loadDevelopers() block if it exists at the bottom to avoid duplicates
content = re.sub(r'let devDataFull = \[\];.*?renderDevTable\(\)\s*\}', '', content, flags=re.DOTALL)

# Inject the clean Dev JS at the end of the script
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
        let tbody = document.getElementById('dev-tbody');
        if(tbody) tbody.innerHTML = '<tr><td colspan="5" class="no-data">无法载入：' + e.message + '</td></tr>';
      }
    }

    function sortDevTable(col) {
      if (devSortCol === col) {
        devSortAsc = !devSortAsc; 
      } else {
        devSortCol = col;
        devSortAsc = false; 
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
      if(!tbody) return;
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
      let countEl = document.getElementById('dev-count');
      if(countEl) countEl.textContent = devDataFull.length + ' 个开发商';
      
      filterTable('dev-table','dev-search','dev-count');
    }
'''
content = content.replace("</script>\n</body>", dev_js + "\n</script>\n</body>")

with open('docs/index.html', 'w', encoding='utf-8') as f:
    f.write(content)

print("HTML completely fixed")
