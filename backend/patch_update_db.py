with open('backend/update_db.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Comment out fetch_hdb_data calls
content = content.replace("fetch_hdb_data('d_8b84c4ee58e3cfc0ece0d773c8ca6abc', 'hdb', hdb_resale_cols)", "# fetch_hdb_data('d_8b84c4ee58e3cfc0ece0d773c8ca6abc', 'hdb', hdb_resale_cols)")
content = content.replace("fetch_hdb_data('d_c9f57187485a850908655db0e8cfe651', 'hdb_rental', hdb_rental_cols)", "# fetch_hdb_data('d_c9f57187485a850908655db0e8cfe651', 'hdb_rental', hdb_rental_cols)")

with open('backend/update_db.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("update_db.py patched")
