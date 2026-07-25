import sqlite3
import requests
import time
import re
from html.parser import HTMLParser

DB_NAME = 'backend/landlord_sg.db'

class DDGParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
    def handle_data(self, data):
        self.text.append(data)

def extract_developer(full_text):
    # 多種常見的描述模式
    patterns = [
        r'(?i)developed by ([A-Za-z0-9\s]+?)(?:Pte Ltd|Ltd|Limited|Group|Corp|Developer|\.|,|-)',
        r'(?i)developer:?\s*([A-Za-z0-9\s]+?)(?:Pte Ltd|Ltd|Limited|Group|Corp|\.|,|-)',
        r'(?i)development by ([A-Za-z0-9\s]+?)(?:Pte Ltd|Ltd|Limited|Group|Corp|\.|,|-)'
    ]
    for p in patterns:
        m = re.search(p, full_text)
        if m:
            dev_name = m.group(1).strip()
            # 排除太長或太短的雜訊
            if 3 < len(dev_name) < 50 and 'property' not in dev_name.lower():
                return dev_name + ' Pte Ltd'
    return None

def search_developer(project_name):
    url = 'https://html.duckduckgo.com/html/'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    data = {'q': f'"{project_name}" developer singapore'}
    try:
        res = requests.post(url, headers=headers, data=data, timeout=10)
        if res.ok:
            parser = DDGParser()
            parser.feed(res.text)
            full_text = ' '.join(parser.text).replace('\n', ' ')
            return extract_developer(full_text)
    except:
        pass
    return None

def main():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # 確保表格存在
    cursor.execute("CREATE TABLE IF NOT EXISTS ura_developers (project TEXT PRIMARY KEY, developer_name TEXT)")
    
    # 找出所有在交易紀錄中，但沒有開發商資料的樓盤
    cursor.execute('''
        SELECT project FROM ura_transactions 
        WHERE project NOT IN (SELECT project FROM ura_developers)
        GROUP BY project
    ''')
    missing_projects = [r[0] for r in cursor.fetchall()]
    
    if not missing_projects:
        print("🎉 所有樓盤都已經有開發商資料了！")
        return
        
    print(f"🔍 發現 {len(missing_projects)} 個樓盤缺乏開發商資料，準備透過搜尋引擎自動查詢...")
    
    found_count = 0
    
    for i, project in enumerate(missing_projects):
        print(f"[{i+1}/{len(missing_projects)}] 查詢 {project} ... ", end='', flush=True)
        dev_name = search_developer(project)
        
        if dev_name:
            print(f"✅ 找到: {dev_name}")
            cursor.execute("INSERT INTO ura_developers (project, developer_name) VALUES (?, ?)", (project, dev_name))
            conn.commit()
            found_count += 1
        else:
            print("❌ 找不到 (將設為 unknown 以免重複查詢)")
            cursor.execute("INSERT INTO ura_developers (project, developer_name) VALUES (?, ?)", (project, "Unknown"))
            conn.commit()
            
        # 避免被搜尋引擎封鎖，隨機休息 2~4 秒
        time.sleep(2)
        
    conn.close()
    print(f"\n🎉 任務完成！成功找回 {found_count} 個開發商資料。")

if __name__ == '__main__':
    main()
