import sqlite3
import requests
import time
import os

# 確保路徑指向您的資料庫
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "landlord_sg.db")

def init_coordinates_table(cursor):
    """建立建案座標字典表"""
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ura_coordinates (
            project TEXT PRIMARY KEY,
            street TEXT,
            lat REAL,
            lng REAL
        )
    ''')

def run_geocoding_plan():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    init_coordinates_table(cursor)
    
    # 🌟 核心邏輯：從交易紀錄中，找出「還沒被轉換過座標」的獨立建案
    print("⏳ 正在分析資料庫中的私宅建案...")
    cursor.execute('''
        SELECT DISTINCT project, street 
        FROM ura_transactions 
        WHERE project NOT IN (SELECT project FROM ura_coordinates)
        AND project != '' 
    ''')
    unique_projects = cursor.fetchall()
    
    total = len(unique_projects)
    print(f"🎯 鎖定目標：共有 {total} 個建案需要轉換經緯度！\n")
    
    headers = {"User-Agent": "Mozilla/5.0"}
    base_url = "https://www.onemap.gov.sg/api/common/elastic/search?returnGeom=Y&getAddrDetails=Y&searchVal="
    
    success_count = 0
    
    for idx, (project, street) in enumerate(unique_projects, 1):
        # 1. 優先用建案名稱搜尋 (如果建案名稱是 NIL，就用街道名稱)
        search_keyword = project if project.upper() != 'NIL' else street
        url = base_url + requests.utils.quote(search_keyword)
        
        try:
            res = requests.get(url, headers=headers)
            data = res.json()
            
            # 2. 如果 OneMap 找得到這個建案
            if data.get('found', 0) > 0:
                lat = float(data['results'][0]['LATITUDE'])
                lng = float(data['results'][0]['LONGITUDE'])
                
                cursor.execute(
                    "INSERT INTO ura_coordinates (project, street, lat, lng) VALUES (?, ?, ?, ?)",
                    (project, street, lat, lng)
                )
                success_count += 1
                print(f"✅ [{idx}/{total}] 成功：{project} ➡️ ({lat}, {lng})")
            
            else:
                # 3. 備援機制：如果建案名找不到，改搜街道名稱
                fallback_url = base_url + requests.utils.quote(street)
                res_street = requests.get(fallback_url, headers=headers)
                data_street = res_street.json()
                
                if data_street.get('found', 0) > 0:
                    lat = float(data_street['results'][0]['LATITUDE'])
                    lng = float(data_street['results'][0]['LONGITUDE'])
                    
                    cursor.execute(
                        "INSERT INTO ura_coordinates (project, street, lat, lng) VALUES (?, ?, ?, ?)",
                        (project, street, lat, lng)
                    )
                    success_count += 1
                    print(f"⚠️ [{idx}/{total}] 備援成功 (搜街道)：{street} ➡️ ({lat}, {lng})")
                else:
                    print(f"❌ [{idx}/{total}] OneMap 查無此地：{project} ({street})")

            # 每處理 50 筆就存檔一次，避免程式中斷心血全毀
            if idx % 50 == 0:
                conn.commit()
                
            # 🌟 防封鎖機制：每次呼叫暫停 0.2 秒，避免被 OneMap 當成惡意攻擊
            time.sleep(0.2)
            
        except Exception as e:
            print(f"🚨 發生錯誤：{project} - {str(e)}")
            time.sleep(1) # 出錯時稍微休息一下
            
    # 最後確保所有進度都存入資料庫
    conn.commit()
    conn.close()
    
    print(f"\n🎉 補完計畫大功告成！成功為 {success_count} 個建案標上精確座標！")

if __name__ == "__main__":
    run_geocoding_plan()
