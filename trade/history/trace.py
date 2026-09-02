import csv

trades = []
with open('ibkr_history1.csv', 'r', encoding='utf-8-sig', errors='ignore') as f:
    reader = csv.reader(f)
    for row in reader:
        if len(row) >= 13 and row[1] == 'Data':
            desc = row[4].upper()
            symbol = row[6].upper()
            if 'MNQ' in desc or 'NQ' in desc or 'QN' in desc:
                try:
                    net = 0
                    if '(' in row[12]:
                        net = -abs(float(row[12].replace('(', '').replace(')', '')))
                    else:
                        net = float(row[12])
                    trades.append({'date': row[2], 'desc': desc, 'action': row[7], 'price': row[8], 'net': net, 'is_mtm': 'MTM' in desc})
                except: pass

trades.sort(key=lambda x: x['date'])
cumulative_pnl = 0
for t in trades:
    cumulative_pnl += t['net']
    action_str = t['action'] if not t['is_mtm'] else 'MTM'
    print(f"{t['date']} | {t['desc']} | Qty: {action_str} | Price: {t['price']} | Net: {t['net']:.2f} | Cum PnL: {cumulative_pnl:.2f}")
