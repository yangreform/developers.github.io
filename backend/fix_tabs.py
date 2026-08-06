import re

with open('docs/index.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Swap tab buttons
old_tabs = '''    <div class="tabs">
      <button class="tab-btn active" data-tab="ura-tab">&#127961;&#65039; 🏙️ 私宅：依楼盘</button>
      <button class="tab-btn" data-tab="dev-tab">&#127968; 🏠 私宅：依开发商</button>
      <button class="tab-btn" data-tab="commercial-tab">&#127980; 🏢 商办 / 店面</button>
    </div>'''

new_tabs = '''    <div class="tabs">
      <button class="tab-btn active" data-tab="dev-tab">&#127968; 🏠 私宅：依开发商</button>
      <button class="tab-btn" data-tab="ura-tab">&#127961;&#65039; 🏙️ 私宅：依楼盘</button>
      <button class="tab-btn" data-tab="commercial-tab">&#127980; 🏢 商办 / 店面</button>
    </div>'''

content = content.replace(old_tabs, new_tabs)

# 2. Swap active panel class
content = content.replace('<div id="ura-tab" class="tab-panel active">', '<div id="ura-tab" class="tab-panel">')
content = content.replace('<div id="dev-tab" class="tab-panel">', '<div id="dev-tab" class="tab-panel active">')

with open('docs/index.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('Swapped tabs order and set dev-tab as default.')
