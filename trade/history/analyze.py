import csv
import json

env_path = r'C:\Users\Administrator\Desktop\docker_mc\developers.github.io\trade\.env'
hedge_config = {}
with open(env_path, 'r', encoding='utf-8') as f:
    for line in f:
        if line.startswith('HEDGE_CONFIG_JSON='):
            json_str = line.split('=', 1)[1].strip().strip('\'')
            hedge_config = json.loads(json_str)

groups = {k: {'pnl': 0.0, 'trades': [], 'mtms': []} for k in hedge_config.keys()}
groups['未分類(Other)'] = {'pnl': 0.0, 'trades': [], 'mtms': []}

def get_group(desc, symbol):
    for g_name, info in hedge_config.items():
        for s in info['symbols']:
            if desc.startswith(s + ' ') or symbol.startswith(s):
                return g_name
    if 'ZC' in desc or 'YC' in desc or 'XC' in symbol or 'OZC' in symbol or 'OCD' in symbol:
        return '玉米(Corn)'
    if 'MES' in desc or 'EWN' in symbol or 'EW1' in symbol or 'EWQ' in symbol:
        return '小標(ES)'
    return '未分類(Other)'

with open('ibkr_history1.csv', 'r', encoding='utf-8-sig', errors='ignore') as f:
    reader = csv.reader(f)
    for row in reader:
        if len(row) >= 13 and row[1] == 'Data':
            desc = row[4].upper()
            symbol = row[6].upper()
            try:
                net_amount = 0
                if '(' in row[12]:
                    net_amount = -abs(float(row[12].replace('(', '').replace(')', '')))
                else:
                    net_amount = float(row[12])
                
                g_name = get_group(desc, symbol)
                groups[g_name]['pnl'] += net_amount
                
                if 'MTM' in desc:
                    groups[g_name]['mtms'].append({'desc': desc, 'net': net_amount, 'date': row[2]})
                else:
                    groups[g_name]['trades'].append({'desc': desc, 'action': row[7], 'price': row[8], 'net': net_amount, 'date': row[2]})
            except ValueError:
                pass

html = """
<html>
<head>
<meta charset="utf-8">
<title>IBKR 對沖群組現金流統計 (Cash Flow Summary)</title>
<style>
    body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; padding: 20px; background-color: #f8f9fa; }
    h1 { color: #343a40; }
    table { border-collapse: collapse; width: 80%; background-color: #fff; margin-bottom: 30px; box-shadow: 0 0 10px rgba(0,0,0,0.1); }
    th, td { border: 1px solid #dee2e6; padding: 12px; text-align: left; }
    th { background-color: #343a40; color: #fff; }
    tr:nth-child(even) { background-color: #f2f2f2; }
    .pos { color: green; font-weight: bold; }
    .neg { color: red; font-weight: bold; }
    .note { padding: 15px; background-color: #e9ecef; border-left: 5px solid #007bff; margin-bottom: 20px; }
</style>
</head>
<body>
    <h1>IBKR 對沖群組 現金流(Cash Flow) 統計報告</h1>
    <div class="note">
        <strong>⚠️ 注意事項：</strong> 本報告基於 IBKR「現金結單/轉帳歷史」生成，此處的金額代表<strong>現金流量 (Cash Flow)</strong>。<br>
        1. 期貨 (Futures)：每日 MTM 損益會直接反映為現金流，因此期貨現金流 = 期貨損益。<br>
        2. 期權 (Options)：期權只有在<strong>買入或賣出時</strong>才會產生現金流，期權的未實現損益 (Unrealized PnL) 不會顯示在此表中。如果使用 Gamma 剝頭皮策略買入期權，買入當下的權利金支出若發生在歷史紀錄範圍外，不會計算在內，但期權的時間價值流失 (Theta Decay) 會導致您的帳戶淨值 (NAV) 下降，這也是帳戶賠錢但現金流表可能看不出來的主因。
    </div>
    <h2>=== 總損益/現金流結算 (SGD) ===</h2>
    <table>
        <tr>
            <th>對沖群組 (Group)</th>
            <th>總現金流 (SGD)</th>
            <th>期貨每日 MTM 次數</th>
            <th>實質交易次數</th>
        </tr>
"""
for g, data in groups.items():
    if data['pnl'] != 0:
        cls = 'pos' if data['pnl'] > 0 else 'neg'
        html += f"<tr><td>{g}</td><td class='{cls}'>{data['pnl']:,.2f}</td><td>{len(data['mtms'])}</td><td>{len(data['trades'])}</td></tr>\n"

html += """
    </table>
    <h2>=== 玉米(Corn) 交易明細 ===</h2>
    <table>
        <tr>
            <th>日期 (Date)</th>
            <th>說明 (Description)</th>
            <th>動作/數量 (Qty)</th>
            <th>價格 (Price)</th>
            <th>現金流 (SGD)</th>
        </tr>
"""
corn = groups['玉米(Corn)']
for t in corn['trades']:
    cls = 'pos' if t['net'] > 0 else 'neg'
    html += f"<tr><td>{t['date']}</td><td>{t['desc']}</td><td>{t['action']}</td><td>{t['price']}</td><td class='{cls}'>{t['net']:,.2f}</td></tr>\n"
html += "</table></body></html>"

with open('ibkr_report.html', 'w', encoding='utf-8') as f:
    f.write(html)
