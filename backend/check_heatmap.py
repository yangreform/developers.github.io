import re
with open('docs/heatmap.html', 'r', encoding='utf-8') as f:
    text = f.read()
    print('Length:', len(text))
    endpoints = re.findall(r'fetch\([\'\"](.*?)[\'\"]', text)
    print('Endpoints:', endpoints)
