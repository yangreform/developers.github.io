
# router.py
import requests
from flask import Flask, request, jsonify, Response
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from waitress import serve

# 1. 匯入處理房產新功能的 Flask App
# ── 修正 import：backend/ 在 trade/ 的上一層目錄 ──────────────
import sys, os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
NGROK_URL         = os.getenv('NGROK_URL', 'http://127.0.0.1:5000')
LANDLORD_API_BASE = os.getenv('LANDLORD_API_BASE', NGROK_URL + '/app')

_backend_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'backend'))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from landlord_api import app as landlord_app

# 2. 建立一個全新的「網關 App」取代原本的 main_app
gateway_app = Flask(__name__)

@gateway_app.route('/webhook', methods=['POST'])
def proxy_webhook():
    """ 
    總機轉發邏輯：
    收到 webhook 訊號後，立刻丟給獨立跑在 5500 port 的 main.py。
    """
    try:
        # 將收到的 JSON 與 Headers 原封不動轉發給 5500
        resp = requests.post(
            "http://127.0.0.1:5500/webhook", 
            json=request.json, 
            headers={key: value for (key, value) in request.headers if key != 'Host'},
            timeout=15  # 設定超時，避免卡死
        )
        # 把交易核心的回傳結果，原樣回傳給 Ngrok/發送方
        return Response(resp.content, status=resp.status_code, content_type=resp.headers.get('Content-Type'))
        
    except requests.exceptions.ConnectionError:
        # 🌟 完美防護機制：如果剛好遇到 main.py 正在重啟 (5500 連不上)
        print("⚠️ 警告: 收到 webhook，但交易核心 (Port 5500) 正在重啟中！")
        return jsonify({
            'status': 'error', 
            'message': '交易伺服器目前正在維護/重啟中，請稍後再試'
        }), 503


# 🌟 手機監控面板轉發：/dashboard, /api/* -> 127.0.0.1:5800 (q.py 內建的 Flask)
# 這樣就能透過同一條 ngrok tunnel (port 5000) 存取，不需要額外開通 port 5800
DASHBOARD_UPSTREAM = "http://127.0.0.1:5800"

@gateway_app.route('/dashboard', methods=['GET'])
def proxy_dashboard():
    try:
        resp = requests.get(f"{DASHBOARD_UPSTREAM}/dashboard", timeout=10)
        return Response(resp.content, status=resp.status_code, content_type=resp.headers.get('Content-Type'))
    except requests.exceptions.ConnectionError:
        return jsonify({'status': 'error', 'message': '監控面板 (Port 5800) 尚未啟動或正在重啟中'}), 503


@gateway_app.route('/api/<path:subpath>', methods=['GET', 'POST'])
def proxy_dashboard_api(subpath):
    try:
        upstream_url = f"{DASHBOARD_UPSTREAM}/api/{subpath}"
        if request.method == 'POST':
            resp = requests.post(upstream_url, json=request.get_json(silent=True), timeout=10)
        else:
            resp = requests.get(upstream_url, timeout=10)
        return Response(resp.content, status=resp.status_code, content_type=resp.headers.get('Content-Type'))
    except requests.exceptions.ConnectionError:
        return jsonify({'status': 'error', 'message': '監控面板 (Port 5800) 尚未啟動或正在重啟中'}), 503

# ── 健康檢查 & 根路由（避免 /  /app 直接打出 404）──────────────
@gateway_app.route('/', methods=['GET'])
def root_health():
    return jsonify({
        'status': 'ok',
        'service': 'LandlordSG API Gateway',
        'routes': {
            '/app/heatmap/ura':          'URA 私宅熱力圖',
            '/app/heatmap/hdb':          'HDB 組屋熱力圖',
            '/app/api/get_heatmap_data': '通用熱力圖（支援 type/metric/year）',
            '/app/hdb':                  'HDB 交易查詢',
            '/app/ura':                  'URA 私宅查詢',
            '/webhook':                  '交易系統 Webhook (proxy->5500)',
            '/dashboard':               '儀表板 (proxy->5800)',
        }
    }), 200

@gateway_app.route('/app', methods=['GET'])
@gateway_app.route('/app/', methods=['GET'])
def app_redirect():
    """
    /app 或 /app/ 單獨訪問時，DispatcherMiddleware 不會轉發。
    這裡直接回傳路由說明，讓前端/測試者知道 API 正常運作。
    """
    return jsonify({
        'status': 'ok',
        'message': 'LandlordSG 房產 API 正常運作',
        'endpoints': [
            'GET /app/heatmap/ura',
            'GET /app/heatmap/hdb',
            'GET /app/api/get_heatmap_data?type=ura&metric=price&year=all',
            'GET /app/hdb?block=xxx&street=xxx',
            'GET /app/ura?keyword=xxx',
            'POST /app/leads',
            'POST /app/otp/send',
        ]
    }), 200
# 3. 設定分流規則
application = DispatcherMiddleware(
    gateway_app,  # 🌟 預設主機：/webhook 會進到上面的 proxy_webhook 幫忙轉發
    {
        '/app': landlord_app  # 🌟 虛擬目錄：網址開頭是 /app/ 的，依然直接由 landlord_app 處理
    }
)

if __name__ == '__main__':
    print("=" * 60)
    print(" 雙核心 API 網關已啟動於 Port 5000")
    print("=" * 60)
    print(f" [房產 API]  本機: http://127.0.0.1:5000/app")
    print(f" [房產 API]  公網: {LANDLORD_API_BASE}")
    print(f" [Webhook]   轉發: http://127.0.0.1:5000/webhook -> Port 5500")
    print(f" [儀表板]    轉發: http://127.0.0.1:5000/dashboard -> Port 5800")
    print(" heatmap.html 的 API_BASE 請設定為:")
    print(f"   {LANDLORD_API_BASE}")
    print("=" * 60)
    serve(application, host='0.0.0.0', port=5000, threads=6)