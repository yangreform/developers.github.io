import sqlite3
import requests
import time
import os

# 确保路径指向您的资料库
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "landlord_sg.db")

def init_coordinates_table(cursor):
    """建立建案坐标字典表 (加入邮递区号，纯数字版)"""
    cursor.execute('DROP TABLE IF EXISTS ura_coordinates')
    cursor.execute('''
        CREATE TABLE ura_coordinates (
            project TEXT PRIMARY KEY,
            street TEXT,
            lat REAL,
            lng REAL,
            postal TEXT
        )
    ''')

def run_geocoding_plan():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    init_coordinates_table(cursor)
    
    print("⏳ 正在分析资料库中的私宅建案...")
    cursor.execute('''
        SELECT DISTINCT project, street 
        FROM ura_transactions 
        WHERE project != '' 
    ''')
    unique_projects = cursor.fetchall()
    
    total = len(unique_projects)
    print(f"🎯 锁定目标：共有 {total} 个建案需要抓取座标与邮递区号！\n")
    
    headers = {"User-Agent": "Mozilla/5.0"}
    base_url = "https://www.onemap.gov.sg/api/common/elastic/search?returnGeom=Y&getAddrDetails=Y&searchVal="
    
    success_count = 0
    
    for idx, (project, street) in enumerate(unique_projects, 1):
        search_keyword = project if project.upper() != 'NIL' else street
        url = base_url + requests.utils.quote(search_keyword)
        
        try:
            res = requests.get(url, headers=headers)
            data = res.json()
            
            if data.get('found', 0) > 0:
                lat = float(data['results'][0]['LATITUDE'])
                lng = float(data['results'][0]['LONGITUDE'])
                # 🌟 抓取邮递区号，并强制移除任何可能的 S 字母，只留数字
                raw_postal = data['results'][0].get('POSTAL', '')
                postal = str(raw_postal).strip().replace('S', '').replace('s', '')
                
                cursor.execute(
                    "INSERT INTO ura_coordinates (project, street, lat, lng, postal) VALUES (?, ?, ?, ?, ?)",
                    (project, street, lat, lng, postal)
                )
                success_count += 1
                print(f"✅ [{idx}/{total}] 成功：{project} ➡️ {postal}")
            
            else:
                fallback_url = base_url + requests.utils.quote(street)
                res_street = requests.get(fallback_url, headers=headers)
                data_street = res_street.json()
                
                if data_street.get('found', 0) > 0:
                    lat = float(data_street['results'][0]['LATITUDE'])
                    lng = float(data_street['results'][0]['LONGITUDE'])
                    raw_postal = data_street['results'][0].get('POSTAL', '')
                    postal = str(raw_postal).strip().replace('S', '').replace('s', '')
                    
                    cursor.execute(
                        "INSERT INTO ura_coordinates (project, street, lat, lng, postal) VALUES (?, ?, ?, ?, ?)",
                        (project, street, lat, lng, postal)
                    )
                    success_count += 1
                    print(f"⚠️ [{idx}/{total}] 备援成功：{street} ➡️ {postal}")
                else:
                    print(f"❌ [{idx}/{total}] OneMap 查无此地：{project} ({street})")

            if idx % 50 == 0:
                conn.commit()
                
            time.sleep(0.2)
            
        except Exception as e:
            print(f"🚨 发生错误：{project} - {str(e)}")
            time.sleep(1) 
            
    conn.commit()
    conn.close()
    
    print(f"\n🎉 补完计画大功告成！成功为 {success_count} 个建案标上座标与纯数字邮递区号！")

if __name__ == "__main__":
    run_geocoding_plan()
