import re

with open('backend/landlord_api.py', 'r', encoding='utf-8') as f:
    api = f.read()

m = re.search(r'# ─── HDB 组屋.*?except Exception:[^\n]*\n\s*pass', api, re.DOTALL)
if m:
    new_com = '''        # ─── 商办 / 店面 ────────────────────────────────────────
        if data_type in ('commercial', 'all'):
            year_filter = ""
            if year != 'all':
                year_filter = f"AND contract_date LIKE '%{year}%'"

            if metric == 'count':
                weight_expr = "COUNT(*)"
            elif metric == 'psf':
                weight_expr = "AVG(psf_sgd)"
            else:
                weight_expr = "AVG(price_sgd)"

            com_query = f"""
                SELECT 
                    c.lat,
                    c.lng,
                    t.project_name AS label,
                    {weight_expr} AS weight
                FROM ura_commercial_transactions t
                JOIN ura_coordinates c ON (t.project_name = c.project OR t.street_name = c.street)
                WHERE c.lat IS NOT NULL {year_filter}
                GROUP BY t.project_name
                HAVING weight > 0
            """
            try:
                cursor.execute(com_query)
                for row in cursor.fetchall():
                    results.append({
                        'lat':    row['lat'],
                        'lng':    row['lng'],
                        'label':  row['label'] + " (商办/店面)",
                        'weight': float(row['weight']) if row['weight'] else 0,
                        'source': 'commercial'
                    })
            except Exception:
                pass'''
    api = api[:m.start()] + new_com + api[m.end():]
    with open('backend/landlord_api.py', 'w', encoding='utf-8') as f2:
        f2.write(api)
    print('Successfully replaced HDB block with Commercial block')
else:
    print('Regex did not match HDB block')
