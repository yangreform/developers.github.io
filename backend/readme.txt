
cd C:\Users\Administrator\Desktop\docker_mc\developers.github.io\backend

# 1. 更新 URA 私宅最新交易數據 (原版腳本，已拔除 HDB)
python update_db.py

# 2. 自動補齊新私宅的開發商名稱 (Selenium)
python fill_missing_developers_selenium.py

# 3. [全新] 一鍵匯入所有商辦 CSV
python sync_commercial_csv.py
