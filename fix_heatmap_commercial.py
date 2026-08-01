import re

# 1. Update docs/heatmap.html
with open('docs/heatmap.html', 'r', encoding='utf-8') as f:
    html = f.read()

html = html.replace('<button class="btn-pill type-hdb" data-type="hdb">&#127968; HDB 组屋</button>', 
                    '<button class="btn-pill type-commercial" data-type="commercial">&#127980; 商办 / 店面</button>')
html = html.replace('.btn-pill.type-hdb.active{background:var(--tag-hdb);color:#f6ad55;border-color:rgba(246,173,85,0.5)}',
                    '.btn-pill.type-commercial.active{background:var(--tag-hdb);color:#f6ad55;border-color:rgba(246,173,85,0.5)}')
html = html.replace("const typeBtns = ['ura', 'hdb', 'all'];", "const typeBtns = ['ura', 'commercial', 'all'];")
html = html.replace(".stat-val.hdb{color:#f6ad55}", ".stat-val.commercial{color:#f6ad55}")
html = html.replace(".badge-hdb{background:var(--tag-hdb);color:#f6ad55}", ".badge-commercial{background:var(--tag-hdb);color:#f6ad55}")

with open('docs/heatmap.html', 'w', encoding='utf-8') as f:
    f.write(html)

print("Updated heatmap.html UI")

# 2. Update backend/landlord_api.py
with open('backend/landlord_api.py', 'r', encoding='utf-8') as f:
    api = f.read()

old_hdb = """        # ─── HDB 组屋（使用 ura_coordinates 里最近的座标） ──
        if data_type in ('hdb', 'all'):
            # HDB month 格式 "YYYY-MM"
            year_filter_hdb = ""
            if year != 'all':
                try:
                    year_filter_hdb = f"AND h.month LIKE '{year}%'"
                except Exception:
                    pass

            if metric == 'count':
                hdb_weight_expr = "COUNT(*)"
            elif metric == 'psf':
                hdb_weight_expr = "AVG(h.resale_price / NULLIF(h.floor_area_sqm * 10.7639, 0))"
            else:
                hdb_weight_expr = "AVG(h.resale_price)"

            # 用 town 关联 ura_coordinates 的 street 栏位取近似座标
            # 更精准做法：用 town 中心点（预先计算好）；此处用 GROUP BY town 搭配已知座标
            hdb_query = f\"\"\"
                SELECT
                    c.lat,
                    c.lng,
                    h.town AS label,
                    {hdb_weight_expr} AS weight
                FROM hdb_transactions h
                JOIN ura_coordinates c ON c.street LIKE h.town || '%'
                WHERE c.lat IS NOT NULL {year_filter_hdb}
                GROUP BY h.town
                HAVING weight > 0
            \"\"\"
            try:
                cursor.execute(hdb_query)
                for row in cursor.fetchall():
                    results.append({
                        'lat':    row['lat'],
                        'lng':    row['lng'],
                        'label':  row['label'] + " (HDB)",
                        'weight': float(row['weight']) if row['weight'] else 0,
                        'source': 'hdb'
                    })
            except Exception:
                pass"""

new_com = """        # ─── 商办 / 店面 ────────────────────────────────────────
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

            com_query = f\"\"\"
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
            \"\"\"
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
            except Exception as e:
                pass"""

if 'def get_heatmap_data' in api and old_hdb in api:
    api = api.replace(old_hdb, new_com)
    with open('backend/landlord_api.py', 'w', encoding='utf-8') as f:
        f.write(api)
    print("Updated get_heatmap_data in landlord_api.py to support commercial instead of HDB")
else:
    print("Could not find HDB block in landlord_api.py to replace")
