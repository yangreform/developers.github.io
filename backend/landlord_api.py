
META_TOKEN = "EAAShf2WkavcBRLSa37S2IUHoSDoseKff2pI2k2OU3ZBkZCjrL45CwUxYquK7ZBf3cr1B5ektXFrwLow8tZB86PDHk2KGhq2O2ql9VRXQRlwDrV3NcB5eBGuZAwrA6tmEL8VoHAqDA7jOnA0arS8rxB9VfcignIrmo1fOkeVmD6MxnqS7QEB4NdMXi2hhRzwZDZD"
PHONE_NUMBER_ID = "1124517050733935"

# 🌟 凶手就是少了这一段！请把它补在设定区的下方、路由的上方：
AGENT_NUMBERS = [
    #'6580789177',
    '6580885201'
]


from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import datetime
import requests  # 🌟 汇入 requests 准备呼叫 Meta API
import random


import os
# 🌟 锁死绝对路径：抓取 landlord_api.py 所在的资料夹
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "landlord_sg.db")
print(f"📂 目前 API 绑定的资料库路径为：{DB_NAME}")

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# ── 全域 CORS Header（确保 null origin / file:// / GitHub Pages 都能存取）──
@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin']  = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-User-Phone, ngrok-skip-browser-warning'
    return response

@app.before_request
def handle_options():
    """全域处理 OPTIONS preflight request"""
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
    # 建立使用者表：记录手机号码、查询次数、帐号是否启用
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

# 在程式启动时执行一次
init_user_table()





# ==========================================
# 🌟 1. 资料库初始化 (补回这个超重要的建表功能)
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # 建立 HDB 历史资料表
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
    
    # 建立 客户名单 (Leads) 资料表
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

# 🌟 让这支程式被 router.py 载入时，自动执行一次建表检查！
init_db()


# ==========================================
# 🌟 3. 接收卖房估价名单 API (挂载 Meta WhatsApp 引擎)
# ==========================================
@app.route('/leads', methods=['POST'])  # 🌟 加上 /app 让 Flutter 找得到
def save_lead():
    data = request.get_json()
    if not data or not data.get('phone'):
        return jsonify({"status": "error", "message": "缺少手机号码"}), 400
        
    phone = data.get('phone')
    building = data.get('building', '未知建案')
    price = data.get('price', 0.0)
    created_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 1. 写入 SQLite 资料库
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO leads (created_at, phone, building, price) VALUES (?, ?, ?, ?)",
        (created_at, phone, building, price)
    )
    conn.commit()
    conn.close()
    
    print(f"🎉 [资料库] 收到新名单！电话: {phone}, 建案: {building}, 估价: {price}")
    
    # 2. 🚀 触发 Meta WhatsApp 范本推播 (无视 24 小时限制)
    try:
        headers = {
            "Authorization": f"Bearer {META_TOKEN}",
            "Content-Type": "application/json"
        }
        url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
        
        # 帮金额加上千分位逗号，看起来更专业
        formatted_price = f"{price:,.0f}"
        
        # 回圈发送给名单上的每一位房仲
        for agent in AGENT_NUMBERS:
            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": agent,
                "type": "template",  # 🌟 关键：宣告这是一则范本讯息
                "template": {
                    "name": "new_lead_alert",  # 🌟 必须与您在 Meta 建立的范本名称一模一样
                    "language": {
                        "code": "zh_CN"        # 🌟 必须与您建立范本时选择的语系一致
                    },
                    "components": [
                        {
                            "type": "body",
                            "parameters": [
                                { "type": "text", "text": building },       # 对应 {{1}}
                                { "type": "text", "text": formatted_price },# 对应 {{2}}
                                { "type": "text", "text": phone }          # 对应 {{3}}
                                #{ "type": "text", "text": created_at }      # 对应 {{4}}
                            ]
                        }
                    ]
                }
            }
            
            response = requests.post(url, headers=headers, json=payload)
            result = response.json()
            
            if response.status_code == 200:
                print(f"✅ 成功以【范本模式】推播至 {agent} (讯息ID: {result['messages'][0]['id']})")
            else:
                print(f"❌ 推播至 {agent} 失败，Meta 回传错误: {result}")
            
    except Exception as e:
        print(f"❌ 执行 WhatsApp 发送程式时发生严重错误: {e}")
    
    return jsonify({"status": "success", "message": "名单已储存，推播作业结束"})



