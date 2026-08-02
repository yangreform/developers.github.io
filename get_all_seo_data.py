import sqlite3
import json

conn = sqlite3.connect('backend/landlord_sg.db')
c = conn.cursor()

# 1. Distinct developers
c.execute("SELECT DISTINCT developer_name FROM ura_developers WHERE developer_name IS NOT NULL AND developer_name != ''")
dev_rows = c.fetchall()
developers = sorted(list(set(
    r[0].strip() for r in dev_rows 
    if r[0] and r[0].strip() and r[0].strip() not in ('N/A', '—') and not r[0].strip().startswith('Unknown')
)))

# 2. Distinct URA projects
c.execute("SELECT DISTINCT project FROM ura_transactions WHERE project IS NOT NULL AND project != ''")
ura_projects = sorted(list(set(r[0].strip() for r in c.fetchall() if r[0] and r[0].strip())))

# 3. Distinct Commercial buildings / projects
c.execute("SELECT DISTINCT project_name, street_name FROM ura_commercial_transactions WHERE project_name IS NOT NULL AND project_name != ''")
com_projects = sorted(list(set(r[0].strip() for r in c.fetchall() if r[0] and r[0].strip())))

print(f"Total Developers: {len(developers)}")
print(f"Total URA Projects: {len(ura_projects)}")
print(f"Total Commercial Projects: {len(com_projects)}")

with open('backend/seo_keywords.json', 'w', encoding='utf-8') as f:
    json.dump({
        'developers': developers,
        'ura_projects': ura_projects,
        'commercial_projects': com_projects
    }, f, ensure_ascii=False, indent=2)

print("Saved to backend/seo_keywords.json")
