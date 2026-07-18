import sqlite3
import requests
import json
import time
import os
import datetime

# 1. 設定與資料庫路徑
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "landlord_sg.db")
ACCESS_KEY = "78bab9ae-c762-4c3d-8707-628138727c83" # 您的 URA 金鑰

# 🌟 升級版：能解析 "80-90" 等範圍字串的推估大腦
def estimate_sales_type(area_sqm, prop_type):
    if 'Semi-Detached' in prop_type or 'Detached' in prop_type or 'Terrace' in prop_type:
        return "別墅"
    
    try:
        # 清理 URA 租金常見的奇怪符號 (例如 <=50, >200, 80-90)
        area_str = str(area_sqm).replace('<=', '').replace('>=', '').replace('<', '').replace('>', '').replace(' ', '')
        
        if '-' in area_str:
            parts = area_str.split('-')
            # 如果是 80-90，我們取中間值 85 來算
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
        # 如果真的推估失敗，至少顯示 Condominium (公寓) 等原生物業類型，不要只寫私宅
        if prop_type and prop_type not in ['NIL', 'NA', '']:
            return prop_type
        return "私宅"


# 初始化資料庫表格 (每次執行前先清空舊資料)
def init_ura_tables():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    print("🧹 正在清理舊有的 URA 私宅資料表...")
    cursor.execute('DROP TABLE IF EXISTS ura_transactions')
    cursor.execute('DROP TABLE IF EXISTS ura_rentals')
    
    # 🌟 升級：建立私宅買賣表 (加入 area 欄位)
    cursor.execute('''
        CREATE TABLE ura_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT,
            street TEXT,
            contractDate TEXT,
            flat_type TEXT,
            floorRange TEXT,
            price REAL,
            area REAL
        )
    ''')
    
    # 🌟 升級：建立私宅租金表 (加入 area 欄位)
    cursor.execute('''
        CREATE TABLE ura_rentals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT,
            street TEXT,
            leaseDate TEXT,
            flat_type TEXT,
            rent REAL,
            area TEXT
        )
    ''')
    conn.commit()
    conn.close()

# 取得 URA 每日 Token
def get_ura_token():
    print("🔑 正在向新加坡政府 (URA) 申請通行證 Token...")
    headers = {"User-Agent": "Mozilla/5.0", "AccessKey": ACCESS_KEY}
    res = requests.get("https://eservice.ura.gov.sg/uraDataService/insertNewToken/v1", headers=headers)
    
    if res.text.startswith('<'):
        raise Exception("❌ 糟糕，被 URA 防火牆擋住了，請稍等幾分鐘後再試！")
        
    data = res.json()
    return data['Result']

# 主程式：大洗劫開始
def main():
    init_ura_tables()
    token = get_ura_token()
    headers = {"User-Agent": "Mozilla/5.0", "AccessKey": ACCESS_KEY, "Token": token}
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # ==========================================
    # 📦 階段一：洗劫私宅買賣資料 (分 4 個 Batch)
    # ==========================================
    print("\n🚀 [階段一] 開始下載 URA 私宅買賣行情...")
    total_sales = 0
    for batch in range(1, 5):
        print(f"   ⏳ 正在下載 Batch {batch}/4...")
        url = f"https://eservice.ura.gov.sg/uraDataService/invokeUraDS/v1?service=PMI_Resi_Transaction&batch={batch}"
        res = requests.get(url, headers=headers)
        
        if res.text.startswith('<'):
            print(f"   ⚠️ Batch {batch} 被防火牆干擾，跳過。")
            continue
            
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
                
                f_range = t.get('floorRange', '未知樓層')
                if f_range == 'NIL': f_range = '未知樓層'

                # 🌟 將 area 轉換為浮點數，確保資料庫可以計算
                try:
                    area_val = float(area_sqm)
                except ValueError:
                    area_val = 0.0

                # 🌟 新增 area_val 寫入
                sale_records.append((p_name, s_name, c_date, calculated_type, f_range, price, area_val))
                
        # 🌟 SQL 新增 area 欄位
        cursor.executemany(
            "INSERT INTO ura_transactions (project, street, contractDate, flat_type, floorRange, price, area) VALUES (?, ?, ?, ?, ?, ?, ?)", 
            sale_records
        )
        total_sales += len(sale_records)
        time.sleep(1) # 溫柔一點
        
    print(f"✅ 買賣資料下載完成！共存入 {total_sales} 筆紀錄。")

    # ==========================================
    # 📦 階段二：洗劫私宅出租資料 (加強版：面積推估房型)
    # ==========================================
    print("\n🚀 [階段二] 開始下載 URA 私宅出租行情...")
    
    # 🌟 找回被遺忘的時光機！自動產生近 3 年 (12 季) 的 URA 時間格式
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
        
        if res.text.startswith('<'): 
            print(f"   ⚠️ 季別 {ref} 被防火牆阻擋，跳過。")
            continue
            
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
                    
                    # 🌟 改良版的判斷邏輯
                    bedrooms = str(r.get('noOfBedrooms', '')).strip().upper()
                    area = str(r.get('areaSqm', '0')).strip()
                    
                    if bedrooms and bedrooms not in ['NIL', 'NA']:
                        flat_type = f"{bedrooms}-Bedroom"
                    else:
                        # 啟動超級推估大腦
                        flat_type = estimate_sales_type(area, p_type)

                    # 🌟 新增 area 寫入
                    rent_records.append((p_name, s_name, l_date, flat_type, rent, area))
                    
            # 🌟 SQL 新增 area 欄位
            cursor.executemany(
                "INSERT INTO ura_rentals (project, street, leaseDate, flat_type, rent, area) VALUES (?, ?, ?, ?, ?, ?)", 
                rent_records
            )
            total_rents += len(rent_records)
            print(f"      👉 成功寫入 {len(rent_records)} 筆")
        except Exception as e:
            print(f"   ⚠️ 解析 {ref} 資料時發生錯誤: {e}")
            
        time.sleep(1) 
        
    conn.commit()
    conn.close()
    print(f"\n✅ 出租資料下載完成！共存入 {total_rents} 筆紀錄。")
    print("\n🎉 大功告成！所有 URA 精準資料已成功封裝進您的 SQLite 武器庫中！")

if __name__ == "__main__":
    main()
