import os
import csv
import sqlite3
import traceback

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'landlord_sg.db')
CSV_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'CommercialTransactionSearch')

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
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
    ''')
    conn.commit()
    return conn

def parse_float(val):
    try:
        val = str(val).replace(',', '').replace('$', '').strip()
        if not val or val == '-' or val == 'N.A.':
            return 0.0
        return float(val)
    except:
        return 0.0

def sync_commercial_csv():
    if not os.path.exists(CSV_DIR):
        print(f"Folder not found: {CSV_DIR}")
        return

    conn = init_db()
    cursor = conn.cursor()
    
    files = [f for f in os.listdir(CSV_DIR) if f.lower().endswith('.csv')]
    if not files:
        print("No CSV files found in the directory.")
        return

    total_inserted = 0

    for file_name in files:
        file_path = os.path.join(CSV_DIR, file_name)
        print(f"Processing {file_name}...")
        
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            reader = csv.reader(f)
            headers = next(reader, None)
            if not headers:
                continue
                
            headers = [h.strip().lower() for h in headers]
            
            # map column indices
            idx_project = -1
            idx_street = -1
            idx_type = -1
            idx_price = -1
            idx_psf = -1
            idx_date = -1
            idx_area = -1
            idx_tenure = -1
            
            for i, h in enumerate(headers):
                if h == 'project name': idx_project = i
                elif h == 'street name': idx_street = i
                elif h == 'property type': idx_type = i
                elif h == 'transacted price ($)': idx_price = i
                elif h == 'unit price ($ psf)': idx_psf = i
                elif h == 'sale date': idx_date = i
                elif h == 'area (sqm)': idx_area = i
                elif h == 'tenure': idx_tenure = i

            inserted_in_file = 0
            for row in reader:
                if len(row) < len(headers):
                    continue
                    
                def get_val(idx):
                    return row[idx].strip() if idx >= 0 and idx < len(row) else ""

                project_name = get_val(idx_project)
                street_name = get_val(idx_street)
                property_type = get_val(idx_type)
                tenure = get_val(idx_tenure)
                
                if not project_name and street_name: project_name = street_name
                if not street_name and project_name: street_name = project_name

                price_sgd = parse_float(get_val(idx_price))
                psf_sgd = parse_float(get_val(idx_psf))
                area_sqm = parse_float(get_val(idx_area))
                contract_date = get_val(idx_date)
                
                try:
                    cursor.execute('''
                        INSERT OR IGNORE INTO ura_commercial_transactions 
                        (project_name, street_name, property_type, tenure, area_sqm, price_sgd, psf_sgd, contract_date)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (project_name, street_name, property_type, tenure, area_sqm, price_sgd, psf_sgd, contract_date))
                    if cursor.rowcount > 0:
                        inserted_in_file += 1
                        total_inserted += 1
                except sqlite3.Error as e:
                    pass
                    
            print(f"  Inserted {inserted_in_file} new rows from {file_name}.")
            
    conn.commit()
    conn.close()
    print(f"Successfully inserted a total of {total_inserted} new commercial transactions into DB.")

if __name__ == '__main__':
    sync_commercial_csv()
