import sqlite3
import requests
import time
import os
import datetime

# 1. 設定與資料庫路徑
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "landlord_sg.db")
URA_ACCESS_KEY = "78bab9ae-c762-4c3d-8707-628138727c83" # URA 金鑰

# 🌟 初始化或清理資料庫表格
def init_tables():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    print("🧹 正在準備資料庫表格...")
    
    # 清空舊資料
    cursor.execute('DROP TABLE IF EXISTS ura_transactions')
    cursor.execute('DROP TABLE IF EXISTS ura_rentals')
    cursor.execute('DROP TABLE IF EXISTS hdb')
    cursor.execute('DROP TABLE IF EXISTS hdb_rental')
    
    # URA 私宅買賣
    cursor.execute('''
        CREATE TABLE ura_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT, street TEXT, contractDate TEXT, flat_type TEXT, 
            floorRange TEXT, price REAL, area REAL
        )
    ''')
    
    # URA 私宅租金
    cursor.execute('''
        CREATE TABLE ura_rentals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT, street TEXT, leaseDate TEXT, flat_type TEXT, 
            rent REAL, area TEXT
        )
    ''')
    
    # HDB 組屋買賣
    cursor.execute('''
        CREATE TABLE hdb (
            month TEXT, town TEXT, flat_type TEXT, block TEXT, 
            street_name TEXT, storey_range TEXT, floor_area_sqm TEXT, 
            flat_model TEXT, lease_commence_date TEXT, remaining_lease TEXT, resale_price TEXT
        )
    ''')
    
    # HDB 組屋租金
    cursor.execute('''
        CREATE TABLE hdb_rental (
            rent_approval_date TEXT, town TEXT, block TEXT, 
            street_name TEXT, flat_type TEXT, monthly_rent TEXT
        )
    ''')
    
    conn.commit()
    conn.close()

# ==========================================
# 🏠 HDB 自動同步 (Data.gov.sg)
# ==========================================
def fetch_hdb_data(resource_id, table_name, columns):
    print(f"\n📥 [Data.gov.sg] 開始下載 {table_name} 最新資料...")
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    offset = 0
    limit = 5000 # 每次抓 5000 筆，避免 HTTP 413
    total_inserted = 0
    
    while True:
        url = f"https://data.gov.sg/api/action/datastore_search?resource_id={resource_id}&limit={limit}&offset={offset}"
        res = requests.get(url)
        if not res.ok:
            print(f"   ❌ 下載失敗: HTTP {res.status_code}")
            break
            
        data = res.json().get('result', {})
        records = data.get('records', [])
        if not records:
            break
            
        values = []
        for r in records:
            values.append(tuple(r.get(c, '') for c in columns))
            
        placeholders = ','.join(['?' for _ in columns])
        cursor.executemany(f"INSERT INTO {table_name} ({','.join(columns)}) VALUES ({placeholders})", values)
        
        total_inserted += len(records)
        print(f"   ...已下載 {total_inserted} 筆")
        offset += limit
        
    conn.commit()
    conn.close()
    print(f"✅ {table_name} 資料下載完成！")

# ==========================================
# 🏙️ URA 私宅自動同步 (URA API)
# ==========================================
def estimate_sales_type(area_sqm, prop_type):
    if 'Semi-Detached' in prop_type or 'Detached' in prop_type or 'Terrace' in prop_type:
        return "別墅"
    try:
        area_str = str(area_sqm).replace('<=', '').replace('>=', '').replace('<', '').replace('>', '').replace(' ', '')
        if '-' in area_str:
            parts = area_str.split('-')
            area = (float(parts[0]) + float(parts[1])) / 2
        elif area_str in ['NA', 'NIL', '']:
            raise ValueError("面積為空")
        else:
            area = float(area_str)
            
        if area <= 50: return "Studio"
        if area <= 70: return "1-Bedroom"
        if area <= 90: return "2-Bedroom"
        if area <= 120: return "3-Bedroom"
        return "4-Bedroom+"
    except:
        if prop_type and prop_type not in ['NIL', 'NA', '']:
            return prop_type
        return "私宅"

def get_ura_token():
    print("\n🔑 正在向新加坡政府 (URA) 申請通行證 Token...")
    headers = {"User-Agent": "Mozilla/5.0", "AccessKey": URA_ACCESS_KEY}
    res = requests.get("https://eservice.ura.gov.sg/uraDataService/insertNewToken/v1", headers=headers)
    if res.text.startswith('<'):
        raise Exception("❌ 被 URA 防火牆擋住了，請稍候重試！")
    return res.json()['Result']

