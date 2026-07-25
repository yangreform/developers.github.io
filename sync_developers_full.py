import os
import time
import json
import sqlite3
import datetime
import requests

# 這是專門用來爬取過去十幾年 (2010-2025) 所有一手開發商銷售紀錄的腳本
# 用於補齊資料庫 ura_developers 中所有建案的開發商名稱

DB_NAME = 'backend/landlord_sg.db'
KEY_FILE = 'backend/URA_accessKey.txt'

def get_ura_token(access_key):
    print("\n🔑 正在向 URA 申請 Token...")
    headers = {"User-Agent": "Mozilla/5.0", "AccessKey": access_key}
    res = requests.get("https://eservice.ura.gov.sg/uraDataService/insertNewToken/v1", headers=headers)
    if res.text.startswith('<'):
        raise Exception("❌ 被 URA 阻擋，請稍候重試！")
    return res.json().get('Result', '')

def sync_all_developers():
    access_key = "78bab9ae-c762-4c3d-8707-628138727c83"
    
    token = get_ura_token(access_key)
    if not token:
        print("無法取得 Token")
        return
        
    headers = {"User-Agent": "Mozilla/5.0", "AccessKey": access_key, "Token": token}
    
    # 產生 2010 年到今年的所有 mmyy 組合 (約 180 個月份)
    # URA API 的上限是一天 200~250 個 Request，這剛好可以在一次執行內完成！
    start_year = 2010
    end_year = datetime.date.today().year
    
    months = []
    for y in range(start_year, end_year + 1):
        for m in range(1, 13):
            # 避免抓到未來的月份
            if y == end_year and m > datetime.date.today().month:
                continue
            months.append(f"{m:02d}{str(y)[-2:]}")
            
    print(f"\n🚀 準備掃描 {len(months)} 個歷史月份的開發商紀錄 (從 {start_year} 到 {end_year})...")
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS ura_developers (project TEXT PRIMARY KEY, developer_name TEXT)")
    
    total_new = 0
    total_scanned = 0
    
    for i, ref in enumerate(months):
        print(f"[{i+1}/{len(months)}] ⏳ 正在查詢 {ref}...")
        url = f"https://eservice.ura.gov.sg/uraDataService/invokeUraDS/v1?service=PMI_Resi_Developer_Sales&refPeriod={ref}"
        
        try:
            res = requests.get(url, headers=headers)
            if res.text.startswith('<'):
                print("   ⚠️ 遭到阻擋，暫停 3 秒...")
                time.sleep(3)
                continue
                
            data = res.json().get('Result', [])
            dev_records = []
            for project in data:
                p_name = project.get('project', '')
                d_name = project.get('developerName', '')
                if p_name and d_name:
                    dev_records.append((p_name, d_name))
            
            if dev_records:
                cursor.executemany("INSERT OR IGNORE INTO ura_developers (project, developer_name) VALUES (?, ?)", dev_records)
                total_new += cursor.rowcount
                total_scanned += len(dev_records)
                print(f"   ✅ 找到 {len(dev_records)} 筆紀錄")
                
        except Exception as e:
            print(f"   ❌ 查詢 {ref} 失敗: {e}")
            
        # 遵守 API 速率限制
        time.sleep(0.5)

    conn.commit()
    conn.close()
    
    print(f"\n🎉 大功告成！")
    print(f"共掃描了 {total_scanned} 筆銷售紀錄，成功為資料庫新增了 {total_new} 個開發商資料！")

if __name__ == '__main__':
    sync_all_developers()
