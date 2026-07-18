
META_TOKEN = "EAFuM14G26QkBQ1tcpQDTrnHABaxl31uwSMZArHEyAsyqUVTkuJAZCn6ZBCB9I0OvYj8ZCKkdzG2q8tV7CSKh9tZCLEXGDUO1jTr0PDLLRnao0KsW5slB1rpGCzZARd6E57gSbP99qPswCK0cDZBP8zgExk4StIph07jVZC0yFlk8gyaZB5iDrC1b72ZArvs7GPZAgZDZD"
PHONE_NUMBER_ID = "1063752500151477"

# 🌟 兇手就是少了這一段！請把它補在設定區的下方、路由的上方：
AGENT_NUMBERS = [
    '6580885201',
    '6580789177'
]


from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import datetime
import requests  # 🌟 匯入 requests 準備呼叫 Meta API


import os
# 🌟 鎖死絕對路徑：抓取 landlord_api.py 所在的資料夾
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "landlord_sg.db")
print(f"📂 目前 API 綁定的資料庫路徑為：{DB_NAME}")

app = Flask(__name__)
CORS(app)

# ==========================================
# 🌟 1. 資料庫初始化 (補回這個超重要的建表功能)
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # 建立 HDB 歷史資料表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS hdb_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            month TEXT,
            town TEXT,
            flat_type TEXT,
            block TEXT,
            street_name TEXT,
            floor_area_sqm REAL,
            resale_price REAL
        )
    ''')
    
    # 建立 客戶名單 (Leads) 資料表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            phone TEXT,
            building TEXT,
            price REAL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS hdb_rental (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rent_approval_date TEXT,
            town TEXT,
            block TEXT,
            street_name TEXT,
            flat_type TEXT,
            monthly_rent REAL
        )
    ''')
    
    conn.commit()
    conn.close()

# 🌟 讓這支程式被 router.py 載入時，自動執行一次建表檢查！
init_db()


# ==========================================
# 🌟 3. 接收賣房估價名單 API (掛載 Meta WhatsApp 引擎)
# ==========================================
@app.route('/leads', methods=['POST'])  # 🌟 加上 /app 讓 Flutter 找得到
def save_lead():
    data = request.get_json()
    if not data or not data.get('phone'):
        return jsonify({"status": "error", "message": "缺少手機號碼"}), 400
        
    phone = data.get('phone')
    building = data.get('building', '未知建案')
    price = data.get('price', 0.0)
    created_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 1. 寫入 SQLite 資料庫
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO leads (created_at, phone, building, price) VALUES (?, ?, ?, ?)",
        (created_at, phone, building, price)
    )
    conn.commit()
    conn.close()
    
    print(f"🎉 [資料庫] 收到新名單！電話: {phone}, 建案: {building}, 估價: {price}")
    
    # 2. 🚀 觸發 Meta WhatsApp 範本推播 (無視 24 小時限制)
    try:
        headers = {
            "Authorization": f"Bearer {META_TOKEN}",
            "Content-Type": "application/json"
        }
        url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
        
        # 幫金額加上千分位逗號，看起來更專業
        formatted_price = f"{price:,.0f}"
        
        # 迴圈發送給名單上的每一位房仲
        for agent in AGENT_NUMBERS:
            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": agent,
                "type": "template",  # 🌟 關鍵：宣告這是一則範本訊息
                "template": {
                    "name": "new_lead_alert",  # 🌟 必須與您在 Meta 建立的範本名稱一模一樣
                    "language": {
                        "code": "zh_CN"        # 🌟 必須與您建立範本時選擇的語系一致
                    },
                    "components": [
                        {
                            "type": "body",
                            "parameters": [
                                { "type": "text", "text": building },       # 對應 {{1}}
                                { "type": "text", "text": formatted_price },# 對應 {{2}}
                                { "type": "text", "text": phone },          # 對應 {{3}}
                                { "type": "text", "text": created_at }      # 對應 {{4}}
                            ]
                        }
                    ]
                }
            }
            
            response = requests.post(url, headers=headers, json=payload)
            result = response.json()
            
            if response.status_code == 200:
                print(f"✅ 成功以【範本模式】推播至 {agent} (訊息ID: {result['messages'][0]['id']})")
            else:
                print(f"❌ 推播至 {agent} 失敗，Meta 回傳錯誤: {result}")
            
    except Exception as e:
        print(f"❌ 執行 WhatsApp 發送程式時發生嚴重錯誤: {e}")
    
    return jsonify({"status": "success", "message": "名單已儲存，推播作業結束"})



