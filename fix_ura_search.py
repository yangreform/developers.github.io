import re

with open('docs/index.html', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix URA table
if 'function renderURATable' in content:
    content = re.sub(
        r'(html \+= `<tr )(style="border-bottom:1px solid rgba\(255,255,255,0\.05\);)',
        r'\1data-row="1" \2',
        content
    )

with open('docs/index.html', 'w', encoding='utf-8') as f:
    f.write(content)
print("Checked URA table data-row")
