import re

with open('backend/landlord_api.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Make sure not to add it twice
if '/api/commercial_transactions' not in content:
    commercial_api = '''
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
        print(f"Error in commercial API: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500
'''
    # Insert it before the app.run
    content = content.replace("if __name__ == '__main__':", commercial_api + "\nif __name__ == '__main__':")
    
    with open('backend/landlord_api.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("Successfully added /api/commercial_transactions to backend.")
else:
    print("API endpoint already exists.")
