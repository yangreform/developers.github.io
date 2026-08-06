import re

with open('docs/index.html', 'r', encoding='utf-8') as f:
    content = f.read()

dev_js = '''
    function showDevProjects(devName) {
      if (!window.uraDataFull) return;
      
      let subTableDiv = document.getElementById('dev-projects-container');
      if (!subTableDiv) {
        subTableDiv = document.createElement('div');
        subTableDiv.id = 'dev-projects-container';
        subTableDiv.style.cssText = 'margin-top: 20px; padding: 15px; background: var(--navy); border-radius: 8px; border: 1px solid var(--border); box-shadow: 0 4px 6px rgba(0,0,0,0.3);';
        // Append it to dev-tab
        let devTab = document.getElementById('dev-tab');
        if(devTab) devTab.appendChild(subTableDiv);
      }
      
      // Filter uraDataFull
      let projects = window.uraDataFull.filter(p => p.developer === devName);
      
      // Render HTML
      let html = '<h3 style="margin-top:0; color:var(--text); font-size:16px;">&#127960;&#65039; ' + devName + ' 的旗下楼盘 (' + projects.length + ')</h3>';
      html += '<div style="overflow-x:auto;"><table style="width:100%; border-collapse:collapse; font-size:13px; text-align:left;">';
      html += '<thead><tr style="border-bottom:1px solid var(--border); color:var(--muted);"><th style="padding:8px;">#</th><th style="padding:8px;">楼盘名称</th><th style="padding:8px;">最新均价 (S$)</th><th style="padding:8px;">年均涨幅</th></tr></thead><tbody>';
      
      if(projects.length === 0) {
         html += '<tr><td colspan="4" style="padding:8px;text-align:center;color:var(--muted);">此开发商暂无对应的 URA 成交纪录</td></tr>';
      } else {
         projects.forEach((p, i) => {
           var cagrCls = p.cagr > 3 ? 'color-up' : (p.cagr < -3 ? 'color-down' : '');
           var cagrArrow = p.cagr >= 0 ? '&#9650;' : '&#9660;';
           var priceStr = p.latest_price ? 'S$ ' + p.latest_price.toLocaleString() : '—';
           var cagrStr = (p.cagr !== null && p.cagr !== undefined) ? cagrArrow + ' ' + Math.abs(p.cagr).toFixed(1) + '% / 年' : '—';
           
           html += '<tr style="border-bottom:1px solid rgba(255,255,255,0.05); cursor:pointer; transition: background 0.2s;" onclick="jumpToUraProject(\'' + (p.project || '').replace(/'/g, "\\'") + '\')" onmouseover="this.style.background=\'var(--hover)\'" onmouseout="this.style.background=\'\'">';
           html += '<td style="padding:8px; color:var(--muted);">' + (i+1) + '</td>';
           html += '<td style="padding:8px;"><strong>' + p.project + '</strong></td>';
           html += '<td style="padding:8px;" class="price">' + priceStr + '</td>';
           html += '<td style="padding:8px;" class="' + cagrCls + '">' + cagrStr + '</td>';
           html += '</tr>';
         });
      }
      
      html += '</tbody></table></div>';
      subTableDiv.innerHTML = html;
      
      subTableDiv.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function jumpToUraProject(projectName) {
      if(!projectName) return;
      
      // 1. Switch to URA tab
      document.querySelectorAll('.tab-btn').forEach(function(b) { b.classList.remove('active'); });
      document.querySelectorAll('.tab-panel').forEach(function(p) { p.classList.remove('active'); });
      
      let uraBtn = document.querySelector('[data-tab="ura-tab"]');
      let uraTab = document.getElementById('ura-tab');
      if(uraBtn) uraBtn.classList.add('active');
      if(uraTab) uraTab.classList.add('active');
      
      // 2. Set search filter
      let searchInput = document.getElementById('ura-search');
      if (searchInput) {
        searchInput.value = projectName;
        // manually call filterTable
        filterTable('ura-table', 'ura-search', 'ura-count');
      }
      
      window.scrollTo({ top: uraTab.offsetTop - 50, behavior: 'smooth' });
    }
</script>
</body>'''

content = content.replace("</script>\n</body>", dev_js)

with open('docs/index.html', 'w', encoding='utf-8') as f:
    f.write(content)
print("Injected UI interactivity for developer tab.")
