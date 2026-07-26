import os
import sys
import time
import sqlite3
import traceback

try:
    import undetected_chromedriver as uc
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import Select
except ImportError:
    print("Please install required packages: pip install undetected-chromedriver selenium")
    sys.exit(1)

import os
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'landlord_sg.db')

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
    conn.close()

def main():
    print("Initializing URA Commercial Scraper (2020-Present)...")
    init_db()

    options = uc.ChromeOptions()
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--window-size=1280,1024')
    
    print("Starting browser...")
    driver = uc.Chrome(options=options)
    
    try:
        url = "https://eservice.ura.gov.sg/property-market-information/pmiCommercialTransactionSearch"
        print(f"Connecting to: {url}")
        driver.get(url)
        
        print("Waiting for page load... (Please solve CAPTCHA/Cloudflare if it appears)")
        time.sleep(5)
        
        input("==> Please MANUALLY select 'Postal Districts 1 to 5' in the Location modal.\n==> Also select Property Type 'Office' and 'Retail', and Date from 'Jan 2020'.\n==> Click 'Search'.\n==> Once the RESULTS TABLE is fully loaded on the screen, press ENTER here to continue and scrape the data...")
        
        print("Extracting data from the results table...")
        
        # In URA, the table usually has id 'searchResults' or similar class 'table'
        tables = driver.find_elements(By.TAG_NAME, "table")
        if not tables:
            print("No tables found on the page!")
            return
            
        # We will parse all tables to find the right one (the one with 'Project Name' or 'S/N')
        target_table = None
        for tbl in tables:
            text = tbl.text
            if "Project Name" in text and "Price" in text:
                target_table = tbl
                break
                
        if not target_table:
            print("Could not identify the correct results table.")
            # save source for debug
            with open("commercial_error.html", "w", encoding="utf-8") as f:
                f.write(driver.page_source)
            return
            
        rows = target_table.find_elements(By.TAG_NAME, "tr")
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
                print(f"Error parsing row {i}: {e}")
                
        conn.commit()
        conn.close()
        
        print(f"✅ Successfully inserted {inserted} new commercial transactions into DB.")
        print("Note: Since URA limits 5 postal districts at a time, you will need to re-run this script for districts 6-10, 11-15, etc.")
        
    except Exception as e:
        print(f"Error: {e}")
        traceback.print_exc()
    finally:
        driver.quit()

if __name__ == '__main__':
    main()