# ==========================================
# 🌟 HDB 买卖查询 (终极智慧分词 + 型别强制转换版)
# ==========================================
@app.route('/hdb', methods=['GET'])
def get_hdb_data():
    street = request.args.get('street', '').strip().upper()
    block = request.args.get('block', '').strip().upper()
    
    words = [w for w in street.split() if w]
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row  
    cursor = conn.cursor()
    
    # 🌟 就是这行！必须加上 CAST(block AS TEXT) 才能破解 Pandas 的整数陷阱！
    query = "SELECT * FROM hdb WHERE CAST(block AS TEXT) LIKE ?"
    params = [block + '%']  
    
    for w in words:
        query += " AND street_name LIKE ?"
        params.append('%' + w + '%')
        
    cursor.execute(query, tuple(params))
    records = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    print(f"📊 [HDB买卖] 搜寻 栋号:{block} 街道:{street}，找到 {len(records)} 笔纪录")
    return jsonify({"status": "success", "records": records})

# ==========================================
# 🌟 HDB 租金查询 (终极智慧分词 + 型别强制转换版)
# ==========================================
@app.route('/hdb_rent', methods=['GET'])
def get_hdb_rent_data():
    street = request.args.get('street', '').strip().upper()
    block = request.args.get('block', '').strip().upper()
    
    words = [w for w in street.split() if w]
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row  
    cursor = conn.cursor()
    
    # 🌟 同样的，这里也必须加上 CAST(block AS TEXT)
    query = "SELECT * FROM hdb_rental WHERE CAST(block AS TEXT) LIKE ?"
    params = [block + '%']
    
    for w in words:
        query += " AND street_name LIKE ?"
        params.append('%' + w + '%')
        
    cursor.execute(query, tuple(params))
    records = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    print(f"📊 [HDB租金] 搜寻 栋号:{block} 街道:{street}，找到 {len(records)} 笔纪录")
    return jsonify({"status": "success", "records": records})







# ==========================================
# 🌟 URA 私宅买卖查询 API (极速版)
# ==========================================
@app.route('/ura', methods=['GET'])
def get_ura_data():
    keyword = request.args.get('keyword', '').upper()
    if not keyword:
        return jsonify({"status": "error", "message": "请提供建案或街道名称"}), 400
        
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row  
    cursor = conn.cursor()
    
    # 模糊搜寻建案名称或街道名称
    cursor.execute("SELECT * FROM ura_transactions WHERE project LIKE ? OR street LIKE ?", ('%' + keyword + '%', '%' + keyword + '%'))
    records = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    print(f"📊 [私宅买卖] 搜寻 '{keyword}'，在资料库找到 {len(records)} 笔纪录")
    return jsonify({"status": "success", "total": len(records), "records": records})

# ==========================================
# 🌟 URA 私宅租金查询 API (极速版)
# ==========================================
@app.route('/ura_rent', methods=['GET'])
def get_ura_rent_data():
    keyword = request.args.get('keyword', '').upper()
    if not keyword:
        return jsonify({"status": "error", "message": "请提供建案或街道名称"}), 400
        
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row  
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM ura_rentals WHERE project LIKE ? OR street LIKE ?", ('%' + keyword + '%', '%' + keyword + '%'))
    records = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    print(f"📊 [私宅租金] 搜寻 '{keyword}'，在资料库找到 {len(records)} 笔纪录")
    return jsonify({"status": "success", "total": len(records), "records": records})


