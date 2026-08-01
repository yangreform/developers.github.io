import os
import sqlite3

# 1. Update the scraper logic in sync_commercial_selenium.py
with open('backend/sync_commercial_selenium.py', 'r', encoding='utf-8') as f:
    content = f.read()

old_logic = """        rows = target_table.find_elements(By.TAG_NAME, "tr")
        print(f"Found {len(rows)} rows in the table.")
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        inserted = 0
        for i, row in enumerate(rows):
            cols = row.find_elements(By.TAG_NAME, "td")
            if len(cols) < 7:
                continue # Header or empty
                
            try:
                # Based on typical URA Commercial columns:
                # S/N | Project Name | Street Name | Type | Tenure | Area (sqm) | Price ($) | Nett Price | Price ($psf) | Date
                # This might vary, so we just grab text. We'll dump it to DB and clean it later.
                project_name = cols[1].text.strip()
                street_name = cols[2].text.strip()
                property_type = cols[3].text.strip()
                tenure = cols[4].text.strip()
                area_sqm_str = cols[5].text.strip().replace(',', '')
                price_str = cols[6].text.strip().replace(',', '').replace('$', '')
                psf_str = cols[8].text.strip().replace(',', '').replace('$', '')
                contract_date = cols[9].text.strip()
                
                try:
                    area_sqm = float(area_sqm_str) if area_sqm_str else 0.0
                except: area_sqm = 0.0
                
                try:
                    price_sgd = float(price_str) if price_str else 0.0
                except: price_sgd = 0.0
                
                try:
                    psf_sgd = float(psf_str) if psf_str else 0.0
                except: psf_sgd = 0.0
                
                cursor.execute('''
                    INSERT OR IGNORE INTO ura_commercial_transactions 
                    (project_name, street_name, property_type, tenure, area_sqm, price_sgd, psf_sgd, contract_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (project_name, street_name, property_type, tenure, area_sqm, price_sgd, psf_sgd, contract_date))
                
                if cursor.rowcount > 0:
                    inserted += 1
                    
            except Exception as e:
                print(f"Error parsing row {i}: {e}")"""

new_logic = """        # Parse headers first
        ths = target_table.find_elements(By.TAG_NAME, "th")
        headers = [th.text.strip().lower() for th in ths]
        
        idx_map = {}
        for i, h in enumerate(headers):
            if "project name" in h or "building name" in h: idx_map['project'] = i
            elif "street name" in h: idx_map['street'] = i
            elif "property type" in h: idx_map['type'] = i
            elif "tenure" in h: idx_map['tenure'] = i
            elif "area (sqm)" in h: idx_map['area'] = i
            elif "price ($)" in h and "psm" not in h and "psf" not in h and "nett" not in h: idx_map['price'] = i
            elif "price ($ psf)" in h: idx_map['psf'] = i
            elif "contract date" in h: idx_map['date'] = i

        rows = target_table.find_elements(By.TAG_NAME, "tr")
        print(f"Found {len(rows)} rows. Header map: {idx_map}")
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        inserted = 0
        for i, row in enumerate(rows):
            cols = row.find_elements(By.TAG_NAME, "td")
            if len(cols) < max(list(idx_map.values()) + [0]):
                continue # Header or empty
                
            try:
                def get_col(key):
                    if key in idx_map and idx_map[key] < len(cols):
                        return cols[idx_map[key]].text.strip()
                    return ""

                project_name = get_col('project')
                street_name = get_col('street')
                property_type = get_col('type')
                tenure = get_col('tenure')
                area_sqm_str = get_col('area').replace(',', '')
                price_str = get_col('price').replace(',', '').replace('$', '')
                psf_str = get_col('psf').replace(',', '').replace('$', '')
                contract_date = get_col('date')
                
                # Fallbacks if columns are missing
                if not project_name and street_name: project_name = street_name
                if not street_name and project_name: street_name = project_name
                
                try: area_sqm = float(area_sqm_str) if area_sqm_str else 0.0
                except: area_sqm = 0.0
                
                try: price_sgd = float(price_str) if price_str else 0.0
                except: price_sgd = 0.0
                
                try: psf_sgd = float(psf_str) if psf_str else 0.0
                except: psf_sgd = 0.0
                
                cursor.execute('''
                    INSERT OR IGNORE INTO ura_commercial_transactions 
                    (project_name, street_name, property_type, tenure, area_sqm, price_sgd, psf_sgd, contract_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (project_name, street_name, property_type, tenure, area_sqm, price_sgd, psf_sgd, contract_date))
                
                if cursor.rowcount > 0:
                    inserted += 1
                    
            except Exception as e:
                print(f"Error parsing row {i}: {e}")"""

if 'def get_col(key):' not in content:
    content = content.replace(old_logic, new_logic)
    with open('backend/sync_commercial_selenium.py', 'w', encoding='utf-8') as f:
        f.write(content)

# 2. Drop the corrupted table so it can be rebuilt cleanly
db_path = 'backend/landlord_sg.db'
if os.path.exists(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM ura_commercial_transactions')
    conn.commit()
    conn.close()
    print("Cleared corrupted data from ura_commercial_transactions.")

print("Updated scraper logic to parse headers dynamically.")
