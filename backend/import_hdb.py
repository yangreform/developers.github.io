import sqlite3
import pandas as pd

# 1. 讀取 CSV 檔案 (如果您的檔名不同，請修改這裡)
csv_file = 'hdb_data.csv'
print(f"⏳ 正在讀取 {csv_file}，這可能需要幾秒鐘...")
df = pd.read_csv(csv_file)

# 🌟 為了對齊我們之前開好的資料庫欄位，這裡做個安全過濾
columns_we_need = ['month', 'town', 'flat_type', 'block', 'street_name', 'floor_area_sqm', 'resale_price']
df_filtered = df[columns_we_need]

# 2. 連線到我們剛剛熱騰騰建好的 SQLite 資料庫
conn = sqlite3.connect('landlord_sg.db')

# 3. 🌟 核心魔法：一鍵將整台 Pandas 砂石車的資料，倒進 hdb_transactions 資料表！
# if_exists='append' 代表接在原本的表格後面
# index=False 代表不要把 pandas 內建的流水號存進去
df_filtered.to_sql('hdb_transactions', conn, if_exists='append', index=False)

conn.close()
print(f"🎉 太神啦！成功將 {len(df_filtered)} 筆 HDB 歷史交易紀錄灌入 SQLite！")
