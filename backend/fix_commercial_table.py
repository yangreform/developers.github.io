with open('backend/landlord_api.py', 'r', encoding='utf-8') as f:
    content = f.read()

create_table_sql = '''        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ura_commercial_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_name TEXT,
                street_name TEXT,
                property_type TEXT,
                tenure TEXT,
                area_sqm REAL,
                price_sgd REAL,
                psf_sgd REAL,
                contract_date TEXT,
                UNIQUE(project_name, property_type, area_sqm, price_sgd, contract_date)
            )
        """)'''

if 'CREATE TABLE IF NOT EXISTS ura_commercial_transactions' not in content:
    content = content.replace(
        'cursor = conn.cursor()\n        \n        # We fetch all columns',
        'cursor = conn.cursor()\n' + create_table_sql + '\n        \n        # We fetch all columns'
    )
    with open('backend/landlord_api.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print('Injected CREATE TABLE')
else:
    print('CREATE TABLE already exists')
