import sqlite3

conn = sqlite3.connect('backend/landlord_sg.db')
cursor = conn.cursor()

cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cursor.fetchall()
print('Tables:', [t[0] for t in tables])

cursor.execute('PRAGMA table_info(ura_commercial_transactions)')
print('ura_commercial_transactions schema:', cursor.fetchall())

cursor.execute('SELECT COUNT(DISTINCT project_name) FROM ura_commercial_transactions')
proj_count = cursor.fetchone()[0]

cursor.execute('SELECT COUNT(DISTINCT street_name) FROM ura_commercial_transactions')
street_count = cursor.fetchone()[0]

print(f'Distinct commercial projects: {proj_count}, Distinct commercial streets: {street_count}')

cursor.execute('''
    SELECT COUNT(DISTINCT t.project_name)
    FROM ura_commercial_transactions t
    LEFT JOIN ura_coordinates c ON (t.project_name = c.project OR t.street_name = c.street)
    WHERE c.lat IS NOT NULL
''')
matched_count = cursor.fetchone()[0]
print(f'Commercial projects matched coordinates currently: {matched_count} / {proj_count}')

cursor.execute('''
    SELECT DISTINCT t.project_name, t.street_name 
    FROM ura_commercial_transactions t
    LEFT JOIN ura_coordinates c ON (t.project_name = c.project OR t.street_name = c.street)
    WHERE c.lat IS NULL
''')
missing = cursor.fetchall()
print(f'Missing coordinates count: {len(missing)}')
print('Sample missing:', missing[:15])
