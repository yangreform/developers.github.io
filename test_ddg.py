import requests
from html.parser import HTMLParser
import re

class DDGParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
    def handle_data(self, data):
        self.text.append(data)

def search_developer(project_name):
    url = 'https://html.duckduckgo.com/html/'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    data = {'q': f'"{project_name}" developer singapore condo'}
    res = requests.post(url, headers=headers, data=data)
    if res.ok:
        parser = DDGParser()
        parser.feed(res.text)
        full_text = ' '.join(parser.text).replace('\\n', ' ')
        
        m = re.search(r'(?i)developed by ([A-Za-z0-9 ]+?)(?:Pte Ltd|Ltd|Limited|Group|Corp|Developer|\\.|,|-)', full_text)
        if m: return m.group(1).strip() + ' Pte Ltd'
        
        m2 = re.search(r'(?i)developer:?\s*([A-Za-z0-9 ]+?)(?:Pte Ltd|Ltd|Limited|Group|Corp|\\.|,|-)', full_text)
        if m2: return m2.group(1).strip() + ' Pte Ltd'
    return None

print('THREE BALMORAL:', search_developer('THREE BALMORAL'))
print('REGENCY SUITES:', search_developer('REGENCY SUITES'))
print('URBAN EDGE @ HOLLAND V:', search_developer('URBAN EDGE @ HOLLAND V'))
