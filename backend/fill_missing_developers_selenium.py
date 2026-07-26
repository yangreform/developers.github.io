import sqlite3
import time
import re
import sys
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By

DB_NAME = 'backend/landlord_sg.db'

def extract_developer(text):
    m = re.search(r'Developer\s+([A-Za-z0-9\s]+?(?:Pte Ltd|Ltd|Limited|Group|Corp|Developer))', text, re.I)
    if m: return m.group(1).strip()
    return None

def main():
    print("==============================================")
    print("請選擇執行模式：")
    print("1: 跳過所有失敗紀錄，只查詢全新未查過的樓盤 (預設)")
    print("2: 重試【Google 找不到】的樓盤 (包含以前的 Unknown)")
    print("3: 重試【頁面未標示開發商】的樓盤")
    print("==============================================")
    
    '''
    choice = input("請輸入選項 (1/2/3) [預設 1]: ").strip()
    if choice not in ['1', '2', '3']:
        choice = '1'
    '''
    choice = '1'
        
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS ura_developers (project TEXT PRIMARY KEY, developer_name TEXT)")
    
    if choice == '1':
        # 1. 跳過所有已經在 DB 裡的 (不管成功還是失敗)
        cursor.execute('''
            SELECT project FROM ura_transactions 
            WHERE project NOT IN (SELECT project FROM ura_developers)
            GROUP BY project
        ''')
    elif choice == '2':
        # 2. 重試 Google 找不到的 (加上之前舊版存的 Unknown)
        cursor.execute('''
            SELECT project FROM ura_developers
            WHERE developer_name = 'Unknown_Google' OR developer_name = 'Unknown'
        ''')
    elif choice == '3':
        # 3. 重試 頁面未標示 的
        cursor.execute('''
            SELECT project FROM ura_developers
            WHERE developer_name = 'Unknown_Page'
        ''')
        
    missing_projects = [r[0] for r in cursor.fetchall()]
    
    if not missing_projects:
        print("🎉 目前沒有符合條件的樓盤需要查詢！")
        return
        
    print(f"🔍 發現 {len(missing_projects)} 個樓盤，準備啟動自動化瀏覽器查詢...")
    
    options = uc.ChromeOptions()
    driver = uc.Chrome(options=options, version_main=150)
    
    found_count = 0
    consecutive_google_fails = 0  # 紀錄連續 Google 失敗次數
    
    try:
        for i, project in enumerate(missing_projects):
            print(f"[{i+1}/{len(missing_projects)}] 查詢 {project} ... ", end='', flush=True)
            
            search_url = f'https://www.google.com/search?q=site:propertyguru.com.sg/project+"{project}"'
            driver.get(search_url)
            time.sleep(40) 
            
            pg_url = None
            try:
                links = driver.find_elements(By.XPATH, "//a[contains(@href, 'propertyguru.com.sg/project/')]")
                for link in links:
                    href = link.get_attribute('href')
                    if re.search(r'\-\d+$', href):  
                        pg_url = href
                        break
            except:
                pass
                
            if not pg_url:
                print("❌ Google 找不到該建案的 PropertyGuru 頁面")
                cursor.execute("INSERT OR REPLACE INTO ura_developers (project, developer_name) VALUES (?, ?)", (project, "Unknown_Google"))
                conn.commit()
                consecutive_google_fails += 1
                
                # 如果連續 10 次 Google 找不到，很可能是遇到驗證碼或被擋了，直接關閉程式
                if consecutive_google_fails >= 3:
                    print("\n⚠️ 偵測到連續 10 次 Google 找不到頁面，可能遇到驗證碼或阻擋，程式自動關閉！")
                    break
                continue
            else:
                consecutive_google_fails = 0 # 只要有成功找到網址，就重置失敗計數
                
            driver.get(pg_url)
            time.sleep(10)
            
            page_text = driver.execute_script("return document.body.innerText;")
            dev_name = extract_developer(page_text)
            
            if dev_name:
                print(f"✅ 找到: {dev_name}")
                cursor.execute("INSERT OR REPLACE INTO ura_developers (project, developer_name) VALUES (?, ?)", (project, dev_name))
                conn.commit()
                found_count += 1
            else:
                print("❌ 頁面中未標示開發商")
                cursor.execute("INSERT OR REPLACE INTO ura_developers (project, developer_name) VALUES (?, ?)", (project, "Unknown_Page"))
                conn.commit()
                
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("\n⚠️ 查詢被手動中斷。")
    finally:
        driver.quit()
        conn.close()
        print(f"\n🎉 任務完成/中斷！本次成功找回 {found_count} 個開發商資料。")

if __name__ == '__main__':
    main()
