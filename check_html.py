import re
with open('docs/index.html', 'r', encoding='utf-8') as f:
    html = f.read()

tabs = re.findall(r'<button class="tab-btn.*?" data-tab="(.*?)">', html)
panels = re.findall(r'<div id="(.*?)" class="tab-panel', html)
print("Tabs:", tabs)
print("Panels:", panels)
