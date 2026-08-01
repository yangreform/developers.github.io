import opencc
import os

converter = opencc.OpenCC('t2s')

files_to_translate = [
    'docs/index.html',
    'docs/heatmap.html',
    'docs/about.html',
    'backend/landlord_api.py'
]

for filepath in files_to_translate:
    if not os.path.exists(filepath):
        print(f"Skipping {filepath} (not found)")
        continue
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    translated = converter.convert(content)
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(translated)
        
    print(f"Translated {filepath}")
