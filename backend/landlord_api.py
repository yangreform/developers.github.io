
META_TOKEN = "EAAShf2WkavcBRLSa37S2IUHoSDoseKff2pI2k2OU3ZBkZCjrL45CwUxYquK7ZBf3cr1B5ektXFrwLow8tZB86PDHk2KGhq2O2ql9VRXQRlwDrV3NcB5eBGuZAwrA6tmEL8VoHAqDA7jOnA0arS8rxB9VfcignIrmo1fOkeVmD6MxnqS7QEB4NdMXi2hhRzwZDZD"
PHONE_NUMBER_ID = "1124517050733935"

# 🌟 兇手就是少了這一段！請把它補在設定區的下方、路由的上方：
AGENT_NUMBERS = [
    #'6580789177',
    '6580885201'
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
CORS(app, resources={r"/*": {"origins": "*"}})

# ── 全域 CORS Header（確保 null origin / file:// / GitHub Pages 都能存取）──
@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin']  = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-User-Phone, ngrok-skip-browser-warning'
    return response

@app.before_request
def handle_options():
    """全域處理 OPTIONS preflight request"""
    if request.method == 'OPTIONS':
        resp = app.make_default_options_response()
        resp.headers['Access-Control-Allow-Origin']  = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-User-Phone, ngrok-skip-browser-warning'
        resp.headers['Access-Control-Max-Age']       = '86400'
        return resp




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
    if request.method == 'OPTIONS':
        return '', 200

    #print("\n--- [DEBUG] 🚀 進入 /app/heatmap/ura API ---")
    try:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        print("[DEBUG] 1. 資料庫連線成功")
    except Exception as e:
        print(f"❌ [ERROR] 1. 資料庫連線失敗: {e}")
        return jsonify({"status": "error", "message": "DB Connection Error"}), 500

    try:
        # ==========================================
        # 🌟 [功能 1] 計數器
        # ==========================================
        user_phone = request.headers.get('X-User-Phone', '').strip()
        print(f"[DEBUG] 2. 收到 Header 手機號碼: '{user_phone}'")
        
        if user_phone and user_phone != "尚未綁定" and user_phone != "":
            safe_phone = ''.join(c for c in user_phone if c.isdigit() or c == '+')
            print(f"[DEBUG] 3. 解析出純數字手機號碼: '{safe_phone}'")
            
            if safe_phone:
                # 🌟 修正點：把 SELECT id 改成 SELECT phone
                cursor.execute("SELECT phone FROM users WHERE phone = ?", (safe_phone,))
                user = cursor.fetchone()
                print(f"[DEBUG] 4. 查詢舊用戶結果: {'找到老朋友' if user else '這是新朋友'}")

                if user:
                    cursor.execute("UPDATE users SET query_count = query_count + 1 WHERE phone = ?", (safe_phone,))
                    print("[DEBUG] 5. 更新舊用戶查詢次數成功")
                else:
                    cursor.execute("INSERT INTO users (phone, query_count, is_active) VALUES (?, 1, 1)", (safe_phone,))
                    print("[DEBUG] 5. 新增用戶建檔成功")
                
                conn.commit() 
                print("[DEBUG] 6. 計數器寫入資料庫成功")

        # ==========================================
        # 🌟 [功能 2] 熱力圖 (極速 + 無日期限制版)
        # ==========================================
        #print("[DEBUG] 7. 準備執行熱力圖 SQL 查詢...")
        query = """
            SELECT 
                c.project, c.lat, c.lng, c.postal,
                COUNT(*) as tx_count,
                AVG(t.price) as avg_price,
                AVG(t.price / (t.area * 10.7639)) as avg_psf
            FROM ura_coordinates c
            JOIN ura_transactions t ON c.project = t.project
            -- 🌟 注意：我們把 WHERE contract_date 這一行徹底刪除了！
            GROUP BY c.project
            HAVING tx_count > 0
        """
        cursor.execute(query)
        heatmap_rows = cursor.fetchall()
        heatmap_data = [dict(row) for row in heatmap_rows]
        #print(f"[DEBUG] 8. 熱力圖查詢成功！共抓取 {len(heatmap_data)} 筆建案資料")


        cursor.execute(query)
        heatmap_rows = cursor.fetchall()
        heatmap_data = [dict(row) for row in heatmap_rows]
        #print(f"[DEBUG] 8. 熱力圖查詢成功！共抓取 {len(heatmap_data)} 筆建案資料")

        # ==========================================
        # 🌟 [功能 3] 強推廣告
        # ==========================================
        #print("[DEBUG] 9. 準備執行強推建案 SQL 查詢...")
        promo_data = None
        try:
            #cursor.execute("SELECT project_name, lat, lng, ig_link FROM featured_promo WHERE is_active = 1 ORDER BY updated_at DESC LIMIT 1")
            cursor.execute("SELECT * FROM featured_promo")
            promo_row = cursor.fetchone()
            if promo_row:
                promo_data = {
                    "name": promo_row['project_name'],
                    "lat": promo_row['lat'],
                    "lng": promo_row['lng'],
                    "ig_link": promo_row['ig_link']
                }

            # 3. 查詢並列印資料
            if not promo_row:
                print("⚠️ 資料表是空的，沒有任何資料。")
            else:
                print(f"✅ 找到 {len(promo_row)} 筆資料：")
                print("-" * 50)
                for row in promo_row:
                    print(row)
                print("-" * 50)


        except Exception as e:
            print(f"⚠️ [DEBUG] 10. (警告) 強推建案查詢失敗 (可忽略): {e}")

        # ==========================================
        # 🌟 [功能 4] 完美打包回傳給 Flutter
        # ==========================================
        # 💡 您可以隨時在這裡切換 True 或 False
        # (未來甚至可以把這個值寫進 SQL 的系統設定表裡)
        #is_otp_required = True 
        is_otp_required = False

        return jsonify({
            "status": "success",
            "data": {
                "heatmap": heatmap_data,
                "promo": promo_data,
                "require_otp": is_otp_required  # 🌟 新增：將開關狀態傳給手機
            }
        })
        
    except Exception as e:
        conn.rollback()
        # 🚨 這裡是最關鍵的錯誤印出點！
        print(f"\n💥 [FATAL ERROR] 程式發生中斷錯誤！")
        print(f"💥 錯誤類型: {type(e).__name__}")
        print(f"💥 錯誤原因: {str(e)}\n")
        return jsonify({"status": "error", "message": str(e)}), 500
        
    finally:
        print("[DEBUG] 12. 關閉資料庫連線")
        print("--- [DEBUG] 🏁 API 執行結束 ---\n")
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


# ==========================================
# 🔥 熱力圖通用 API — 前端 heatmap.html 使用
# 支援 type: ura / hdb / all
# 支援 metric: price / psf / count
# 支援 year: 2020 / 2021 / ... / all
# ==========================================
@app.route('/api/get_heatmap_data', methods=['GET', 'OPTIONS'])
def get_heatmap_data():
    if request.method == 'OPTIONS':
        response = jsonify({'status': 'ok'})
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Headers'] = '*'
        return response, 200

    data_type = request.args.get('type', 'ura').lower()   # ura | hdb | all
    metric    = request.args.get('metric', 'price').lower()  # price | psf | count
    year      = request.args.get('year', 'all')              # all | 2017~2026

    try:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        results = []

        # ─── URA 私宅 ────────────────────────────────────────
        if data_type in ('ura', 'all'):
            # 日期篩選條件：contractDate 格式為 "YYMM" e.g. "2301" = 2023年1月
            year_filter = ""
            if year != 'all':
                try:
                    yy = str(int(year) % 100).zfill(2)   # 2023 → "23"
                    year_filter = f"AND t.contractDate LIKE '{yy}%'"
                except Exception:
                    pass

            if metric == 'count':
                weight_expr = "COUNT(*)"
            elif metric == 'psf':
                weight_expr = "AVG(t.price / NULLIF(t.area * 10.7639, 0))"
            else:
                weight_expr = "AVG(t.price)"

            ura_query = f"""
                SELECT
                    c.lat,
                    c.lng,
                    c.project AS label,
                    {weight_expr} AS weight
                FROM ura_coordinates c
                JOIN ura_transactions t ON c.project = t.project
                WHERE c.lat IS NOT NULL AND c.lng IS NOT NULL {year_filter}
                GROUP BY c.project
                HAVING weight > 0
            """
            cursor.execute(ura_query)
            for row in cursor.fetchall():
                results.append({
                    'lat':    row['lat'],
                    'lng':    row['lng'],
                    'label':  row['label'],
                    'weight': float(row['weight']) if row['weight'] else 0,
                    'source': 'ura'
                })

        # ─── HDB 組屋（使用 ura_coordinates 裡最近的座標） ──
        if data_type in ('hdb', 'all'):
            # HDB month 格式 "YYYY-MM"
            year_filter_hdb = ""
            if year != 'all':
                try:
                    year_filter_hdb = f"AND h.month LIKE '{year}%'"
                except Exception:
                    pass

            if metric == 'count':
                hdb_weight_expr = "COUNT(*)"
            elif metric == 'psf':
                hdb_weight_expr = "AVG(h.resale_price / NULLIF(h.floor_area_sqm * 10.7639, 0))"
            else:
                hdb_weight_expr = "AVG(h.resale_price)"

            # 用 town 關聯 ura_coordinates 的 street 欄位取近似座標
            # 更精準做法：用 town 中心點（預先計算好）；此處用 GROUP BY town 搭配已知座標
            hdb_query = f"""
                SELECT
                    h.town,
                    {hdb_weight_expr} AS weight,
                    COUNT(*) AS tx_count
                FROM hdb h
                WHERE 1=1 {year_filter_hdb}
                GROUP BY h.town
                HAVING weight > 0
            """
            cursor.execute(hdb_query)
            hdb_rows = [dict(r) for r in cursor.fetchall()]

            # HDB 市鎮中心座標（新加坡 26 個市鎮）
            TOWN_COORDS = {
                'ANG MO KIO':      (1.3691, 103.8454),
                'BEDOK':           (1.3236, 103.9273),
                'BISHAN':          (1.3526, 103.8352),
                'BUKIT BATOK':     (1.3590, 103.7637),
                'BUKIT MERAH':     (1.2819, 103.8239),
                'BUKIT PANJANG':   (1.3774, 103.7719),
                'BUKIT TIMAH':     (1.3294, 103.7959),
                'CENTRAL AREA':    (1.2897, 103.8501),
                'CHOA CHU KANG':   (1.3840, 103.7470),
                'CLEMENTI':        (1.3151, 103.7651),
                'GEYLANG':         (1.3201, 103.8918),
                'HOUGANG':         (1.3612, 103.8863),
                'JURONG EAST':     (1.3329, 103.7436),
                'JURONG WEST':     (1.3404, 103.7090),
                'KALLANG/WHAMPOA': (1.3100, 103.8651),
                'MARINE PARADE':   (1.3025, 103.9054),
                'PASIR RIS':       (1.3721, 103.9474),
                'PUNGGOL':         (1.4019, 103.9021),
                'QUEENSTOWN':      (1.2942, 103.7861),
                'SEMBAWANG':       (1.4491, 103.8185),
                'SENGKANG':        (1.3868, 103.8914),
                'SERANGOON':       (1.3554, 103.8679),
                'TAMPINES':        (1.3496, 103.9568),
                'TOA PAYOH':       (1.3343, 103.8563),
                'WOODLANDS':       (1.4382, 103.7890),
                'YISHUN':          (1.4304, 103.8354),
            }

            for row in hdb_rows:
                town = row['town'].strip().upper()
                coords = TOWN_COORDS.get(town)
                if coords:
                    results.append({
                        'lat':    coords[0],
                        'lng':    coords[1],
                        'label':  town,
                        'weight': float(row['weight']) if row['weight'] else 0,
                        'source': 'hdb'
                    })

        conn.close()

        # 正規化權重到 0~1 方便前端調整半徑
        if results:
            max_w = max(r['weight'] for r in results) or 1
            for r in results:
                r['weight_norm'] = round(r['weight'] / max_w, 4)

        return jsonify({
            'status':  'success',
            'count':   len(results),
            'metric':  metric,
            'type':    data_type,
            'year':    year,
            'data':    results
        })

    except Exception as e:
        print(f"❌ [get_heatmap_data ERROR] {type(e).__name__}: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


# ==========================================
# 🔥 HDB 熱力圖獨立 API（依市鎮統計）
# ==========================================
@app.route('/heatmap/hdb', methods=['GET', 'OPTIONS'])
def get_hdb_heatmap():
    if request.method == 'OPTIONS':
        return '', 200

    try:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                town,
                COUNT(*) as tx_count,
                AVG(resale_price) as avg_price,
                AVG(resale_price / NULLIF(floor_area_sqm * 10.7639, 0)) as avg_psf
            FROM hdb
            GROUP BY town
            HAVING tx_count > 0
        """)
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()

        TOWN_COORDS = {
            'ANG MO KIO':      (1.3691, 103.8454),
            'BEDOK':           (1.3236, 103.9273),
            'BISHAN':          (1.3526, 103.8352),
            'BUKIT BATOK':     (1.3590, 103.7637),
            'BUKIT MERAH':     (1.2819, 103.8239),
            'BUKIT PANJANG':   (1.3774, 103.7719),
            'BUKIT TIMAH':     (1.3294, 103.7959),
            'CENTRAL AREA':    (1.2897, 103.8501),
            'CHOA CHU KANG':   (1.3840, 103.7470),
            'CLEMENTI':        (1.3151, 103.7651),
            'GEYLANG':         (1.3201, 103.8918),
            'HOUGANG':         (1.3612, 103.8863),
            'JURONG EAST':     (1.3329, 103.7436),
            'JURONG WEST':     (1.3404, 103.7090),
            'KALLANG/WHAMPOA': (1.3100, 103.8651),
            'MARINE PARADE':   (1.3025, 103.9054),
            'PASIR RIS':       (1.3721, 103.9474),
            'PUNGGOL':         (1.4019, 103.9021),
            'QUEENSTOWN':      (1.2942, 103.7861),
            'SEMBAWANG':       (1.4491, 103.8185),
            'SENGKANG':        (1.3868, 103.8914),
            'SERANGOON':       (1.3554, 103.8679),
            'TAMPINES':        (1.3496, 103.9568),
            'TOA PAYOH':       (1.3343, 103.8563),
            'WOODLANDS':       (1.4382, 103.7890),
            'YISHUN':          (1.4304, 103.8354),
        }

        heatmap_data = []
        for row in rows:
            town = row['town'].strip().upper()
            coords = TOWN_COORDS.get(town)
            if coords:
                heatmap_data.append({
                    'town':      town,
                    'lat':       coords[0],
                    'lng':       coords[1],
                    'tx_count':  row['tx_count'],
                    'avg_price': round(row['avg_price'], 0) if row['avg_price'] else 0,
                    'avg_psf':   round(row['avg_psf'], 2) if row['avg_psf'] else 0,
                })

        return jsonify({
            'status': 'success',
            'count':  len(heatmap_data),
            'data':   heatmap_data
        })

    except Exception as e:
        print(f"❌ [get_hdb_heatmap ERROR] {type(e).__name__}: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


# ==========================================
# 📈 URA 私宅樓盤 — 年均漲幅 & 3年預估價 API
# GET /api/ura_price_trend
# 可選 query param: limit (default 200)
# ==========================================
@app.route('/api/ura_price_trend', methods=['GET', 'OPTIONS'])
def get_ura_price_trend():
    limit = request.args.get('limit', 200, type=int)
    limit = min(max(limit, 1), 1000)   # clamp 1~1000

    try:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Step 1: 取每個樓盤各年度的平均成交價
        # contractDate 格式 MMYY (4位), 年份 = SUBSTR(contractDate,3,2)
        cursor.execute("""
            SELECT
                t.project,
                c.postal,
                CAST('20' || SUBSTR(t.contractDate,3,2) AS INTEGER) AS year,
                AVG(t.price) AS avg_price,
                AVG(t.price / NULLIF(t.area * 10.7639, 0)) AS avg_psf,
                COUNT(*) AS tx_count
            FROM ura_transactions t
            JOIN ura_coordinates c ON t.project = c.project
            WHERE t.price > 0
              AND t.area  > 0
              AND LENGTH(t.contractDate) = 4
            GROUP BY t.project, year
            HAVING tx_count >= 2
            ORDER BY t.project, year
        """)
        rows = cursor.fetchall()
        conn.close()

        # Step 2: 整理成 project -> {year: avg_price} 的字典
        from collections import defaultdict
        project_years = defaultdict(dict)
        project_postal = {}
        project_psf    = defaultdict(dict)

        for r in rows:
            proj = r['project']
            yr   = r['year']
            project_years[proj][yr]  = r['avg_price']
            project_psf[proj][yr]    = r['avg_psf'] if r['avg_psf'] else 0
            project_postal[proj]     = r['postal']

        # Step 3: 計算每個樓盤的 CAGR 及 3年預估價
        CURRENT_YEAR = 2025   # 以 2025 為「當前」基準
        CAGR_MIN = -30.0      # 過濾異常（整幢收購/清盤等極端值）
        CAGR_MAX = +40.0
        results = []

        for proj, year_data in project_years.items():
            years_sorted = sorted(year_data.keys())
            if len(years_sorted) < 2:
                continue

            # 取最早年和最晚年（排除 2026 部分年資料）
            years_full = [y for y in years_sorted if y <= CURRENT_YEAR]
            if len(years_full) < 2:
                years_full = years_sorted   # 若只有 2026 資料，仍計算

            earliest_yr    = years_full[0]
            latest_yr      = years_full[-1]
            earliest_price = year_data[earliest_yr]
            latest_price   = year_data[latest_yr]

            n_years = latest_yr - earliest_yr
            if n_years <= 0 or earliest_price <= 0:
                continue

            # CAGR = (latest/earliest)^(1/n) - 1
            import math
            cagr = (math.pow(latest_price / earliest_price, 1.0 / n_years) - 1) * 100

            # 過濾異常值（例如整幢收購、清盤等極端交易）
            if cagr < CAGR_MIN or cagr > CAGR_MAX:
                continue
            # 資料跨度太短（只有1年）不夠準確
            if n_years < 2:
                continue

            # 3年後預估價（以 latest_price 為起點，用 CAGR 推算）
            est_3yr = latest_price * math.pow(1 + cagr / 100, 3)

            # 平均 PSF（最新年）
            latest_psf = project_psf[proj].get(latest_yr, 0)

            # 所有年度資料（供前端 sparkline）
            yearly = [{'year': y, 'avg_price': round(year_data[y])} for y in years_sorted]

            results.append({
                'project':      proj,
                'postal':       project_postal.get(proj, ''),
                'earliest_yr':  earliest_yr,
                'latest_yr':    latest_yr,
                'earliest_price': round(earliest_price),
                'latest_price': round(latest_price),
                'latest_psf':   round(latest_psf, 1),
                'cagr':         round(cagr, 2),          # 年均漲幅 %
                'est_3yr':      round(est_3yr),          # 3年後預估價
                'n_years':      n_years,
                'yearly':       yearly
            })

        # 按 cagr 降序排序
        results.sort(key=lambda x: x['cagr'], reverse=True)

        return jsonify({
            'status':  'success',
            'count':   len(results),
            'data':    results[:limit]
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"❌ [get_ura_price_trend ERROR] {type(e).__name__}: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500
