with open('backend/landlord_api.py', 'a', encoding='utf-8') as f:
    f.write('''
@app.route('/api/commercial_transactions', methods=['GET', 'OPTIONS'])
def get_commercial_transactions():
    if request.method == 'OPTIONS':
        return build_cors_preflight_response()
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # We fetch all columns, sorted by price_sgd DESC as requested by user
        cursor.execute("""
            SELECT 
                project_name, 
                street_name, 
                property_type, 
                tenure, 
                area_sqm, 
                price_sgd, 
                psf_sgd, 
                contract_date
            FROM ura_commercial_transactions
            ORDER BY price_sgd DESC
        """)
        rows = cursor.fetchall()
        
        data = []
        for r in rows:
            data.append({
                'project_name': r['project_name'],
                'street_name': r['street_name'],
                'property_type': r['property_type'],
                'tenure': r['tenure'],
                'area_sqm': r['area_sqm'],
                'price_sgd': r['price_sgd'],
                'psf_sgd': r['psf_sgd'],
                'contract_date': r['contract_date']
            })
            
        conn.close()
        
        response = jsonify({'status': 'success', 'data': data})
        response.headers.add("Access-Control-Allow-Origin", "*")
        return response
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500
''')
print("Successfully appended commercial API endpoint.")
