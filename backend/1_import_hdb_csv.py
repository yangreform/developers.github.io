import sqlite3
import pandas as pd

def import_csv_to_sqlite():
    # 1. 連線到您的資料庫
    db_name = 'landlord_sg.db'
    conn = sqlite3.connect(db_name)
    print(f"🔌 已連線至資料庫：{db_name}")

    # ==========================================
    # 📦 任務 1：匯入 HDB 出租資料 (Rental)
    # ==========================================
    csv_rental = 'RentingOutofFlatsfromJan2021.csv'
    print(f"\n⏳ [任務 1] 正在讀取 {csv_rental}...")
    df_rental = pd.read_csv(csv_rental)

    # 挑選出我們需要的出租欄位
    columns_rental = [
        'rent_approval_date', 'town', 'block', 'street_name', 'flat_type', 'monthly_rent'
    ]
    df_rental_filtered = df_rental[columns_rental]

    # 🌟 修正點：使用 if_exists='replace'，全自動處理建表與覆蓋
    print("🚀 正在建立並寫入最新的出租紀錄...")
    df_rental_filtered.to_sql('hdb_rental', conn, if_exists='replace', index=False)
    print(f"✅ 成功！已匯入 {len(df_rental_filtered)} 筆出租資料。")


    # ==========================================
    # 📦 任務 2：匯入 HDB 買賣資料 (Resale)
    # ==========================================
    csv_resale = 'ResaleflatpricesbasedonregistrationdatefromJan2017onwards.csv'
    print(f"\n⏳ [任務 2] 正在讀取 {csv_resale}...")
    df_resale = pd.read_csv(csv_resale)

    # 挑選出所有的買賣欄位
    columns_resale = [
        'month', 'town', 'flat_type', 'block', 'street_name', 
        'storey_range', 'floor_area_sqm', 'flat_model', 
        'lease_commence_date', 'remaining_lease', 'resale_price'
    ]
    df_resale_filtered = df_resale[columns_resale]

    # 🌟 修正點：使用 if_exists='replace'，全自動處理建表與覆蓋
    print("🚀 正在建立並寫入最新的買賣紀錄...")
    df_resale_filtered.to_sql('hdb', conn, if_exists='replace', index=False)
    print(f"✅ 成功！已匯入 {len(df_resale_filtered)} 筆買賣資料。")


    # ==========================================
    # 結尾：關閉連線
    # ==========================================
    conn.close()
    print("\n🎉 大功告成！兩大 CSV 資料皆已完美匯入您的 SQLite 資料庫！")

if __name__ == "__main__":
    import_csv_to_sqlite()
