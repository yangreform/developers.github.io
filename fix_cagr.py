import os
import re

def fix_api():
    filepath = 'backend/landlord_api.py'
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # 1. Update CURRENT_YEAR to dynamic year
    content = content.replace("CURRENT_YEAR = 2025   # 以 2025 為「當前」基準",
                              "import datetime\n        CURRENT_YEAR = datetime.date.today().year")
    
    # 2. Update tx_count >= 2 to >= 1 in SQL query inside get_ura_price_trend()
    # Let's target the exact query
    old_query = '''            GROUP BY t.project, year
            HAVING tx_count >= 2
            ORDER BY t.project, year'''
    new_query = '''            GROUP BY t.project, year
            HAVING tx_count >= 1
            ORDER BY t.project, year'''
    content = content.replace(old_query, new_query)

    # 3. Allow n_years >= 1 (some projects might only have 1 year difference between earliest and latest if we relax things, but actually n_years < 1 is caught above anyway)
    # The current code has:
    # if n_years < 2:
    #     continue
    # Which requires at least 2 years difference. That's fine, 2021 to 2023 is 2 years. 2024 to 2025 is 1 year. If a project was only sold in 2024 and 2025, CAGR is just year-over-year growth. We should allow n_years >= 1.
    content = content.replace("if n_years < 2:\n                continue", "if n_years < 1:\n                continue")

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
        
    print("Fixed landlord_api.py successfully.")

if __name__ == '__main__':
    fix_api()
