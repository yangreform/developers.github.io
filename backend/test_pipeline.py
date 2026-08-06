from googlesearch import search
import requests
from bs4 import BeautifulSoup
import re

def get_developer(project_name):
    query = f'site:propertyguru.com.sg/project "{project_name}"'
    urls = list(search(query, num_results=3, lang="en"))
    pg_url = None
    for u in urls:
        if '/project/' in u and re.search(r'\-\d+$', u):
            pg_url = u
            break
            
    if not pg_url:
        print(f"Could not find PropertyGuru URL for {project_name}")
        return None
        
    print(f"Found URL: {pg_url}")
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        res = requests.get(pg_url, headers=headers, timeout=10)
        if not res.ok:
            print("PropertyGuru returned:", res.status_code)
            return None
            
        soup = BeautifulSoup(res.text, 'html.parser')
        
        # Look for Developer info in PropertyGuru
        # Usually it's in a td or span next to 'Developer'
        text_nodes = soup.find_all(text=re.compile(r'Developer', re.I))
        for node in text_nodes:
            parent = node.parent
            if parent:
                # Sometimes it's the next sibling or in the same container
                # Let's just dump the text around it
                container = parent.parent
                if container:
                    full_text = container.get_text(strip=True)
                    m = re.search(r'Developer[:\s]*([A-Za-z0-9\s]+?(?:Pte Ltd|Ltd|Limited|Group|Corp))', full_text, re.I)
                    if m:
                        return m.group(1).strip()
        
        # fallback if regex fails but we have the table
        rows = soup.find_all('tr')
        for row in rows:
            if 'Developer' in row.get_text():
                cols = row.find_all('td')
                if len(cols) >= 2:
                    return cols[1].get_text(strip=True)
                    
    except Exception as e:
        print("Error fetching:", e)
        
    return None

print(get_developer('THREE BALMORAL'))
print(get_developer('REGENCY SUITES'))
