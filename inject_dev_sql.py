import re

def modify_api():
    filepath = 'backend/landlord_api.py'
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # 1. Modify the SQL to LEFT JOIN ura_developers
    old_sql = """            SELECT
                t.project,
                c.postal,
                CAST('20' || SUBSTR(t.contractDate,3,2) AS INTEGER) AS year,
                AVG(t.price) AS avg_price,
                AVG(t.price / NULLIF(t.area * 10.7639, 0)) AS avg_psf,
                COUNT(*) AS tx_count
            FROM ura_transactions t
            JOIN ura_coordinates c ON t.project = c.project
            WHERE t.price > 0"""
            
    new_sql = """            SELECT
                t.project,
                c.postal,
                d.developer_name,
                CAST('20' || SUBSTR(t.contractDate,3,2) AS INTEGER) AS year,
                AVG(t.price) AS avg_price,
                AVG(t.price / NULLIF(t.area * 10.7639, 0)) AS avg_psf,
                COUNT(*) AS tx_count
            FROM ura_transactions t
            JOIN ura_coordinates c ON t.project = c.project
            LEFT JOIN ura_developers d ON t.project = d.project
            WHERE t.price > 0"""
            
    content = content.replace(old_sql, new_sql)

    # 2. Add project_developer dictionary
    content = content.replace("project_postal = {}", "project_postal = {}\n        project_developer = {}")
    
    # 3. Save developer name in loop
    old_loop = """        for r in rows:
            proj = r['project']
            yr   = r['year']
            project_years[proj][yr]  = r['avg_price']
            project_psf[proj][yr]    = r['avg_psf'] if r['avg_psf'] else 0
            project_postal[proj]     = r['postal']"""
            
    new_loop = """        for r in rows:
            proj = r['project']
            yr   = r['year']
            project_years[proj][yr]  = r['avg_price']
            project_psf[proj][yr]    = r['avg_psf'] if r['avg_psf'] else 0
            project_postal[proj]     = r['postal']
            project_developer[proj]  = r['developer_name'] if r['developer_name'] else '—'"""
            
    content = content.replace(old_loop, new_loop)

    # 4. Return developer in results JSON
    old_result = """            results.append({
                'project':      proj,
                'postal':       project_postal.get(proj, ''),"""
                
    new_result = """            results.append({
                'project':      proj,
                'postal':       project_postal.get(proj, ''),
                'developer':    project_developer.get(proj, '—'),"""
                
    content = content.replace(old_result, new_result)
    
    # 5. Fix try/except block just in case table ura_developers doesn't exist yet before update_db.py runs
    # Actually, we should just ensure ura_developers exists at the start of get_ura_price_trend
    old_conn = """        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()"""
    new_conn = """        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Ensure developers table exists so query doesn't crash before first sync
        cursor.execute("CREATE TABLE IF NOT EXISTS ura_developers (project TEXT PRIMARY KEY, developer_name TEXT)")
"""
    content = content.replace(old_conn, new_conn)

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    print("Fixed landlord_api.py")

if __name__ == '__main__':
    modify_api()
