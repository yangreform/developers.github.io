import re
with open('docs/index.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Remove loadHDBData function
content = re.sub(r'async function loadHDBData\(\).*?document\.getElementById\(\'hdb-tbody\'\)\.innerHTML = \'<tr\><td colspan="7" class="no-data"\>无法载入：\' \+ e\.message \+ \'</td\></tr\>\';\s*\}', '', content, flags=re.DOTALL)

# 2. Remove loadRentalData function
content = re.sub(r'async function loadRentalData\(\).*?document\.getElementById\(\'rental-tbody\'\)\.innerHTML = \'<tr\><td colspan="7" class="no-data"\>无法载入：\' \+ e\.message \+ \'</td\></tr\>\';\s*\}', '', content, flags=re.DOTALL)

# 3. Update window load event
old_load = '''window.addEventListener('load', function() {
    loadHDBData();
    loadURAData();
    loadRentalData();
  });'''
new_load = '''window.addEventListener('load', function() {
    loadDevelopers();
    loadURAData();
  });'''
content = content.replace(old_load, new_load)

# Also fix the duplicate load listener that might exist
old_load_2 = '''window.addEventListener('load', function() {
    loadHDBData();
    loadURAData();
    loadRentalData();
  });'''
content = content.replace(old_load_2, new_load)

# Just to be extremely sure, if it says loadHDBData(); anywhere, replace it
content = content.replace("loadHDBData();", "loadDevelopers();")
content = content.replace("loadRentalData();", "")

with open('docs/index.html', 'w', encoding='utf-8') as f:
    f.write(content)

print("JS cleaned up!")
