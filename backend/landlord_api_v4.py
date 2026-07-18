
META_TOKEN = "EAAShf2WkavcBRLSa37S2IUHoSDoseKff2pI2k2OU3ZBkZCjrL45CwUxYquK7ZBf3cr1B5ektXFrwLow8tZB86PDHk2KGhq2O2ql9VRXQRlwDrV3NcB5eBGuZAwrA6tmEL8VoHAqDA7jOnA0arS8rxB9VfcignIrmo1fOkeVmD6MxnqS7QEB4NdMXi2hhRzwZDZD"
PHONE_NUMBER_ID = "1124517050733935"

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
import random


import os
# 🌟 鎖死絕對路徑：抓取 landlord_api.py 所在的資料夾
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "landlord_sg.db")
print(f"📂 目前 API 綁定的資料庫路徑為：{DB_NAME}")

app = Flask(__name__)
CORS(app)




def init_user_table():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # 建立使用者表：記錄手機號碼、查詢次數、帳號是否啟用
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            phone TEXT PRIMARY KEY,
            query_count INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

# 在程式啟動時執行一次
init_user_table()





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
                                { "type": "text", "text": phone }          # 對應 {{3}}
                                #{ "type": "text", "text": created_at }      # 對應 {{4}}
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


@app.route('/heatmap/ura', methods=['GET', 'OPTIONS'])
def get_ura_heatmap():
    if request.method == 'OPTIONS': return jsonify({"status": "success"}), 200
    
    # 🌟 從 Request Header 中取得使用者的電話號碼
    user_phone = request.headers.get('X-User-Phone')
    
    # 🌟 記錄查詢量並檢查權限
    if not record_user_query(user_phone):
        return jsonify({"status": "error", "message": "您的帳號查詢量已達上限或被停用，請聯絡客服。"}), 403

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row  
    cursor = conn.cursor()
    
    # 🌟 核心演算法：合併座標表與交易表，過濾近 1 年資料並計算平均
    # URA 的 contractDate 通常是 'MMYY' 格式 (例如 '0325' 代表 2025 年 3 月，'0126' 代表 2026 年 1 月)
    # 這裡抓取結尾是 25 或 26 的資料，剛好涵蓋最近 1 年多
    query = """
        SELECT 
            c.project,
            c.lat, 
            c.lng,
            COUNT(*) as tx_count,
            AVG(t.price) as avg_price
            , AVG(t.price / (t.area * 10.7639)) as avg_psf
        FROM ura_coordinates c
        JOIN ura_transactions t ON c.project = t.project
        WHERE (t.contractDate LIKE '%25' OR t.contractDate LIKE '%26') 
        
        -- 🌟 殺手鐧：永遠封殺 URA 的通用垃圾名稱，保持熱力圖純淨！
        AND c.project NOT IN (
            'LANDED HOUSING DEVELOPMENT', 
            'RESIDENTIAL APARTMENTS', 
            'DETACHED HOUSE', 
            'SEMI-DETACHED HOUSE', 
            'TERRACE HOUSE',
            'NIL'
        )
        
        GROUP BY c.project
        HAVING c.lat IS NOT NULL AND c.lng IS NOT NULL
    """
    
    try:
        cursor.execute(query)
        records = [dict(row) for row in cursor.fetchall()]
        print(f"🗺️ [熱力圖] 成功產出 {len(records)} 個建案的熱點資料！")
        
        return jsonify({"status": "success", "total": len(records), "data": records})
        
    except Exception as e:
        print(f"❌ 熱力圖 API 發生錯誤: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()



# 🌟 暫時用來儲存 OTP 的記憶體字典 (實務上可放資料庫)
otp_storage = {}

# ==========================================
# 1. 發送 WhatsApp OTP API
# ==========================================
@app.route('/otp/send', methods=['POST', 'OPTIONS'])
def send_otp():
    if request.method == 'OPTIONS':
        return jsonify({"status": "success"}), 200

    data = request.json
    phone = data.get('phone') # 格式需為 6588888888 (去掉 + 號)

    # 產生 4 位數隨機密碼
    otp_code = str(random.randint(1000, 9999))
    otp_storage[phone] = otp_code
    print(f"🔑 產生 OTP 給 {phone}: {otp_code}")

    # 呼叫 Meta API 發射！
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "template",
        "template": {
            "name": "sn1", # 🌟 這裡填寫您剛通過審核的範本名稱
            "language": {"code": "en"}, # 🌟 依照您申請時的語言填寫 (例如 en_US)
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": otp_code}] # 把 4 位數塞進 {{1}}
                },
            ]
        }
    }

    res = requests.post(url, headers=headers, json=payload)
    print(f"📡 Meta 回傳結果: {res.text}")

    if res.status_code == 200:
        return jsonify({"status": "success"})
    else:
        return jsonify({"status": "error", "message": "WhatsApp 發送失敗"}), 500

# ==========================================
# 2. 核對 OTP API
# ==========================================
@app.route('/otp/verify', methods=['POST', 'OPTIONS'])
def verify_otp():
    if request.method == 'OPTIONS':
        return jsonify({"status": "success"}), 200

    data = request.json
    phone = data.get('phone')
    user_code = data.get('code')

    # 核對密碼
    real_code = otp_storage.get(phone)
    if real_code and real_code == user_code:
        # 🌟 驗證成功！將使用者加入資料庫 (如果已經存在就不理他)
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (phone) VALUES (?)", (phone,))
        conn.commit()
        conn.close()
        
        return jsonify({"status": "success"})

def record_user_query(phone):
    """記錄使用者的查詢次數，並檢查帳號是否被停用"""
    if not phone: return True # 如果沒傳電話 (可能是未登入體驗)，暫時放行
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # 檢查該使用者狀態
    cursor.execute("SELECT is_active FROM users WHERE phone = ?", (phone,))
    user = cursor.fetchone()
    
    if user:
        if user['is_active'] == 0:
            conn.close()
            return False # 🚨 帳號已被停用
        
        # 查詢次數 + 1
        cursor.execute("UPDATE users SET query_count = query_count + 1 WHERE phone = ?", (phone,))
        conn.commit()
        
    conn.close()
    return True
