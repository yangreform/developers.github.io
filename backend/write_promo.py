import sqlite3
import os

# 鎖死絕對路徑
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "landlord_sg.db")

def update_and_check_promo():
    # 1. 先確認檔案是否存在
    if not os.path.exists(DB_NAME):
        print(f"❌ 找不到資料庫檔案: {os.path.abspath(DB_NAME)}")
        return

    print(f"📂 正在操作資料庫: {os.path.abspath(DB_NAME)}")
    
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        # 2. 檢查資料表是否存在
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='featured_promo';")
        if not cursor.fetchone():
            print("❌ 資料表 'featured_promo' 不存在！請先執行初始化資料表的程式。")
            return

        # 3. 【核心修改】先清空舊資料
        cursor.execute("DELETE FROM featured_promo")
        print("🧹 已清空 featured_promo 資料表")

        # 4. 寫入新資料
        cursor.execute('''
            INSERT OR REPLACE INTO featured_promo (id, project_name, lat, lng, ig_link)
            VALUES (1, 'Tengah Garden Residences', 1.3588712, 103.725482, 'https://www.instagram.com/reel/DWga-90E6aj/?igsh=MWxhdzAxNnU4MWxmOQ==')
        ''')
        
        # 記得要 commit 才會生效
        conn.commit()
        print("💾 新資料已寫入並存檔")

        # 5. 查詢並列印結果確認
        cursor.execute("SELECT * FROM featured_promo")
        rows = cursor.fetchall()
        
        if not rows:
            print("⚠️ 資料表目前是空的。")
        else:
            print(f"✅ 檢查目前資料（共 {len(rows)} 筆）：")
            print("-" * 50)
            for row in rows:
                print(row)
            print("-" * 50)
            
    except Exception as e:
        print(f"💥 發生錯誤: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    update_and_check_promo()

