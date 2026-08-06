import os

def modify_html():
    with open('docs/index.html', 'r', encoding='utf-8') as f:
        content = f.read()

    # 1. Add Developer to Header
    old_th = '<th style="text-align:left">楼盘名称</th>'
    new_th = '<th style="text-align:left">楼盘名称</th>\n            <th style="width:120px; text-align:left">开发商</th>'
    content = content.replace(old_th, new_th)

    # 2. Add Developer to Row
    old_td = "+ '<td class=\"town\">' + (d.project || '—') + spark + '</td>'"
    new_td = "+ '<td class=\"town\">' + (d.project || '—') + spark + '</td>'\n              + '<td style=\"color:var(--muted);font-size:12px\">' + (d.developer || '—') + '</td>'"
    content = content.replace(old_td, new_td)

    with open('docs/index.html', 'w', encoding='utf-8') as f:
        f.write(content)
    print("Fixed index.html headers")

if __name__ == '__main__':
    modify_html()
