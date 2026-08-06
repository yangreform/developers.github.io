import re

with open('docs/index.html', 'r', encoding='utf-8') as f:
    content = f.read()

# Add the Tab Button
if 'data-tab="commercial-tab"' not in content:
    content = content.replace(
        '<button class="tab-btn" data-tab="dev-tab">&#127968; 🏠 私宅：依开发商</button>',
        '<button class="tab-btn" data-tab="dev-tab">&#127968; 🏠 私宅：依开发商</button>\n      <button class="tab-btn" data-tab="commercial-tab">&#127980; 🏢 商办 / 店面</button>'
    )

# Add the Tab Panel
if 'id="commercial-tab"' not in content:
    commercial_panel = '''
    <!-- COMMERCIAL Tab -->
    <div id="commercial-tab" class="tab-panel">
      <div class="search-bar">
        <input class="search-input" id="com-search" placeholder="&#128269; 搜寻楼盘、街道或类型..." oninput="filterTable('com-table','com-search','com-count')">
        <span class="result-count" id="com-count"></span>
      </div>
      <div class="table-wrap">
        <table id="com-table">
          <thead>
            <tr>
              <th>#</th>
              <th>楼盘名称</th>
              <th>街道</th>
              <th>类型</th>
              <th>年限</th>
              <th>面积 (sqm)</th>
              <th style="cursor:pointer; color:var(--accent);" onclick="sortComTable('price')">总价 (S$) &#8693;</th>
              <th style="cursor:pointer; color:var(--accent);" onclick="sortComTable('psf')">单价 (S$ psf) &#8693;</th>
              <th style="cursor:pointer; color:var(--accent);" onclick="sortComTable('date')">成交日期 &#8693;</th>
            </tr>
          </thead>
          <tbody id="com-tbody">
            <tr><td colspan="9" class="no-data">载入中...</td></tr>
          </tbody>
        </table>
      </div>
    </div>
    '''
    content = content.replace('<!-- Disclaimer & Contact Section -->', commercial_panel + '\n<!-- Disclaimer & Contact Section -->')

# Add the JS Logic
if 'function loadCommercialData' not in content:
    js_logic = '''
  var comDataFull = [];
  var comSortCol = 'price';
  var comSortAsc = false;

  async function loadCommercialData() {
    try {
      var resp = await fetch(API_BASE + '/api/commercial_transactions', FETCH_OPTS);
      var json = await resp.json();
      if (json.status !== 'success') throw new Error(json.message || '资料载入失败');
      
      comDataFull = json.data || [];
      renderComTable();
    } catch(e) {
      document.getElementById('com-tbody').innerHTML = '<tr><td colspan="9" class="no-data">无法载入：' + e.message + '</td></tr>';
    }
  }

  function sortComTable(col) {
    if (comSortCol === col) {
      comSortAsc = !comSortAsc;
    } else {
      comSortCol = col;
      comSortAsc = false; // default desc
    }
    
    comDataFull.sort(function(a, b) {
      var valA, valB;
      if (col === 'price') {
        valA = a.price_sgd || 0;
        valB = b.price_sgd || 0;
      } else if (col === 'psf') {
        valA = a.psf_sgd || 0;
        valB = b.psf_sgd || 0;
      } else if (col === 'date') {
        // Date format might be MMM-YYYY, parsing it is tricky so we do basic string compare or parse Date
        // Actually URA returns MM-YYYY or similar. Let's do string compare as fallback, or parse if possible.
        var dA = new Date(a.contract_date);
        var dB = new Date(b.contract_date);
        valA = isNaN(dA) ? a.contract_date : dA.getTime();
        valB = isNaN(dB) ? b.contract_date : dB.getTime();
      }
      
      if (valA < valB) return comSortAsc ? -1 : 1;
      if (valA > valB) return comSortAsc ? 1 : -1;
      return 0;
    });
    
    renderComTable();
    // Re-apply filter if exists
    filterTable('com-table', 'com-search', 'com-count');
  }

  function renderComTable() {
    var tbody = document.getElementById('com-tbody');
    if (!comDataFull.length) {
      tbody.innerHTML = '<tr><td colspan="9" class="no-data">暂无商办/店面资料，请执行爬虫抓取</td></tr>';
      return;
    }
    
    var html = '';
    comDataFull.forEach(function(d, i) {
      var priceStr = d.price_sgd ? 'S$ ' + d.price_sgd.toLocaleString() : '—';
      var psfStr = d.psf_sgd ? 'S$ ' + d.psf_sgd.toLocaleString() : '—';
      var typeColor = d.property_type.toLowerCase().includes('office') ? 'color: #63b3ed;' : 'color: #f0c84a;';
      
      html += '<tr data-row="1">';
      html += '<td style="color:var(--muted)">' + (i+1) + '</td>';
      html += '<td><strong>' + d.project_name + '</strong></td>';
      html += '<td>' + d.street_name + '</td>';
      html += '<td style="font-weight:bold; ' + typeColor + '">' + d.property_type + '</td>';
      html += '<td style="color:var(--muted); font-size:12px;">' + d.tenure + '</td>';
      html += '<td>' + d.area_sqm + '</td>';
      html += '<td class="price" style="color:#4ade80">' + priceStr + '</td>';
      html += '<td style="color:var(--muted)">' + psfStr + '</td>';
      html += '<td style="font-size:12px;">' + d.contract_date + '</td>';
      html += '</tr>';
    });
    tbody.innerHTML = html;
    document.getElementById('com-count').textContent = comDataFull.length + ' 笔商办/店面资料';
  }
    '''
    # Append to window.addEventListener('load')
    content = content.replace("loadURAData();", "loadURAData();\n    loadCommercialData();")
    
    # Append functions at the end before </script>
    content = content.replace("</script>\n</body>", js_logic + "\n</script>\n</body>")

with open('docs/index.html', 'w', encoding='utf-8') as f:
    f.write(content)

print("Commercial UI successfully injected.")
