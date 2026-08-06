import re

with open('docs/index.html', 'r', encoding='utf-8') as f:
    content = f.read()

# Replace <tr> in renderDevTable
old_tr = """      html += '<tr style="cursor:pointer; transition: background 0.2s;" onclick="showDevProjects(\\'\' + d.developer.replace(/'/g, "\\\\'") + '\\\')" onmouseover="this.style.background=\\'var(--hover)\\'" onmouseout="this.style.background=\\'\\'">';"""
new_tr = """      html += '<tr data-row="1" style="cursor:pointer; transition: background 0.2s;" onclick="showDevProjects(\\'\' + d.developer.replace(/'/g, "\\\\'") + '\\\')" onmouseover="this.style.background=\\'var(--hover)\\'" onmouseout="this.style.background=\\'\\'">';"""

if 'data-row="1"' not in old_tr and old_tr in content:
    content = content.replace(old_tr, new_tr)
    with open('docs/index.html', 'w', encoding='utf-8') as f:
        f.write(content)
    print("Fixed developer search data-row")
else:
    # try regex
    content = re.sub(
        r'(html \+= \'<tr )(style="cursor:pointer; transition: background 0\.2s;" onclick="showDevProjects)',
        r'\1data-row="1" \2',
        content
    )
    with open('docs/index.html', 'w', encoding='utf-8') as f:
        f.write(content)
    print("Fixed developer search data-row using regex")
