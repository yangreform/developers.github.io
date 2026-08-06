import re
with open('backend/landlord_api.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Remove /hdb
content = re.sub(r"@app\.route\('/hdb'.*?def get_hdb_data\(\):.*?conn\.close\(\)\s*return jsonify\(results\)\s*except Exception as e:.*?return jsonify\(\{.*?\}\), 500", '', content, flags=re.DOTALL)

# Remove /hdb_rent
content = re.sub(r"@app\.route\('/hdb_rent'.*?def get_hdb_rent_data\(\):.*?conn\.close\(\)\s*return jsonify\(results\)\s*except Exception as e:.*?return jsonify\(\{.*?\}\), 500", '', content, flags=re.DOTALL)

# Remove /heatmap/hdb
content = re.sub(r"@app\.route\('/heatmap/hdb'.*?def get_hdb_heatmap\(\):.*?return jsonify\(\{.*?\}\), 500", '', content, flags=re.DOTALL)

# Add /api/developer_stats at the end
dev_stats_code = '''
# ==========================================
# 📈 私宅：依開發商 (Developer Stats)
# GET /api/developer_stats
# ==========================================
@app.route('/api/developer_stats', methods=['GET', 'OPTIONS'])
def get_developer_stats():
    if request.method == 'OPTIONS':
        return '', 200

    try:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Ensure developers table exists
        cursor.execute("CREATE TABLE IF NOT EXISTS ura_developers (project TEXT PRIMARY KEY, developer_name TEXT)")

        # Step 1: Calculate average price per year for each project
        cursor.execute("""
            SELECT
                t.project,
                d.developer_name,
                CAST('20' || SUBSTR(t.contractDate,3,2) AS INTEGER) AS year,
                AVG(t.price) AS avg_price,
                COUNT(*) AS tx_count
            FROM ura_transactions t
            LEFT JOIN ura_developers d ON t.project = d.project
            WHERE t.price > 0
              AND LENGTH(t.contractDate) = 4
            GROUP BY t.project, year
            HAVING tx_count >= 1
            ORDER BY t.project, year
        """)
        rows = cursor.fetchall()
        conn.close()

        from collections import defaultdict
        import math
        import datetime
        CURRENT_YEAR = datetime.date.today().year

        project_years = defaultdict(dict)
        project_developer = {}

        for r in rows:
            proj = r['project']
            yr = r['year']
            project_years[proj][yr] = r['avg_price']
            project_developer[proj] = r['developer_name'] if r['developer_name'] else '—'

        developer_stats = defaultdict(lambda: {'projects': set(), 'cagrs': [], 'latest_prices': []})

        for proj, year_data in project_years.items():
            dev = project_developer[proj]
            if dev == '—' or dev.startswith('Unknown'):
                continue
                
            years_sorted = sorted(year_data.keys())
            if len(years_sorted) < 2:
                continue

            years_full = [y for y in years_sorted if y <= CURRENT_YEAR]
            if len(years_full) < 2:
                years_full = years_sorted

            earliest_yr = years_full[0]
            latest_price = year_data[years_full[-1]]
            earliest_price = year_data[earliest_yr]
            n_years = CURRENT_YEAR - earliest_yr
            
            if n_years <= 0 or earliest_price <= 0:
                continue

            cagr = (math.pow(latest_price / earliest_price, 1.0 / n_years) - 1) * 100
            if cagr < -30.0 or cagr > 40.0 or n_years < 1:
                continue

            developer_stats[dev]['projects'].add(proj)
            developer_stats[dev]['cagrs'].append(cagr)
            developer_stats[dev]['latest_prices'].append(latest_price)

        results = []
        for dev, stats in developer_stats.items():
            if len(stats['projects']) > 0:
                avg_cagr = sum(stats['cagrs']) / len(stats['cagrs'])
                avg_price = sum(stats['latest_prices']) / len(stats['latest_prices'])
                results.append({
                    'developer': dev,
                    'total_projects': len(stats['projects']),
                    'avg_cagr': round(avg_cagr, 2),
                    'avg_price': round(avg_price)
                })

        # Sort by total_projects descending by default
        results.sort(key=lambda x: x['total_projects'], reverse=True)

        return jsonify({
            'status': 'success',
            'count': len(results),
            'data': results
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500
'''

content += '\n' + dev_stats_code

with open('backend/landlord_api.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('Success')
