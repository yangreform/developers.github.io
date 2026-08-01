import requests
url = 'https://lite.duckduckgo.com/lite/'
headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
data = {'q': 'site:propertyguru.com.sg/project/ "THREE BALMORAL"'}
res = requests.post(url, headers=headers, data=data)
print('Status:', res.status_code)
print(res.text[:500])
