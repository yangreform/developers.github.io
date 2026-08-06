import re

def fix_html():
    with open('docs/index.html', 'r', encoding='utf-8') as f:
        content = f.read()

    # 1. Update the Data Period column header
    content = content.replace('>数据期间<', '>数据开始年份<')
    
    # 2. Add Developer Header if missing (find 楼盘名称 and add if missing)
    if '开发商</th>' not in content:
        content = re.sub(
            r'(<th[^>]*>楼盘名称</th>)',
            r'\1\n            <th style="width:120px; text-align:left">开发商</th>',
            content
        )
        
    # 3. Change JS period string format
    old_period = "var period = d.earliest_yr + '–' + d.latest_yr + ' (' + d.n_years + '年)';"
    new_period = "var period = d.earliest_yr + ' (' + d.n_years + '年)';"
    content = content.replace(old_period, new_period)

    with open('docs/index.html', 'w', encoding='utf-8') as f:
        f.write(content)

    print("Success")

if __name__ == '__main__':
    fix_html()
