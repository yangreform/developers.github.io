import os

with open('backend/fill_missing_developers_selenium.py', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace("DB_PATH = 'landlord_sg.db'", "import os\\nDB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'landlord_sg.db')")
content = content.replace('DB_PATH = "landlord_sg.db"', "import os\\nDB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'landlord_sg.db')")

with open('backend/fill_missing_developers_selenium.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("Fixed dev script db path")
