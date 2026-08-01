import re

def modify_api():
    filepath = 'backend/landlord_api.py'
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # We need to replace the section:
    # earliest_yr    = years_full[0]
    # latest_yr      = years_full[-1]
    # earliest_price = year_data[earliest_yr]
    # latest_price   = year_data[latest_yr]
    #
    # n_years = latest_yr - earliest_yr

    old_block = """            earliest_yr    = years_full[0]
            latest_yr      = years_full[-1]
            earliest_price = year_data[earliest_yr]
            latest_price   = year_data[latest_yr]

            n_years = latest_yr - earliest_yr"""

    new_block = """            earliest_yr    = years_full[0]
            actual_latest_yr = years_full[-1]
            earliest_price = year_data[earliest_yr]
            latest_price   = year_data[actual_latest_yr]

            # 依據使用者需求，不管最新交易是哪一年，永遠將結束年份設為今年 (CURRENT_YEAR)
            latest_yr = CURRENT_YEAR
            n_years = latest_yr - earliest_yr"""

    if old_block in content:
        content = content.replace(old_block, new_block)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        print("Success: Replaced block.")
    else:
        print("Error: Could not find the target block in landlord_api.py")

if __name__ == '__main__':
    modify_api()
