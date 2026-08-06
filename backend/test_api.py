import urllib.request
import json

def test_endpoint(url):
    try:
        resp = urllib.request.urlopen(url)
        data = json.loads(resp.read().decode('utf-8'))
        print(f"{url} -> status: {data.get('status', 'unknown')}, len: {len(data.get('data', []))}")
        if data.get('status') == 'error':
            print('Error:', data.get('message'))
    except Exception as e:
        print(f"Failed to fetch {url}: {e}")

test_endpoint('http://localhost:5000/app/api/ura_price_trend')
test_endpoint('http://localhost:5000/app/api/get_heatmap_data?type=ura')
test_endpoint('http://localhost:5000/app/api/get_heatmap_data?type=commercial')
