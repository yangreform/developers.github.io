def get_latest_bull_put_csv():
    import glob
    csv_files = glob.glob(os.path.join(BARCHART_DIR, "bull-put-spread-option-screener-bull-put-adv*.csv"))
    if not csv_files: return None
    return max(csv_files, key=os.path.getctime)

def parse_numeric(val):
    if isinstance(val, str):
        val = val.replace(',', '').replace('%', '').strip()
        if val in ('', 'NA', 'N/A'): return 0.0
    try:
        return float(val)
    except:
        return 0.0
    try:
        return float(val)
    except:
        return 0.0

def connect_dash_ib():
    if not dash_ib.isConnected():
        try:
            # ?輯撒??CLIENT_ID + 1 ??蹓??? option.py ????制???
            dash_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(2000, 2999))
        except:
            pass



@dash_app.route('/api/option/data_bull_put')
def get_dash_data_bull_put():
    file_path = get_latest_bull_put_csv()
    if not file_path:
        return jsonify({'status': 'error', 'message': f'?????? bull-put-spread-option-screener-bull-put-adv CSV ?澗??'})
    
    try:
        import pandas as pd
        df = pd.read_csv(file_path)
        df = df.dropna(subset=['Exp Date']) # Remove Barchart footer
        
        options = []
        for _, row in df.iterrows():
            symbol = row['Symbol']
            exp = str(row['Exp Date']).replace('-', '')
            leg1 = float(row['Leg1 Strike'])
            leg2 = float(row['Leg2 Strike'])
            options.append(Option(symbol, exp, leg1, 'P', 'SMART'))
            options.append(Option(symbol, exp, leg2, 'P', 'SMART'))

        # Create isolated IB instance for thread safety
        import random
        from ib_insync import IB
        import asyncio
        # ALWAYS create a new event loop for this thread to prevent hanging on reuse
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        local_ib = IB()
        try:
            local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(3000, 4999), timeout=10)
            qualified = local_ib.qualifyContracts(*options)
        finally:
            local_ib.disconnect()
            
        valid_conids = { (c.symbol, c.lastTradeDateOrContractMonth, c.strike, c.right): c.conId for c in qualified }
        
        results = []
        for _, row in df.iterrows():
            symbol = row['Symbol']
            exp = str(row['Exp Date']).replace('-', '')
            leg1 = float(row['Leg1 Strike'])
            leg2 = float(row['Leg2 Strike'])
            max_profit = float(str(row['Max Profit']).replace('$',''))
            
            # Check if both legs qualified
            key1 = (symbol, exp, leg1, 'P')
            key2 = (symbol, exp, leg2, 'P')
            
            is_valid = False
            con1, con2 = None, None
            if key1 in valid_conids and key2 in valid_conids:
                is_valid = True
                con1 = valid_conids[key1]
                con2 = valid_conids[key2]
                
            results.append({
                'Symbol': symbol,
                'Exp_Date': str(row['Exp Date']),
                'Leg1_Strike': leg1,
                'Leg2_Strike': leg2,
                'Max_Profit': max_profit,
                'is_valid': is_valid,
                'Leg1_conId': con1,
                'Leg2_conId': con2
            })
            
        return jsonify({'status': 'ok', 'data': results})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@dash_app.route('/api/option/portfolio')
def get_dash_portfolio():
    import random
    from ib_insync import IB
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    local_ib = IB()
    try:
        local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(7000, 7999), timeout=10)
        positions = []
        for p in local_ib.portfolio():
            if p.contract.secType in ['OPT', 'FOP']:
                action = 'BUY' if p.position < 0 else 'SELL'
                positions.append({
                    'conId': p.contract.conId,
                    'symbol': p.contract.symbol,
                    'localSymbol': p.contract.localSymbol,
                    'position': p.position,
                    'marketPrice': p.marketPrice,
                    'action': action
                })
        return jsonify({'status': 'ok', 'positions': positions})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})
    finally:
        local_ib.disconnect()

@dash_app.route('/api/option/close', methods=['POST'])
def close_dash_position():
    payload = request.get_json()
    conId = payload.get('conId')
    action = payload.get('action')
    qty = payload.get('quantity')
    
    import random
    from ib_insync import IB, Contract, MarketOrder, TagValue
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    local_ib = IB()
    try:
        local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(8000, 8999), timeout=10)
        contract = Contract(conId=int(conId))
        local_ib.qualifyContracts(contract)
        
        order = MarketOrder(action, float(qty))
        order.tif = 'DAY'
        order.algoStrategy = 'Adaptive'
        order.algoParams = [TagValue('adaptivePriority', 'Patient')]
        
        trade = local_ib.placeOrder(contract, order)
        local_ib.sleep(1)
        return jsonify({'status': 'ok', 'message': f'Order placed: {action} {qty}'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})
    finally:
        local_ib.disconnect()

@dash_app.route('/api/option/trade_bull_put', methods=['POST'])
def place_dash_trade_bull_put():
    payload = request.get_json()
    symbol = payload.get('Symbol')
    leg1_conId = payload.get('Leg1_conId')
    leg2_conId = payload.get('Leg2_conId')
    credit = float(payload.get('Max_Profit'))
    
    if not leg1_conId or not leg2_conId:
         return jsonify({'status': 'error', 'message': 'Invalid conIds for legs.'})
         
    try:
        from ib_insync import Contract, ComboLeg, LimitOrder
        # Build BAG
        contract = Contract()
        contract.symbol = symbol
        contract.secType = 'BAG'
        contract.currency = 'USD'
        contract.exchange = 'SMART'

        # To SELL the spread, the leg definitions must be reversed relative to the final execution
        l1 = ComboLeg(conId=int(leg1_conId), ratio=1, action='BUY', exchange='SMART')   # SELL order * BUY leg = SELL leg1
        l2 = ComboLeg(conId=int(leg2_conId), ratio=1, action='SELL', exchange='SMART')  # SELL order * SELL leg = BUY leg2
        contract.comboLegs = [l1, l2]

                # Isolated connection for placing order
        import random
        from ib_insync import IB
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        local_ib = IB()
        try:
            local_ib.connect(IB_HOST, IB_PORT, clientId=random.randint(5000, 6999), timeout=10)
            
            # Request market data to get ASK price
            local_ib.qualifyContracts(contract)
            ticker = local_ib.reqMktData(contract, "", True, False)
            
            # Wait up to 2 seconds for data
            limit_price = credit
            import math
            for _ in range(20):
                local_ib.sleep(0.1)
                if ticker.ask and not math.isnan(ticker.ask) and ticker.ask > 0:
                    limit_price = ticker.ask
                    break
            
            limit_price = round(limit_price, 2)
            
            # Parent limit order
            parent = LimitOrder('SELL', 1, limit_price)
            parent.transmit = False
            
            # Child limit order
            take_profit = LimitOrder('BUY', 1, round(limit_price * 0.25, 2))
            take_profit.transmit = True

            parent.orderId = local_ib.client.getReqId()
            take_profit.parentId = parent.orderId
            local_ib.placeOrder(contract, parent)
            local_ib.placeOrder(contract, take_profit)
            # Give TWS time to process the orders before disconnecting
            local_ib.sleep(1)
        finally:
            local_ib.disconnect()

        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'??謘??: {e}'})

def start_dashboard_server():
    print("?賹? Barchart Dashboard ??Port 5900...")
    serve(dash_app, host='0.0.0.0', port=5900)



