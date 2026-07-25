import sqlite3
import time
import re
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

DB_NAME = 'backend/landlord_sg.db'

def extract_developer(text):
    m = re.search(r'Developer\s+([A-Za-z0-9\s]+?(?:Pte Ltd|Ltd|Limited|Group|Corp|Developer))', text, re.I)
    if m: return m.group(1).strip()
    return None

def main():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS ura_developers (project TEXT PRIMARY KEY, developer_name TEXT)")
    
    # 找尋沒有開發商資料的建案
    cursor.execute('''
        SELECT project FROM ura_transactions 
        WHERE project NOT IN (SELECT project FROM ura_developers)
        GROUP BY project
    ''')
    missing_projects = [r[0] for r in cursor.fetchall()]
    
    if not missing_projects:
        print("🎉 所有樓盤都已經有開發商資料了！")
        return
        
    print(f"🔍 發現 {len(missing_projects)} 個樓盤缺乏開發商資料，準備啟動自動化瀏覽器查詢...")
    
    # 啟動真實的 Chrome 瀏覽器 (不使用 headless，這樣萬一遇到驗證碼您可以手動點擊)
    options = Options()
    # 避免被輕易偵測為自動化測試軟體
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })
    
    found_count = 0
    
    try:
        for i, project in enumerate(missing_projects):
            print(f"[{i+1}/{len(missing_projects)}] 查詢 {project} ... ", end='', flush=True)
            
            # 第一步：先透過 Google 搜尋取得 PropertyGuru 的準確網址 (包含 ID)
            search_url = f'https://www.google.com/search?q=site:propertyguru.com.sg/project+"{project}"'
            driver.get(search_url)
            time.sleep(2) # 等待 Google 載入
            
            pg_url = None
            try:
                links = driver.find_elements(By.XPATH, "//a[contains(@href, 'propertyguru.com.sg/project/')]")
                for link in links:
                    href = link.get_attribute('href')
                    if re.search(r'\-\d+$', href):  # 確認網址最後是數字 ID
                        pg_url = href
                        break
            except:
                pass
                
            if not pg_url:
                print("❌ Google 找不到該建案的 PropertyGuru 頁面")
                cursor.execute("INSERT INTO ura_developers (project, developer_name) VALUES (?, ?)", (project, "Unknown"))
                conn.commit()
                continue
                
            # 第二步：進入 PropertyGuru 頁面抓取開發商
            driver.get(pg_url)
            time.sleep(3) # 等待網頁與 Cloudflare 載入
            
            page_text = driver.execute_script("return document.body.innerText;")
            dev_name = extract_developer(page_text)
            
            if dev_name:
                print(f"✅ 找到: {dev_name}")
                cursor.execute("INSERT INTO ura_developers (project, developer_name) VALUES (?, ?)", (project, dev_name))
                conn.commit()
                found_count += 1
            else:
                print("❌ 頁面中未標示開發商 (設為 Unknown)")
                cursor.execute("INSERT INTO ura_developers (project, developer_name) VALUES (?, ?)", (project, "Unknown"))
                conn.commit()
                
            time.sleep(1) # 休息一下再繼續下一個
            
    except KeyboardInterrupt:
        print("\n⚠️ 查詢被手動中斷。")
    finally:
        driver.quit()
        conn.close()
        print(f"\n🎉 任務完成/中斷！本次成功找回 {found_count} 個開發商資料。")

if __name__ == '__main__':
    main()
