import sqlite3
import re

# 1. Fix DB schema
conn = sqlite3.connect('backend/landlord_sg.db')
cursor = conn.cursor()
cursor.execute('DROP TABLE IF EXISTS ura_coordinates')
cursor.execute('''
    CREATE TABLE IF NOT EXISTS ura_coordinates (
        project TEXT PRIMARY KEY,
        street TEXT,
        lat REAL,
        lng REAL
    )
''')
conn.commit()
conn.close()

# 2. Fix queries and injected schema in landlord_api.py
with open('backend/landlord_api.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix the injected CREATE TABLE
content = content.replace(
    'lon REAL,', 'lng REAL,'
).replace(
    'id INTEGER PRIMARY KEY AUTOINCREMENT,\n                project TEXT,\n                street TEXT,\n                lat REAL,\n                lng REAL,\n                UNIQUE(project)',
    'project TEXT PRIMARY KEY,\n                street TEXT,\n                lat REAL,\n                lng REAL'
)

# Remove c.postal,
content = content.replace('c.postal,', '')

with open('backend/landlord_api.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Fixed ura_coordinates schema and removed c.postal from landlord_api.py")
