import sqlite3
import requests
import time
import os
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "landlord_sg.db")

def init_tables(cursor):
    cursor.execute('DROP TABLE IF EXISTS ura_commercial_coordinates')
    cursor.execute('''
        CREATE TABLE ura_commercial_coordinates (
            project_name TEXT,
            street_name TEXT,
            lat REAL,
            lng REAL,
            postal TEXT,
            formatted_address TEXT,
            PRIMARY KEY (project_name, street_name)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ura_coordinates (
            project TEXT PRIMARY KEY,
            street TEXT,
            lat REAL,
            lng REAL
        )
    ''')

def clean_name(val):
    if not val: return ""
    v = val.strip()
    v = v.split('/')[0].strip()
    return v

def search_photon(query):
    try:
        url = f"https://photon.komoot.io/api/?q={requests.utils.quote(query)}&lat=1.3521&lon=103.8198"
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        if r.status_code == 200:
            data = r.json()
            features = data.get('features', [])
            for f in features:
                props = f.get('properties', {})
                country = props.get('country', '').lower()
                # Ensure it is in Singapore
                if 'singapore' in country or props.get('countrycode', '').lower() == 'sg' or (1.1 <= f['geometry']['coordinates'][1] <= 1.5 and 103.5 <= f['geometry']['coordinates'][0] <= 104.1):
                    coords = f['geometry']['coordinates']
                    return {
                        'lat': float(coords[1]),
                        'lng': float(coords[0]),
                        'postal': props.get('postcode', ''),
                        'address': props.get('name', '') + ' ' + props.get('street', '')
                    }
    except Exception:
        pass
    return None

def search_nominatim(query):
    try:
        url = f"https://nominatim.openstreetmap.org/search?q={requests.utils.quote(query)}&format=json&countrycodes=sg&limit=1"
        r = requests.get(url, headers={'User-Agent': 'LandlordSG-Geocoding-Agent/1.0'}, timeout=5)
        if r.status_code == 200:
            data = r.json()
            if data and len(data) > 0:
                return {
                    'lat': float(data[0]['lat']),
                    'lng': float(data[0]['lon']),
                    'postal': '',
                    'address': data[0].get('display_name', '')
                }
    except Exception:
        pass
    return None

def geocode_pair(proj, street, existing_street_coords):
    # Tier 1: Check existing DB coordinates
    if street in existing_street_coords:
        lat, lng = existing_street_coords[street]
        return {'lat': lat, 'lng': lng, 'postal': '', 'address': f'{street} (from DB)'}

    proj_clean = clean_name(proj)
    street_clean = clean_name(street)
    is_conservation = "CONSERVATION" in proj.upper() or proj.upper() == 'NIL' or proj.upper() == street.upper()

    # Tier 2: Photon Search with Building Name
    if not is_conservation and proj_clean:
        res = search_photon(f"{proj_clean}, Singapore")
        if res: return res
        res = search_photon(f"{proj_clean} {street_clean}, Singapore")
        if res: return res

    # Tier 3: Photon Search with Street Name
    if street_clean:
        res = search_photon(f"{street_clean}, Singapore")
        if res: return res

    # Tier 4: Nominatim Search Fallback
    if not is_conservation and proj_clean:
        res = search_nominatim(f"{proj_clean}, Singapore")
        if res: return res
    if street_clean:
        res = search_nominatim(f"{street_clean}, Singapore")
        if res: return res

    return None

def main():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    init_tables(cursor)

    # Pre-cache known streets from ura_coordinates
    cursor.execute("SELECT street, AVG(lat), AVG(lng) FROM ura_coordinates WHERE lat IS NOT NULL GROUP BY street")
    existing_street_coords = {r[0]: (r[1], r[2]) for r in cursor.fetchall()}
    print(f"Loaded {len(existing_street_coords)} reference street coordinates from existing database.")

    cursor.execute('''
        SELECT DISTINCT project_name, street_name 
        FROM ura_commercial_transactions 
        WHERE project_name IS NOT NULL AND project_name != ''
    ''')
    pairs = cursor.fetchall()
    total = len(pairs)
    print(f"Total commercial (project, street) pairs to geocode: {total}\n")

    success_count = 0
    fail_list = []

    for idx, (proj, street) in enumerate(pairs, 1):
        # Check if already processed in ura_commercial_coordinates
        cursor.execute("SELECT lat, lng FROM ura_commercial_coordinates WHERE project_name=? AND street_name=?", (proj, street))
        row = cursor.fetchone()
        if row and row[0] is not None:
            success_count += 1
            # Ensure it is also in ura_coordinates
            cursor.execute("INSERT OR IGNORE INTO ura_coordinates (project, street, lat, lng) VALUES (?, ?, ?, ?)",
                           (proj, street, row[0], row[1]))
            continue

        res = geocode_pair(proj, street, existing_street_coords)

        if res:
            cursor.execute('''
                INSERT OR REPLACE INTO ura_commercial_coordinates 
                (project_name, street_name, lat, lng, postal, formatted_address)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (proj, street, res['lat'], res['lng'], res.get('postal', ''), res.get('address', '')))
            
            # Also register into ura_coordinates
            cursor.execute('''
                INSERT OR IGNORE INTO ura_coordinates (project, street, lat, lng)
                VALUES (?, ?, ?, ?)
            ''', (proj, street, res['lat'], res['lng']))

            # Cache the newly found street coordinate
            if street not in existing_street_coords:
                existing_street_coords[street] = (res['lat'], res['lng'])

            success_count += 1
            print(f"[{idx}/{total}] OK: {proj} ({street}) -> ({res['lat']:.5f}, {res['lng']:.5f})")
        else:
            fail_list.append((proj, street))
            print(f"[{idx}/{total}] FAIL: {proj} ({street})")

        if idx % 25 == 0:
            conn.commit()
        time.sleep(0.05)

    conn.commit()
    conn.close()

    print(f"\n==========================================")
    print(f"Geocoding Finished!")
    print(f"Total: {total}, Successfully Geocoded: {success_count} ({success_count/total*100:.1f}%), Failed: {len(fail_list)}")
    print(f"==========================================")
    if fail_list:
        print("Failed list:", fail_list)

if __name__ == '__main__':
    main()