@app.route('/heatmap/ura', methods=['GET', 'OPTIONS'])
def get_ura_heatmap():
    if request.method == 'OPTIONS':
        return '', 200

    #print("\n--- [DEBUG] 🚀 进入 /app/heatmap/ura API ---")
    try:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        print("[DEBUG] 1. 资料库连线成功")
    except Exception as e:
        print(f"❌ [ERROR] 1. 资料库连线失败: {e}")
        return jsonify({"status": "error", "message": "DB Connection Error"}), 500

    try:
        # ==========================================
        # 🌟 [功能 1] 计数器
        # ==========================================
        user_phone = request.headers.get('X-User-Phone', '').strip()
        print(f"[DEBUG] 2. 收到 Header 手机号码: '{user_phone}'")
        
        if user_phone and user_phone != "尚未绑定" and user_phone != "":
            safe_phone = ''.join(c for c in user_phone if c.isdigit() or c == '+')
            print(f"[DEBUG] 3. 解析出纯数字手机号码: '{safe_phone}'")
            
            if safe_phone:
                # 🌟 修正点：把 SELECT id 改成 SELECT phone
                cursor.execute("SELECT phone FROM users WHERE phone = ?", (safe_phone,))
                user = cursor.fetchone()
                print(f"[DEBUG] 4. 查询旧用户结果: {'找到老朋友' if user else '这是新朋友'}")

                if user:
                    cursor.execute("UPDATE users SET query_count = query_count + 1 WHERE phone = ?", (safe_phone,))
                    print("[DEBUG] 5. 更新旧用户查询次数成功")
                else:
                    cursor.execute("INSERT INTO users (phone, query_count, is_active) VALUES (?, 1, 1)", (safe_phone,))
                    print("[DEBUG] 5. 新增用户建档成功")
                
                conn.commit() 
                print("[DEBUG] 6. 计数器写入资料库成功")

        # ==========================================
        # 🌟 [功能 2] 热力图 (极速 + 无日期限制版)
        # ==========================================
        #print("[DEBUG] 7. 准备执行热力图 SQL 查询...")
        query = """
            SELECT 
                c.project, c.lat, c.lng, c.postal,
                COUNT(*) as tx_count,
                AVG(t.price) as avg_price,
                AVG(t.price / (t.area * 10.7639)) as avg_psf
            FROM ura_coordinates c
            JOIN ura_transactions t ON c.project = t.project
            -- 🌟 注意：我们把 WHERE contract_date 这一行彻底删除了！
            GROUP BY c.project
            HAVING tx_count > 0
        """
        cursor.execute(query)
        heatmap_rows = cursor.fetchall()
        heatmap_data = [dict(row) for row in heatmap_rows]
        #print(f"[DEBUG] 8. 热力图查询成功！共抓取 {len(heatmap_data)} 笔建案资料")


        cursor.execute(query)
        heatmap_rows = cursor.fetchall()
        heatmap_data = [dict(row) for row in heatmap_rows]
        #print(f"[DEBUG] 8. 热力图查询成功！共抓取 {len(heatmap_data)} 笔建案资料")

        # ==========================================
        # 🌟 [功能 3] 强推广告
        # ==========================================
        #print("[DEBUG] 9. 准备执行强推建案 SQL 查询...")
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

            # 3. 查询并列印资料
            if not promo_row:
                print("⚠️ 资料表是空的，没有任何资料。")
            else:
                print(f"✅ 找到 {len(promo_row)} 笔资料：")
                print("-" * 50)
                for row in promo_row:
                    print(row)
                print("-" * 50)


        except Exception as e:
            print(f"⚠️ [DEBUG] 10. (警告) 强推建案查询失败 (可忽略): {e}")

        # ==========================================
        # 🌟 [功能 4] 完美打包回传给 Flutter
        # ==========================================
        # 💡 您可以随时在这里切换 True 或 False
        # (未来甚至可以把这个值写进 SQL 的系统设定表里)
        #is_otp_required = True 
        is_otp_required = False

        return jsonify({
            "status": "success",
            "data": {
                "heatmap": heatmap_data,
                "promo": promo_data,
                "require_otp": is_otp_required  # 🌟 新增：将开关状态传给手机
            }
        })
        
    except Exception as e:
        conn.rollback()
        # 🚨 这里是最关键的错误印出点！
        print(f"\n💥 [FATAL ERROR] 程式发生中断错误！")
        print(f"💥 错误类型: {type(e).__name__}")
        print(f"💥 错误原因: {str(e)}\n")
        return jsonify({"status": "error", "message": str(e)}), 500
        
    finally:
        print("[DEBUG] 12. 关闭资料库连线")
        print("--- [DEBUG] 🏁 API 执行结束 ---\n")
        conn.close()



# 🌟 暂时用来储存 OTP 的记忆体字典 (实务上可放资料库)
otp_storage = {}

# ==========================================
# 1. 发送 WhatsApp OTP API
# ==========================================
@app.route('/otp/send', methods=['POST', 'OPTIONS'])
def send_otp():
    if request.method == 'OPTIONS':
        return jsonify({"status": "success"}), 200

    data = request.json
    phone = data.get('phone') # 格式需为 6588888888 (去掉 + 号)

    # 产生 4 位数随机密码
    otp_code = str(random.randint(1000, 9999))
    otp_storage[phone] = otp_code
    print(f"🔑 产生 OTP 给 {phone}: {otp_code}")

    # 呼叫 Meta API 发射！
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
            "name": "sn1", # 🌟 这里填写您刚通过审核的范本名称
            "language": {"code": "en"}, # 🌟 依照您申请时的语言填写 (例如 en_US)
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": otp_code}] # 把 4 位数塞进 {{1}}
                },
            ]
        }
    }

    res = requests.post(url, headers=headers, json=payload)
    print(f"📡 Meta 回传结果: {res.text}")

    if res.status_code == 200:
        return jsonify({"status": "success"})
    else:
        return jsonify({"status": "error", "message": "WhatsApp 发送失败"}), 500

