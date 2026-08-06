import re

def fix_key():
    # Fix sync_developers_full.py
    with open('sync_developers_full.py', 'r', encoding='utf-8') as f:
        content = f.read()
    content = content.replace("project.get('developerName', '')", "project.get('developer', '')")
    with open('sync_developers_full.py', 'w', encoding='utf-8') as f:
        f.write(content)

    # Fix backend/update_db.py
    with open('backend/update_db.py', 'r', encoding='utf-8') as f:
        content = f.read()
    content = content.replace("project.get('developerName', '')", "project.get('developer', '')")
    with open('backend/update_db.py', 'w', encoding='utf-8') as f:
        f.write(content)

    print("Fixed developer JSON key in both scripts")

if __name__ == '__main__':
    fix_key()
