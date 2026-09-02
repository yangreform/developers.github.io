import sys
import time
import argparse
import datetime
from ib_insync import *

# Parse arguments
parser = argparse.ArgumentParser()
parser.add_argument('--symbol', required=True, help='Stock symbol')
parser.add_argument('--strategy', required=True, help='iron_condor, short_call, short_put')
parser.add_argument('--qty', type=int, default=1)
parser.add_argument('--port', type=int, default=4001)
args = parser.parse_args()

ib = IB()
try:
    # Use a high client ID to avoid conflicts
    ib.connect('127.0.0.1', args.port, clientId=9999)
except Exception as e:
    print(f"Failed to connect to IBKR: {e}")
    sys.exit(1)

def main():
    symbol = args.symbol
    stk = Stock(symbol, 'SMART', 'USD')
    try:
        ib.qualifyContracts(stk)
    except:
        print(f"Failed to qualify {symbol}")
        return

    chains = ib.reqSecDefOptParams(stk.symbol, '', stk.secType, stk.conId)
    if not chains:
        print(f"No options chains for {symbol}")
        return
        
    chain = next((c for c in chains if c.exchange == 'SMART'), chains[0])
    expirations = sorted(chain.expirations)
    
    # Pick expiration ~30-45 days out
    today = datetime.date.today()
    target_date = today + datetime.timedelta(days=40)
    closest_expiry = min(expirations, key=lambda x: abs(datetime.datetime.strptime(x, '%Y%m%d').date() - target_date))
    
    print(f"[{symbol}] Picked expiry: {closest_expiry}")
    
    [ticker] = ib.reqTickers(stk)
    ib.sleep(1)
    current_price = ticker.marketPrice()
    if not current_price or current_price != current_price:
        current_price = ticker.close
        
    print(f"[{symbol}] Current Price: {current_price}")
    
    strikes = sorted(chain.strikes)
    
    # Simple strike selection based on distance
    
    if args.strategy == 'short_put':
        short_put_strike = next((s for s in reversed(strikes) if s < current_price * 0.90), strikes[0])
        print(f"[{symbol}] Short Put Strike: {short_put_strike}")
        
        opt = Option(symbol, closest_expiry, short_put_strike, 'P', 'SMART')
        ib.qualifyContracts(opt)
        
        order = MarketOrder('SELL', args.qty)
        order.tif = 'DAY'
        order.algoStrategy = 'Adaptive'
        order.algoParams = [TagValue('adaptivePriority', 'Patient')]
        trade = ib.placeOrder(opt, order)
        
    elif args.strategy == 'short_call':
        short_call_strike = next((s for s in strikes if s > current_price * 1.10), strikes[-1])
        print(f"[{symbol}] Short Call Strike: {short_call_strike}")
        
        opt = Option(symbol, closest_expiry, short_call_strike, 'C', 'SMART')
        ib.qualifyContracts(opt)
        
        order = MarketOrder('SELL', args.qty)
        order.tif = 'DAY'
        order.algoStrategy = 'Adaptive'
        order.algoParams = [TagValue('adaptivePriority', 'Patient')]
        trade = ib.placeOrder(opt, order)
        
    elif args.strategy == 'iron_condor':
        short_put_strike = next((s for s in reversed(strikes) if s < current_price * 0.90), strikes[0])
        long_put_strike = next((s for s in reversed(strikes) if s < short_put_strike), strikes[0])
        
        short_call_strike = next((s for s in strikes if s > current_price * 1.10), strikes[-1])
        long_call_strike = next((s for s in strikes if s > short_call_strike), strikes[-1])
        
        print(f"[{symbol}] Iron Condor Strikes: +P{long_put_strike} -P{short_put_strike}  -C{short_call_strike} +C{long_call_strike}")
        
        combo = Contract(symbol=symbol, secType='BAG', currency='USD', exchange='SMART')
        
        # Qualify legs
        p_long = Option(symbol, closest_expiry, long_put_strike, 'P', 'SMART')
        p_short = Option(symbol, closest_expiry, short_put_strike, 'P', 'SMART')
        c_short = Option(symbol, closest_expiry, short_call_strike, 'C', 'SMART')
        c_long = Option(symbol, closest_expiry, long_call_strike, 'C', 'SMART')
        ib.qualifyContracts(p_long, p_short, c_short, c_long)
        
        leg1 = ComboLeg(conId=p_long.conId, ratio=1, action='BUY', exchange='SMART')
        leg2 = ComboLeg(conId=p_short.conId, ratio=1, action='SELL', exchange='SMART')
        leg3 = ComboLeg(conId=c_short.conId, ratio=1, action='SELL', exchange='SMART')
        leg4 = ComboLeg(conId=c_long.conId, ratio=1, action='BUY', exchange='SMART')
        
        combo.comboLegs = [leg1, leg2, leg3, leg4]
        
        order = MarketOrder('SELL', args.qty)
        order.tif = 'DAY'
        order.algoStrategy = 'Adaptive'
        order.algoParams = [TagValue('adaptivePriority', 'Patient')]
        trade = ib.placeOrder(combo, order)

    print(f"=== [Order Placed] Waiting for Fill ({symbol}) ===")
    end_time = time.time() + 60
    while time.time() < end_time:
        ib.sleep(1)
        if trade.orderStatus.status == 'Filled':
            break
            
    if trade.orderStatus.status == 'Filled':
        print(f"=== [Filled] {symbol} at {trade.orderStatus.avgFillPrice} ===")
    else:
        print(f"=== [Not completely filled] Status: {trade.orderStatus.status} ===")

try:
    main()
finally:
    ib.disconnect()
