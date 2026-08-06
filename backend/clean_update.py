import re

with open('backend/update_db.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
skip = False
for line in lines:
    if 'def update_hdb_transactions' in line:
        skip = True
    elif 'def update_ura_private' in line:
        skip = False
        
    if not skip:
        if 'update_hdb_transactions()' in line:
            continue
        if 'Starting HDB' in line or 'HDB transaction update complete' in line:
            continue
        new_lines.append(line)

with open('backend/update_db.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
print("Removed HDB from update_db.py")
