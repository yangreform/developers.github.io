import requests
url = 'https://html.duckduckgo.com/html/'
headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
data = {'q': '"1 LOFT" developer singapore condo'}
res = requests.post(url, headers=headers, data=data, timeout=10)
print(res.status_code)
print(res.text[:300])
