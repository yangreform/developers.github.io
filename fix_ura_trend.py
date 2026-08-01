import re

with open('backend/landlord_api.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Remove limit clamping
content = content.replace("limit = min(max(limit, 1), 1000)", "limit = min(max(limit, 1), 10000)")

# 2. Modify the loop to not skip
old_loop = """        for proj, year_data in project_years.items():
            years_sorted = sorted(year_data.keys())
            if len(years_sorted) < 2:
                continue

            # 取最早年和最晚年（排除 2026 部分年资料）
            years_full = [y for y in years_sorted if y <= CURRENT_YEAR]
            if len(years_full) < 2:
                years_full = years_sorted   # 若只有 2026 资料，仍计算

            earliest_yr    = years_full[0]
            actual_latest_yr = years_full[-1]
            earliest_price = year_data[earliest_yr]
            latest_price   = year_data[actual_latest_yr]

            # 依据使用者需求，不管最新交易是哪一年，永远将结束年份设为今年 (CURRENT_YEAR)
            latest_yr = CURRENT_YEAR
            n_years = latest_yr - earliest_yr
            if n_years <= 0 or earliest_price <= 0:
                continue

            # CAGR = (latest/earliest)^(1/n) - 1
            import math
            cagr = (math.pow(latest_price / earliest_price, 1.0 / n_years) - 1) * 100
            
            if cagr < CAGR_MIN or cagr > CAGR_MAX:
                continue

            est_3yr = latest_price * math.pow(1 + (cagr / 100.0), 3)

            # --- Generate yearly breakdown for sparkline ---
            yearly_breakdown = []
            for y in years_sorted:
                yearly_breakdown.append({
                    'year': y,
                    'avg_price': round(year_data[y])
                })"""

new_loop = """        for proj, year_data in project_years.items():
            years_sorted = sorted(year_data.keys())
            
            cagr = None
            est_3yr = None
            earliest_yr = years_sorted[0]
            actual_latest_yr = years_sorted[-1]
            n_years = CURRENT_YEAR - earliest_yr
            
            latest_price = year_data[actual_latest_yr]
            latest_psf = project_psf.get(proj, {}).get(actual_latest_yr, 0)
            
            if len(years_sorted) >= 2:
                years_full = [y for y in years_sorted if y <= CURRENT_YEAR]
                if len(years_full) < 2:
                    years_full = years_sorted
                earliest_yr = years_full[0]
                actual_latest_yr = years_full[-1]
                earliest_price = year_data[earliest_yr]
                latest_price = year_data[actual_latest_yr]
                latest_psf = project_psf.get(proj, {}).get(actual_latest_yr, 0)
                n_years = CURRENT_YEAR - earliest_yr
                
                if n_years > 0 and earliest_price > 0:
                    import math
                    temp_cagr = (math.pow(latest_price / earliest_price, 1.0 / n_years) - 1) * 100
                    if CAGR_MIN <= temp_cagr <= CAGR_MAX:
                        cagr = temp_cagr
                        est_3yr = latest_price * math.pow(1 + (cagr / 100.0), 3)

            # --- Generate yearly breakdown for sparkline ---
            yearly_breakdown = []
            for y in years_sorted:
                yearly_breakdown.append({
                    'year': y,
                    'avg_price': round(year_data[y])
                })"""

content = content.replace(old_loop, new_loop)

# 3. Modify the results.append part
old_append = """            results.append({
                'project': proj,
                'postal': project_postal.get(proj, 'NIL'),
                'developer': project_developer.get(proj, '—'),
                'earliest_yr': earliest_yr,
                'latest_yr': actual_latest_yr,
                'n_years': n_years,
                'cagr': round(cagr, 2),
                'latest_price': round(latest_price),
                'latest_psf': round(project_psf.get(proj, {}).get(actual_latest_yr, 0)),
                'est_3yr': round(est_3yr),
                'yearly': yearly_breakdown
            })"""

new_append = """            results.append({
                'project': proj,
                'postal': project_postal.get(proj, 'NIL'),
                'developer': project_developer.get(proj, '—'),
                'earliest_yr': earliest_yr,
                'latest_yr': actual_latest_yr,
                'n_years': n_years if n_years > 0 else 1,
                'cagr': round(cagr, 2) if cagr is not None else None,
                'latest_price': round(latest_price) if latest_price else None,
                'latest_psf': round(latest_psf) if latest_psf else None,
                'est_3yr': round(est_3yr) if est_3yr is not None else None,
                'yearly': yearly_breakdown
            })"""

content = content.replace(old_append, new_append)

# Also fix the sort logic because cagr can be None
old_sort = "results.sort(key=lambda x: x['cagr'], reverse=True)"
new_sort = "results.sort(key=lambda x: x['cagr'] if x['cagr'] is not None else -999, reverse=True)"
content = content.replace(old_sort, new_sort)

with open('backend/landlord_api.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Updated backend to include all projects regardless of data quality.")
