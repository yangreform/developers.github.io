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
            
        total_inserted = 0
        conn = sqlite3.connect(DB_PATH)
        
        while True:
            # Parse headers first on current page
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
            print(f"Found {len(rows)} rows on current page. Header map: {idx_map}")
            
            cursor = conn.cursor()
            inserted_this_page = 0
            
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
                        inserted_this_page += 1
                        
                except Exception as e:
                    print(f"Error parsing row {i}: {e}")
                    
            conn.commit()
            total_inserted += inserted_this_page
            print(f"Inserted {inserted_this_page} records from this page.")
            
            # Check for next page
            try:
                # Broaden next button search
                valid_next = None
                
                # Method 1: DataTables generic _next ID
                try:
                    btn = driver.find_element(By.XPATH, "//*[contains(@id, '_next')]")
                    if 'disabled' not in btn.get_attribute('class'):
                        if btn.tag_name == 'li':
                            valid_next = btn.find_element(By.TAG_NAME, 'a')
                        else:
                            valid_next = btn
                except:
                    pass
                
                # Method 2: Contains 'next' class
                if not valid_next:
                    next_btns = driver.find_elements(By.XPATH, "//*[contains(translate(@class, 'NEXT', 'next'), 'next')]")
                    for btn in next_btns:
                        if btn.tag_name == 'li' and 'disabled' not in btn.get_attribute('class'):
                            valid_next = btn.find_element(By.TAG_NAME, 'a')
                            break
                        elif btn.tag_name == 'a':
                            try: parent = btn.find_element(By.XPATH, '..')
                            except: parent = None
                            
                            p_class = parent.get_attribute('class') if parent else ''
                            if 'disabled' not in p_class and 'disabled' not in (btn.get_attribute('class') or ''):
                                valid_next = btn
                                break
                
                # Method 3: Contains 'Next' text
                if not valid_next:
                    next_btns = driver.find_elements(By.XPATH, "//a[contains(translate(text(), 'NEXT', 'next'), 'next')]")
                    for btn in next_btns:
                        try: parent = btn.find_element(By.XPATH, '..')
                        except: parent = None
                        
                        p_class = parent.get_attribute('class') if parent else ''
                        if 'disabled' not in p_class and 'disabled' not in (btn.get_attribute('class') or ''):
                            valid_next = btn
                            break
                            
                if not valid_next:
                    # Try using JS directly if it's a datatable
                    js_next = '''
                        var tables = $.fn.dataTable.tables();
                        if (tables.length > 0) {
                            var api = $(tables[0]).DataTable();
                            var info = api.page.info();
                            if (info.page < info.pages - 1) {
                                api.page('next').draw('page');
                                return true;
                            }
                        }
                        return false;
                    '''
                    try:
                        success = driver.execute_script(js_next)
                        if success:
                            valid_next = "JS_TRIGGERED"
                    except:
                        pass
                        
                if valid_next == "JS_TRIGGERED":
                    print("Clicking 'Next' page via DataTables JS API...")
                    time.sleep(3)
                elif valid_next:
                    print("Clicking 'Next' page...")
                    driver.execute_script("arguments[0].click();", valid_next)
                    time.sleep(3)
                    
                    # Re-find the target table as DOM might have refreshed
                    tables = driver.find_elements(By.TAG_NAME, "table")
                    target_table = None
                    for tbl in tables:
                        if "Project Name" in tbl.text and "Price" in tbl.text:
                            target_table = tbl
                            break
                    if not target_table:
                        print("Could not find table on next page.")
                        break
                else:
                    print("No more pages.")
                    break
            except Exception as e:
                print(f"Pagination error or reached end: {e}")
                break
                
        conn.close()
        inserted = total_inserted
        print(f"✅ Successfully inserted {inserted} new commercial transactions into DB.")
        print("Note: Since URA limits 5 postal districts at a time, you will need to re-run this script for districts 6-10, 11-15, etc.")
        
    except Exception as e:
        print(f"Error: {e}")
        traceback.print_exc()
    finally:
        driver.quit()

if __name__ == '__main__':
    main()
