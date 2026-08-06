import os

with open('backend/landlord_api.py', 'r', encoding='utf-8') as f:
    content = f.read()

create_table_sql = """        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ura_coordinates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project TEXT,
                street TEXT,
                lat REAL,
                lon REAL,
                UNIQUE(project)
            )
        ''')"""

if 'CREATE TABLE IF NOT EXISTS ura_coordinates' not in content:
    # insert it right after the commercial CREATE TABLE
    target = 'UNIQUE(project_name, property_type, area_sqm, price_sgd, contract_date)\n            )\n        """)'
    if target in content:
        content = content.replace(target, target + '\n\n' + create_table_sql)
        with open('backend/landlord_api.py', 'w', encoding='utf-8') as f:
            f.write(content)
        print("Injected ura_coordinates CREATE TABLE")
    else:
        print("Target for injection not found in landlord_api.py")
else:
    print("ura_coordinates CREATE TABLE already exists")