# ==========================================
# 🌟 HDB 買賣查詢 (終極智慧分詞 + 型別強制轉換版)
# ==========================================
@app.route('/hdb', methods=['GET'])
def get_hdb_data():
    street = request.args.get('street', '').strip().upper()
    block = request.args.get('block', '').strip().upper()
    
    words = [w for w in street.split() if w]
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row  
    cursor = conn.cursor()
    
    # 🌟 就是這行！必須加上 CAST(block AS TEXT) 才能破解 Pandas 的整數陷阱！
    query = "SELECT * FROM hdb WHERE CAST(block AS TEXT) LIKE ?"
    params = [block + '%']  
    
    for w in words:
        query += " AND street_name LIKE ?"
        params.append('%' + w + '%')
        
    cursor.execute(query, tuple(params))
    records = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    print(f"📊 [HDB買賣] 搜尋 棟號:{block} 街道:{street}，找到 {len(records)} 筆紀錄")
    return jsonify({"status": "success", "records": records})

# ==========================================
# 🌟 HDB 租金查詢 (終極智慧分詞 + 型別強制轉換版)
# ==========================================
@app.route('/hdb_rent', methods=['GET'])
def get_hdb_rent_data():
    street = request.args.get('street', '').strip().upper()
    block = request.args.get('block', '').strip().upper()
    
    words = [w for w in street.split() if w]
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row  
    cursor = conn.cursor()
    
    # 🌟 同樣的，這裡也必須加上 CAST(block AS TEXT)
    query = "SELECT * FROM hdb_rental WHERE CAST(block AS TEXT) LIKE ?"
    params = [block + '%']
    
    for w in words:
        query += " AND street_name LIKE ?"
        params.append('%' + w + '%')
        
    cursor.execute(query, tuple(params))
    records = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    print(f"📊 [HDB租金] 搜尋 棟號:{block} 街道:{street}，找到 {len(records)} 筆紀錄")
    return jsonify({"status": "success", "records": records})







# ==========================================
# 🌟 URA 私宅買賣查詢 API (極速版)
# ==========================================
@app.route('/ura', methods=['GET'])
def get_ura_data():
    keyword = request.args.get('keyword', '').upper()
    if not keyword:
        return jsonify({"status": "error", "message": "請提供建案或街道名稱"}), 400
        
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row  
    cursor = conn.cursor()
    
    # 模糊搜尋建案名稱或街道名稱
    cursor.execute("SELECT * FROM ura_transactions WHERE project LIKE ? OR street LIKE ?", ('%' + keyword + '%', '%' + keyword + '%'))
    records = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    print(f"📊 [私宅買賣] 搜尋 '{keyword}'，在資料庫找到 {len(records)} 筆紀錄")
    return jsonify({"status": "success", "total": len(records), "records": records})

# ==========================================
# 🌟 URA 私宅租金查詢 API (極速版)
# ==========================================
@app.route('/ura_rent', methods=['GET'])
def get_ura_rent_data():
    keyword = request.args.get('keyword', '').upper()
    if not keyword:
        return jsonify({"status": "error", "message": "請提供建案或街道名稱"}), 400
        
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row  
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM ura_rentals WHERE project LIKE ? OR street LIKE ?", ('%' + keyword + '%', '%' + keyword + '%'))
    records = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    print(f"📊 [私宅租金] 搜尋 '{keyword}'，在資料庫找到 {len(records)} 筆紀錄")
    return jsonify({"status": "success", "total": len(records), "records": records})
