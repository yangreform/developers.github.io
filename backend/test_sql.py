import sqlite3
import os

# 請確保這裡的 DB_NAME 與你 landlord_api.py 裡定義的一模一樣
    # 🌟 鎖死絕對路徑：抓取 landlord_api.py 所在的資料夾
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "landlord_sg.db")


def check_data():
    # 1. 先確認檔案是否存在
    if not os.path.exists(DB_NAME):
        print(f"❌ 找不到資料庫檔案: {os.path.abspath(DB_NAME)}")
        return

    print(f"📂 正在檢查資料庫: {os.path.abspath(DB_NAME)}")
    
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        # 2. 檢查資料表是否存在
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='featured_promo';")
        if not cursor.fetchone():
            print("❌ 資料表 'featured_promo' 不存在！請重新執行 init_promo_table()")
            return

        cursor.execute('''
            INSERT OR REPLACE INTO featured_promo (id, project_name, lat, lng, ig_link)
            VALUES (1, 'Tengah Garden Residences', 1.3588712, 103.725482, 'https://www.instagram.com/reel/DWga-90E6aj/?igsh=MWxhdzAxNnU4MWxmOQ==')
        ''')

        # 3. 查詢並列印資料
        cursor.execute("SELECT * FROM featured_promo")
        rows = cursor.fetchall()
        
        if not rows:
            print("⚠️ 資料表是空的，沒有任何資料。")
        else:
            print(f"✅ 找到 {len(rows)} 筆資料：")
            print("-" * 50)
            for row in rows:
                print(row)
            print("-" * 50)
            
    except Exception as e:
        print(f"💥 發生錯誤: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    check_data()

