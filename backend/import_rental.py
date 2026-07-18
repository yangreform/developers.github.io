import sqlite3
import pandas as pd

# 1. 讀取 CSV 檔案
csv_file = 'RentingOutofFlatsfromJan2021.csv'
print(f"⏳ 正在讀取 {csv_file}...")
df = pd.read_csv(csv_file)

# 🌟 修正點：只填寫 CSV 裡「真正的」標題名稱，不要加 TEXT 或 REAL
columns_we_need = [
    'rent_approval_date', 
    'town', 
    'block', 
    'street_name', 
    'flat_type', 
    'monthly_rent'
]

# 過濾欄位
df_filtered = df[columns_we_need]

# 2. 連線到資料庫
conn = sqlite3.connect('landlord_sg.db')

# 3. 匯入資料
# index=False 確保不會把 Pandas 的索引當成資料存入
# SQLite 會自動幫 INTEGER PRIMARY KEY AUTOINCREMENT 的欄位填值
df_filtered.to_sql('hdb_rental', conn, if_exists='append', index=False)

conn.close()
print(f"🎉 成功！已將 {len(df_filtered)} 筆交易紀錄匯入 'hdb_rental' 表格！")