# ==========================================
# 2. 核对 OTP API
# ==========================================
@app.route('/otp/verify', methods=['POST', 'OPTIONS'])
def verify_otp():
    if request.method == 'OPTIONS':
        return jsonify({"status": "success"}), 200

    data = request.json
    phone = data.get('phone')
    user_code = data.get('code')

    # 核对密码
    real_code = otp_storage.get(phone)
    if real_code and real_code == user_code:
        # 🌟 验证成功！将使用者加入资料库 (如果已经存在就不理他)
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (phone) VALUES (?)", (phone,))
        conn.commit()
        conn.close()
        
        return jsonify({"status": "success"})

def record_user_query(phone):
    """记录使用者的查询次数，并检查帐号是否被停用"""
    if not phone: return True # 如果没传电话 (可能是未登入体验)，暂时放行
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # 检查该使用者状态
    cursor.execute("SELECT is_active FROM users WHERE phone = ?", (phone,))
    user = cursor.fetchone()
    
    if user:
        if user['is_active'] == 0:
            conn.close()
            return False # 🚨 帐号已被停用
        
        # 查询次数 + 1
        cursor.execute("UPDATE users SET query_count = query_count + 1 WHERE phone = ?", (phone,))
        conn.commit()
        
    conn.close()
    return True


# ==========================================
# 🔥 热力图通用 API — 前端 heatmap.html 使用
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
            # 日期筛选条件：contractDate 格式为 "YYMM" e.g. "2301" = 2023年1月
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

        # ─── HDB 组屋（使用 ura_coordinates 里最近的座标） ──
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

            # 用 town 关联 ura_coordinates 的 street 栏位取近似座标
            # 更精准做法：用 town 中心点（预先计算好）；此处用 GROUP BY town 搭配已知座标
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

            # HDB 市镇中心座标（新加坡 26 个市镇）
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

        # 正规化权重到 0~1 方便前端调整半径
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
# 🔥 HDB 热力图独立 API（依市镇统计）
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
# 📈 URA 私宅楼盘 — 年均涨幅 & 3年预估价 API
# GET /api/ura_price_trend
# 可选 query param: limit (default 200)
# ==========================================
@app.route('/api/ura_price_trend', methods=['GET', 'OPTIONS'])
def get_ura_price_trend():
    limit = request.args.get('limit', 200, type=int)
    limit = min(max(limit, 1), 1000)   # clamp 1~1000

    try:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Step 1: 取每个楼盘各年度的平均成交价
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
            HAVING tx_count >= 1
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

        # Step 3: 计算每个楼盘的 CAGR 及 3年预估价
        import datetime
        CURRENT_YEAR = datetime.date.today().year
        CAGR_MIN = -30.0      # 过滤异常（整幢收购/清盘等极端值）
        CAGR_MAX = +40.0
        results = []

        for proj, year_data in project_years.items():
            years_sorted = sorted(year_data.keys())
            if len(years_sorted) < 2:
                continue

            # 取最早年和最晚年（排除 2026 部分年资料）
            years_full = [y for y in years_sorted if y <= CURRENT_YEAR]
            if len(years_full) < 2:
                years_full = years_sorted   # 若只有 2026 资料，仍计算

            earliest_yr    = years_full[0]
            actual_latest_yr = years_full[-1]
            earliest_price = year_data[earliest_yr]
            latest_price   = year_data[actual_latest_yr]

            # 依据使用者需求，不管最新交易是哪一年，永远将结束年份设为今年 (CURRENT_YEAR)
            latest_yr = CURRENT_YEAR
            n_years = latest_yr - earliest_yr
            if n_years <= 0 or earliest_price <= 0:
                continue

            # CAGR = (latest/earliest)^(1/n) - 1
            import math
            cagr = (math.pow(latest_price / earliest_price, 1.0 / n_years) - 1) * 100

            # 过滤异常值（例如整幢收购、清盘等极端交易）
            if cagr < CAGR_MIN or cagr > CAGR_MAX:
                continue
            # 资料跨度太短（只有1年）不够准确
            if n_years < 1:
                continue

            # 3年后预估价（以 latest_price 为起点，用 CAGR 推算）
            est_3yr = latest_price * math.pow(1 + cagr / 100, 3)

            # 平均 PSF（最新交易年）
            latest_psf = project_psf[proj].get(actual_latest_yr, 0)

            # 所有年度资料（供前端 sparkline）
            yearly = [{'year': y, 'avg_price': round(year_data[y])} for y in years_sorted]

            results.append({
                'project':      proj,
                'postal':       project_postal.get(proj, ''),
                'earliest_yr':  earliest_yr,
                'latest_yr':    latest_yr,
                'earliest_price': round(earliest_price),
                'latest_price': round(latest_price),
                'latest_psf':   round(latest_psf, 1),
                'cagr':         round(cagr, 2),          # 年均涨幅 %
                'est_3yr':      round(est_3yr),          # 3年后预估价
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
