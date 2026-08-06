import os
import shutil
import glob
import re

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(ROOT_DIR, 'docs')
BACKEND_DIR = os.path.join(ROOT_DIR, 'backend')

print("Starting reorganization...")
print(f"Root: {ROOT_DIR}")
print(f"Docs: {DOCS_DIR}")
print(f"Backend: {BACKEND_DIR}")

# 1. Move all root .py files and test scripts into backend/
root_py_files = [f for f in os.listdir(ROOT_DIR) if f.endswith('.py') and f != 'organize_project.py']
for f in root_py_files:
    src = os.path.join(ROOT_DIR, f)
    dst = os.path.join(BACKEND_DIR, f)
    print(f"Moving PY: {f} -> backend/{f}")
    shutil.move(src, dst)

# Move test/script txt files and swap files
for f in os.listdir(ROOT_DIR):
    if f.startswith('script') and f.endswith('.txt'):
        src = os.path.join(ROOT_DIR, f)
        dst = os.path.join(BACKEND_DIR, f)
        print(f"Moving script: {f} -> backend/{f}")
        shutil.move(src, dst)
    elif f.endswith('.un~'):
        src = os.path.join(ROOT_DIR, f)
        dst = os.path.join(BACKEND_DIR, f)
        print(f"Moving swap file: {f} -> backend/{f}")
        shutil.move(src, dst)

# Check landlord_sg.db in root vs backend
root_db = os.path.join(ROOT_DIR, 'landlord_sg.db')
backend_db = os.path.join(BACKEND_DIR, 'landlord_sg.db')
if os.path.exists(root_db):
    if os.path.getsize(root_db) == 0:
        print("Removing empty root landlord_sg.db (0 bytes)")
        os.remove(root_db)
    elif not os.path.exists(backend_db) or os.path.getsize(backend_db) == 0:
        print("Moving root landlord_sg.db -> backend/landlord_sg.db")
        shutil.move(root_db, backend_db)
    else:
        print("Root landlord_sg.db exists, backing up to backend/landlord_sg_root.db")
        shutil.move(root_db, os.path.join(BACKEND_DIR, 'landlord_sg_root.db'))

# 2. Move everything from docs/ up one level to root
if os.path.exists(DOCS_DIR):
    docs_items = os.listdir(DOCS_DIR)
    for item in docs_items:
        src = os.path.join(DOCS_DIR, item)
        dst = os.path.join(ROOT_DIR, item)
        print(f"Moving from docs: {item} -> root/{item}")
        if os.path.exists(dst):
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            else:
                os.remove(dst)
        shutil.move(src, dst)
    
    # Check if docs is empty and remove or keep
    try:
        os.rmdir(DOCS_DIR)
        print("Removed empty docs/ directory")
    except Exception as e:
        print(f"Docs directory not empty or could not be removed: {e}")

# 3. Update sitemap.xml in root
sitemap_content = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://developers.marketing/</loc>
    <changefreq>daily</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>https://developers.marketing/LandlordSG.html</loc>
    <changefreq>daily</changefreq>
    <priority>0.9</priority>
  </url>
  <url>
    <loc>https://developers.marketing/heatmap.html</loc>
    <changefreq>daily</changefreq>
    <priority>0.9</priority>
  </url>
  <url>
    <loc>https://developers.marketing/thomson-reserve/</loc>
    <changefreq>weekly</changefreq>
    <priority>0.8</priority>
  </url>
  <url>
    <loc>https://developers.marketing/about.html</loc>
    <changefreq>monthly</changefreq>
    <priority>0.5</priority>
  </url>
  <url>
    <loc>https://developers.marketing/contact.html</loc>
    <changefreq>monthly</changefreq>
    <priority>0.5</priority>
  </url>
</urlset>
"""
with open(os.path.join(ROOT_DIR, 'sitemap.xml'), 'w', encoding='utf-8') as f:
    f.write(sitemap_content)
print("Updated sitemap.xml")

# 4. Update robots.txt in root
robots_content = """User-agent: *
Allow: /

Sitemap: https://developers.marketing/sitemap.xml
"""
with open(os.path.join(ROOT_DIR, 'robots.txt'), 'w', encoding='utf-8') as f:
    f.write(robots_content)
print("Updated robots.txt")

# 5. Update canonical URLs & structured data in LandlordSG.html and heatmap.html to developers.marketing
for fname in ['LandlordSG.html', 'heatmap.html']:
    fpath = os.path.join(ROOT_DIR, fname)
    if os.path.exists(fpath):
        with open(fpath, 'r', encoding='utf-8') as f:
            content = f.read()
        content = content.replace('https://yangreform.github.io/developers.github.io/', 'https://developers.marketing/')
        content = content.replace('https://profjacky.github.io/heatmap.html', 'https://developers.marketing/heatmap.html')
        content = content.replace('https://profjacky.github.io/', 'https://developers.marketing/')
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"Updated canonical and domain URLs in {fname}")

print("Reorganization complete!")