def fetch_ura_data():
    token = get_ura_token()
    headers = {"User-Agent": "Mozilla/5.0", "AccessKey": URA_ACCESS_KEY, "Token": token}
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # 1. 私宅買賣
    print("\n🚀 [URA] 開始下載 私宅買賣行情...")
    total_sales = 0
    for batch in range(1, 5):
        print(f"   ⏳ 正在下載 Batch {batch}/4...")
        url = f"https://eservice.ura.gov.sg/uraDataService/invokeUraDS/v1?service=PMI_Resi_Transaction&batch={batch}"
        res = requests.get(url, headers=headers)
        if res.text.startswith('<'): continue
            
        data = res.json().get('Result', [])
        sale_records = []
        for project in data:
            p_name = project.get('project', '')
            s_name = project.get('street', '')
            for t in project.get('transaction', []):
                price = float(t.get('price', 0))
                c_date = t.get('contractDate', '')
                area_sqm = t.get('area', '0')
                p_type = t.get('propertyType', '')
                
                calculated_type = estimate_sales_type(area_sqm, p_type)
                f_range = '未知樓層' if t.get('floorRange') in ['NIL', None] else t.get('floorRange', '未知樓層')
                
                try: area_val = float(area_sqm)
                except ValueError: area_val = 0.0

                sale_records.append((p_name, s_name, c_date, calculated_type, f_range, price, area_val))
                
        cursor.executemany("INSERT INTO ura_transactions (project, street, contractDate, flat_type, floorRange, price, area) VALUES (?, ?, ?, ?, ?, ?, ?)", sale_records)
        total_sales += len(sale_records)
        time.sleep(1)
    print(f"✅ URA 買賣資料下載完成！共存入 {total_sales} 筆。")

    # 2. 私宅出租
    print("\n🚀 [URA] 開始下載 私宅出租行情 (近3年)...")
    quarters = []
    y = datetime.date.today().year
    q = (datetime.date.today().month - 1) // 3 + 1
    for _ in range(12):
        quarters.append(f"{str(y)[-2:]}q{q}")
        q -= 1
        if q == 0:
            q = 4
            y -= 1

    total_rents = 0
    for ref in quarters:
        print(f"   ⏳ 正在下載季別 {ref}...")
        url = f"https://eservice.ura.gov.sg/uraDataService/invokeUraDS/v1?service=PMI_Resi_Rental&refPeriod={ref}"
        res = requests.get(url, headers=headers)
        if res.text.startswith('<'): continue
            
        try:
            data = res.json().get('Result', [])
            rent_records = []
            for project in data:
                p_name = project.get('project', '')
                s_name = project.get('street', '')
                for r in project.get('rental', []):
                    rent = float(r.get('rent', 0))
                    l_date = r.get('leaseDate', '')
                    p_type = r.get('propertyType', '')
                    bedrooms = str(r.get('noOfBedrooms', '')).strip().upper()
                    area = str(r.get('areaSqm', '0')).strip()
                    
                    if bedrooms and bedrooms not in ['NIL', 'NA']: flat_type = f"{bedrooms}-Bedroom"
                    else: flat_type = estimate_sales_type(area, p_type)

                    rent_records.append((p_name, s_name, l_date, flat_type, rent, area))
                    
            cursor.executemany("INSERT INTO ura_rentals (project, street, leaseDate, flat_type, rent, area) VALUES (?, ?, ?, ?, ?, ?)", rent_records)
            total_rents += len(rent_records)
        except Exception as e:
            print(f"   ⚠️ 解析季別 {ref} 失敗: {e}")
            
    print(f"✅ URA 出租資料下載完成！共存入 {total_rents} 筆。")

    # 3. 開發商名稱 (Developer Sales)
    print("\n🚀 [URA] 開始下載 開發商資料 (近3年)...")
    months = []
    dy = datetime.date.today().year
    dm = datetime.date.today().month
    for _ in range(36):
        months.append(f"{dm:02d}{str(dy)[-2:]}")
        dm -= 1
        if dm == 0:
            dm = 12
            dy -= 1

    cursor.execute("CREATE TABLE IF NOT EXISTS ura_developers (project TEXT PRIMARY KEY, developer_name TEXT)")
    
    total_devs = 0
    for ref in months:
        print(f"   ⏳ 正在下載開發商資料 (月份 {ref})...")
        url = f"https://eservice.ura.gov.sg/uraDataService/invokeUraDS/v1?service=PMI_Resi_Developer_Sales&refPeriod={ref}"
        res = requests.get(url, headers=headers)
        if res.text.startswith('<'): continue
        
        try:
            data = res.json().get('Result', [])
            dev_records = []
            for project in data:
                p_name = project.get('project', '')
                d_name = project.get('developerName', '')
                if p_name and d_name:
                    dev_records.append((p_name, d_name))
            
            cursor.executemany("INSERT OR REPLACE INTO ura_developers (project, developer_name) VALUES (?, ?)", dev_records)
            total_devs += len(dev_records)
        except Exception as e:
            pass
            
    print(f"✅ 開發商資料更新完成！")

    conn.commit()
    conn.close()
    print(f"✅ URA 出租資料下載完成！共存入 {total_rents} 筆。")


if __name__ == "__main__":
    print("========================================")
    print(" 🌟 LandlordSG 全自動資料庫同步程式 🌟")
    print("========================================")
    
    init_tables()
    
    # 1. HDB Resale (API ID: d_8b84c4ee58e3cfc0ece0d773c8ca6abc)
    hdb_resale_cols = ['month', 'town', 'flat_type', 'block', 'street_name', 'storey_range', 'floor_area_sqm', 'flat_model', 'lease_commence_date', 'remaining_lease', 'resale_price']
    fetch_hdb_data('d_8b84c4ee58e3cfc0ece0d773c8ca6abc', 'hdb', hdb_resale_cols)
    
    # 2. HDB Rental (API ID: d_c9f57187485a850908655db0e8cfe651)
    hdb_rental_cols = ['rent_approval_date', 'town', 'block', 'street_name', 'flat_type', 'monthly_rent']
    fetch_hdb_data('d_c9f57187485a850908655db0e8cfe651', 'hdb_rental', hdb_rental_cols)
    
    # 3. URA Private Property
    fetch_ura_data()
    
    print("\n🎉 大功告成！四大房市資料皆已完美封裝進您的 SQLite 武器庫中！")
