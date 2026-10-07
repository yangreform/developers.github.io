#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Barchart Bull Put Spread Pipeline with Skills Architecture (trade/bull-put.py)
================================================================================
本模組為 Bull Put 垂直價差高階管線調度器，導入模組化 Agentic Skills 設計：
  (1) Download Skill (trade/skills/download_skill/):
      - 自動連線 Barchart 下載官方 Bull Put 垂直價差篩選數據：
        • URL: https://www.barchart.com/options/vertical-spreads/bull-put-spread?popularScreener=221982&setPopularScreener=true&orderBy=lossProbability&orderDir=asc
        • Target Folder: trade/Barchart/
  (2) Gemini Helper (trade/skills/gemini_helper.py):
      - 清洗價差數據、過濾已到期合約、按跌破機率與報酬率排序
      - 調用 Gemini AI 深度量化分析，精選唯一最佳 Bull Put Spread 組合
      - 儲存詳細分析報告並推播至手機 LINE
  (3) Scale In Order Skill (trade/skills/scale_in_order_skill/):
      - 連線 IBKR 建立下翼賣權 (Short Put) 與保護賣權 (Long Put) 之 BAG 組合單
      - 當執行加碼委託 (SELL) 時若遭遇 IBKR Error 201:
        "Cannot have open orders on both sides of the same US Option contract."
        自動依序執行 4 大重組步驟：撤銷衝突舊單 -> 送出加碼母單 -> 合併新舊口數 -> 重掛總口數 OCA 括號單
================================================================================
"""

import os
import sys
import glob
import time
import math
import re
import datetime
import argparse
import pandas as pd

# 確保 Windows 主控台正確輸出 UTF-8 字符
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
for p in [BASE_DIR, PROJECT_ROOT]:
    if p not in sys.path:
        sys.path.insert(0, p)

ENV_FILE = os.path.join(BASE_DIR, ".env")
BARCHART_DIR = os.path.join(BASE_DIR, "Barchart")
OLD_DIR = os.path.join(BARCHART_DIR, "old")
REPORTS_DIR = os.path.join(BARCHART_DIR, "reports")

os.makedirs(BARCHART_DIR, exist_ok=True)
os.makedirs(OLD_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)

# 載入模組化 Skills
try:
    from trade.skills import (
        init_driver,
        dismiss_popups,
        login_if_needed,
        load_credentials_from_env,
        download_bull_put_csv,
        PROFILE_DIR,
        ScaleInOrderSkill,
        load_env_settings,
        create_fast_ib_connection,
        load_gemini_api_key,
        call_gemini_for_skill,
    )
except ImportError:
    from skills.download_skill import (
        init_driver,
        dismiss_popups,
        login_if_needed,
        load_credentials_from_env,
        download_bull_put_csv,
        PROFILE_DIR,
    )
    from skills.scale_in_order_skill import ScaleInOrderSkill
    from skills.ibkr_skill import load_env_settings, create_fast_ib_connection
    from skills.gemini_helper import load_gemini_api_key, call_gemini_for_skill

try:
    from ib_insync import IB, Option, Contract, ComboLeg
except ImportError:
    IB = None
    Option = None
    Contract = None
    ComboLeg = None

try:
    from notifier import send_push_message
except ImportError:
    send_push_message = None


def normalize_exp_date(date_val):
    """將到期日轉換為 YYYYMMDD 格式"""
    if pd.isna(date_val):
        return ""
    s = str(date_val).strip()
    if " " in s:
        s = s.split(" ")[0]
    s = s.replace("/", "-")
    parts = s.split("-")
    if len(parts) == 3:
        if len(parts[0]) == 4:
            return f"{parts[0]}{parts[1].zfill(2)}{parts[2].zfill(2)}"
        elif len(parts[2]) == 4:
            return f"{parts[2]}{parts[0].zfill(2)}{parts[1].zfill(2)}"
        elif len(parts[2]) == 2:
            return f"20{parts[2]}{parts[0].zfill(2)}{parts[1].zfill(2)}"
    cleaned = re.sub(r"\D", "", s)
    return cleaned if len(cleaned) == 8 else s


def format_exp_date_display(exp_date_yyyymmdd):
    """將 YYYYMMDD 格式化為 YYYY-MM-DD"""
    s = str(exp_date_yyyymmdd).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


# ==============================================================================
# 1. Bull Put Spread 數據清洗與 Gemini 深度分析
# ==============================================================================
def clean_and_prepare_bull_put_data(csv_path, top_count=35):
    """
    清洗 Bull Put 數據，過濾已過期合約，自動結合歷史 old/ 候選組合，
    依跌破虧損機率 (Loss Prob) 昇序與最大報酬率降序排序。
    """
    candidate_files = []
    if csv_path and os.path.exists(csv_path):
        candidate_files.append(csv_path)

    all_bp = glob.glob(os.path.join(BARCHART_DIR, "*bull-put*.csv")) + glob.glob(os.path.join(OLD_DIR, "*bull-put*.csv"))
    all_bp.sort(key=os.path.getmtime, reverse=True)
    for f in all_bp:
        if f not in candidate_files:
            candidate_files.append(f)

    all_dfs = []
    today_str = datetime.date.today().strftime("%Y-%m-%d")

    for f in candidate_files:
        if not os.path.exists(f) or os.path.getsize(f) < 350:
            continue
        try:
            df = pd.read_csv(f)
            df = df.dropna(subset=["Symbol", "Exp Date"])
            df = df[~df["Symbol"].astype(str).str.contains("Downloaded", case=False, na=False)]
            df = df[df["Exp Date"].astype(str) >= today_str]
            if not df.empty:
                all_dfs.append(df)
        except Exception:
            continue

    if not all_dfs:
        raise ValueError("未找到任何含有未過期合約之 Bull Put Spread 數據檔案。")

    combined_bp = pd.concat(all_dfs, ignore_index=True)
    dedup_cols = [c for c in ["Symbol", "Exp Date", "Leg1 Strike", "Leg2 Strike"] if c in combined_bp.columns]
    if dedup_cols:
        combined_bp = combined_bp.drop_duplicates(subset=dedup_cols)

    # 排序：以 Loss Prob 昇序為主
    if "Loss Prob" in combined_bp.columns:
        loss_clean = combined_bp["Loss Prob"].astype(str).str.replace("%", "").str.strip()
        combined_bp["_loss_sort"] = pd.to_numeric(loss_clean, errors="coerce")
        combined_bp = combined_bp.sort_values(by="_loss_sort", ascending=True)
        combined_bp = combined_bp.drop(columns=["_loss_sort"])

    top_df = combined_bp.head(top_count).copy()
    print(f"[INFO] [BullPutSkill] 數據清洗完成：篩選出 {len(top_df)} 筆未過期候選價差組合 (涵蓋標的: {list(top_df['Symbol'].unique())[:6]})")
    return top_df


def build_bull_put_prompt(top_df):
    """組裝專屬之 Bull Put Spread 提示詞"""
    display_cols = [c for c in ["Symbol", "Price~", "Exp Date", "DTE", "Leg1 Strike", "Leg1 Bid", "Leg2 Strike", "Leg2 Ask", "Net Credit", "Max Loss", "Return", "Loss Prob", "BE (Buffer)", "IV Rank"] if c in top_df.columns]
    table_str = top_df[display_cols].to_string(index=False)

    return f"""你是一名華爾街頂級期權量化經理人與波動率價差收租專家。
請根據以下來自 Barchart Bull Put Spread 官方篩選器的未過期候選價差組合清單，進行深度的多因子量化評選，挑選出「唯一最佳、勝率極高、安全緩衝極充足、性價比最高」的 1 組 Bull Put 垂直價差組合：

======================================================================
【待評選之 Bull Put 垂直價差候選清單】
======================================================================
{table_str}

======================================================================
【量化評比標準】
1. **安全防禦空間 (Break-Even Buffer %)**：
   優先挑選下檔緩衝空間充足（BE Buffer 較大），現貨價格遠高於賣出下翼 Put 履約價，具備堅實技術支撐的標的。
2. **極高勝率與低跌破機率 (Low Loss Probability)**：
   跌破虧損機率 (Loss Prob) 越低越優（通常建議小於 20%~25%），享有 75% 以上的高概率收租勝率。
3. **合理的淨權利金收入 (Net Credit & Return %)**：
   在確保極高安全邊際的前提下，提供健康的年化報酬率與每口淨權利金收入（Net Credit 建議至少 $0.30~$1.50 以上）。
4. **期限窗口最適性 (DTE Horizon)**：
   到期天數 DTE 介於 14 至 50 天（時間價值 Theta 加速衰退甜蜜期）。

======================================================================
【輸出格式規範】
請嚴格依據下列 Markdown 結構輸出，不得更動欄位名稱：

#### 📌 【最佳 Bull Put 垂直價差推薦】
- **推薦標的代號 (Symbol)**: [標的代號，如 QQQ]
- **標的現價 (Price)**: $[現價]
- **建議策略**: Bull Put Spread (垂直賣權信用價差)
- **到期日 (Exp Date)**: [YYYY-MM-DD]
- **到期天數 (DTE)**: [數字] 天
- **賣出下翼 (Short Put Leg 1)**: 履約價 $[數字] @ Bid $[數字]
- **買入保護 (Long Put Leg 2)**: 履約價 $[數字] @ Ask $[數字]
- **淨權利金收入 (Net Credit)**: $[數字] (每口最大獲利)
- **最大虧損風險 (Max Loss)**: $[數字]
- **損益兩平點與安全空間 (Break-Even Buffer)**: $[數字] (緩衝約 [百分比]%)
- **最大報酬率 (Max Return %)**: [數字]%
- **跌破虧損機率 (Loss Prob %)**: [數字]%
- **進場推薦理由與深度量化分析**:
  1. [安全邊際與下檔支撐評估]
  2. [跌破機率與勝率優勢]
  3. [權利金收斂與 Theta 衰退性價比]
"""


def parse_best_bull_put(report_text, top_df):
    """自報告中解析獲勝之 Bull Put 價差合約參數"""
    if not report_text:
        return None

    sym_m = re.search(r"(?:推薦標的代號|標的代號|Symbol).*?[:：]\s*[*_`]*([A-Za-z0-9\.\-]+)", report_text, re.IGNORECASE)
    symbol = sym_m.group(1).strip().upper().replace("*", "") if sym_m else None

    exp_m = re.search(r"(?:到期日|Exp(?:iration)?\s*(?:Date)?).*?[:：]\s*[*_`]*([0-9\-\/]{8,10})", report_text, re.IGNORECASE)
    exp_date_norm = normalize_exp_date(exp_m.group(1)) if exp_m else None

    # Leg 1 Short Put
    leg1_m = re.search(r"(?:賣出下翼|Short Put|Leg 1).*?履約價.*?\$?([0-9\.]+)", report_text, re.IGNORECASE)
    if not leg1_m:
        leg1_m = re.search(r"Leg 1 Strike.*?\$?([0-9\.]+)", report_text, re.IGNORECASE)
    leg1_strike = float(leg1_m.group(1)) if leg1_m else None

    # Leg 2 Long Put
    leg2_m = re.search(r"(?:買入保護|Long Put|Leg 2).*?履約價.*?\$?([0-9\.]+)", report_text, re.IGNORECASE)
    if not leg2_m:
        leg2_m = re.search(r"Leg 2 Strike.*?\$?([0-9\.]+)", report_text, re.IGNORECASE)
    leg2_strike = float(leg2_m.group(1)) if leg2_m else None

    # Net Credit
    credit_m = re.search(r"(?:淨權利金收入|Net Credit).*?[:：]\s*[*_`]*\$?([0-9\.]+)", report_text, re.IGNORECASE)
    net_credit = float(credit_m.group(1)) if credit_m else 0.50

    # 備援：若解析缺失，自 top_df 首筆取出
    if not symbol or not exp_date_norm or leg1_strike is None or leg2_strike is None:
        top_row = top_df.iloc[0]
        symbol = str(top_row["Symbol"]).strip().upper()
        exp_date_norm = normalize_exp_date(top_row["Exp Date"])
        leg1_strike = float(top_row["Leg1 Strike"])
        leg2_strike = float(top_row["Leg2 Strike"])
        net_credit = float(str(top_row.get("Net Credit", 0.5)).replace("$", "")) if "Net Credit" in top_row else 0.50

    # 確保 Short Strike > Long Strike (Bull Put Spread 特性)
    if leg1_strike < leg2_strike:
        leg1_strike, leg2_strike = leg2_strike, leg1_strike

    spread_info = {
        "symbol": symbol,
        "strategy": "Bull Put Spread",
        "action": "SELL",
        "exp_date": exp_date_norm,
        "exp_date_disp": format_exp_date_display(exp_date_norm),
        "leg1_strike": leg1_strike,
        "leg2_strike": leg2_strike,
        "net_credit": net_credit,
        "report_text": report_text,
    }
    return spread_info


def save_bull_put_report_and_notify(report_text, spread_info):
    """儲存 Bull Put 報告並推播至 LINE"""
    now = datetime.datetime.now()
    ts_str = now.strftime("%Y-%m-%d_%H%M%S")
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    archive_fname = f"latest_bull_put_{ts_str}.txt"
    archive_path = os.path.join(REPORTS_DIR, archive_fname)
    latest_path = os.path.join(BARCHART_DIR, "latest_bull_put.txt")

    content = f"==分析時間：{now_str}==\n{report_text}\n"

    for p in [archive_path, latest_path]:
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception:
            pass

    print(f"[INFO] [BullPutSkill] 完整報告已存檔至：{archive_path}")

    # 發送 LINE 推播
    if send_push_message:
        sym = spread_info["symbol"]
        exp = spread_info["exp_date_disp"]
        s1 = spread_info["leg1_strike"]
        s2 = spread_info["leg2_strike"]
        nc = spread_info["net_credit"]

        line_msg = f"""🛡️【Barchart Bull Put 垂直價差最佳推薦】
🕒 時間：{now_str}
📋 標的：{sym} Bull Put Spread
📅 到期日：{exp}
📉 組合：賣出 Short Put ${s1} / 買入 Long Put ${s2}
💵 預估權利金收入：${nc:.2f}

📁 完整報告：{archive_fname}"""
        print(f"[INFO] [BullPutSkill] 正在發送 LINE 推播通知...")
        ok = send_push_message(line_msg.strip())
        if ok:
            print(f"[SUCCESS] [BullPutSkill] ✅ LINE 推播成功！")

    return archive_path


# ==============================================================================
# 2. 主控管線入口 (Pipeline Orchestrator with Skills)
# ==============================================================================
def run_bull_put_pipeline(
    headless=False,
    dry_run=False,
    skip_download=False,
    skip_order=False,
    retries=3,
    #no_line=False,
    no_line=True,
):
    """
    執行 Bull Put 完整模組化技能流程：
      (1) 下載 Bull Put Spread 官方 CSV
      (2) 調用 Gemini AI 量化分析精選 1 檔最佳垂直價差
      (3) 調用 ScaleInOrderSkill 下單並處理 Error 201 兩側衝突防禦
    """
    global send_push_message
    if no_line:
        send_push_message = None

    start_time = time.time()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("\n" + "=" * 70)
    print(f"🌟 Barchart Bull Put 垂直價差 AI 選股與自動下單系統啟動 ({now_str})")
    print("=" * 70)

    driver = None
    bp_csv = None

    try:
        # ======================================================================
        # (1) 步驟 1: 下載 Bull Put Spread 官方篩選器數據
        # ======================================================================
        print("\n📥 【步驟 1】取得 Bull Put Spread 官方 CSV 表格")
        print("-" * 70)
        if not skip_download:
            account, password = load_credentials_from_env(ENV_FILE)
            driver = init_driver(download_dir=BARCHART_DIR, profile_dir=PROFILE_DIR, headless=headless)
            login_if_needed(driver, account, password)
            bp_csv = download_bull_put_csv(driver, target_dir=BARCHART_DIR)
        else:
            candidates = glob.glob(os.path.join(BARCHART_DIR, "*bull-put*.csv"))
            if not candidates:
                candidates = glob.glob(os.path.join(OLD_DIR, "*bull-put*.csv"))
            if candidates:
                candidates.sort(key=os.path.getmtime, reverse=True)
                bp_csv = candidates[0]
                print(f"[INFO] (跳過下載) 使用現存 Bull Put CSV: {bp_csv}")
            else:
                raise FileNotFoundError("未找到任何現存 Bull Put Spread CSV 檔案。")

        # 關閉瀏覽器釋放資源
        if driver:
            try:
                driver.quit()
                driver = None
            except Exception:
                pass

        # ======================================================================
        # (2) 步驟 2: 清洗數據並調用 Gemini AI 深度量化分析
        # ======================================================================
        print("\n🎯 【步驟 2】清洗數據並呼叫 Gemini AI 精選唯一最佳 Bull Put Spread")
        print("-" * 70)
        api_key = load_gemini_api_key(ENV_FILE) if load_gemini_api_key else None
        top_df = clean_and_prepare_bull_put_data(bp_csv)
        prompt = build_bull_put_prompt(top_df)

        report_text = None
        for attempt in range(1, retries + 1):
            print(f"[INFO] [BullPutSkill] 正在向 Gemini 請求 Bull Put 深度量化分析 (嘗試第 {attempt}/{retries} 次)...")
            try:
                if call_gemini_for_skill:
                    res = call_gemini_for_skill(prompt=prompt, api_key=api_key, min_chars=500, max_output_tokens=40096)
                    if res and len(res.strip()) > 300:
                        report_text = res
                        print(f"[SUCCESS] [BullPutSkill] ✅ 第 {attempt} 次成功獲得 Bull Put 最佳量化評選報告！")
                        break
            except Exception as e:
                print(f"[WARN] [BullPutSkill] 第 {attempt} 次呼叫異常: {e}")
            time.sleep(2)

        if not report_text:
            print("[WARN] [BullPutSkill] AI 分析未果，啟動排行榜首位量化備援機制...")
            top_row = top_df.iloc[0]
            report_text = f"""#### 📌 【最佳 Bull Put 垂直價差推薦 (規則型備援)】
- **推薦標的代號 (Symbol)**: {top_row['Symbol']}
- **標的現價 (Price)**: ${top_row.get('Price~', 0)}
- **建議策略**: Bull Put Spread (垂直賣權信用價差)
- **到期日 (Exp Date)**: {top_row['Exp Date']}
- **到期天數 (DTE)**: {top_row.get('DTE', 30)} 天
- **賣出下翼 (Short Put Leg 1)**: 履約價 ${top_row['Leg1 Strike']} @ Bid ${top_row.get('Leg1 Bid', 0)}
- **買入保護 (Long Put Leg 2)**: 履約價 ${top_row['Leg2 Strike']} @ Ask ${top_row.get('Leg2 Ask', 0)}
- **淨權利金收入 (Net Credit)**: ${top_row.get('Net Credit', 0.5)}
- **進場推薦理由與深度量化分析**:
  1. 系統自動由 Barchart 官方篩選器按跌破機率昇序與高性價比綜合排名首位自動挑選。
"""

        spread_info = parse_best_bull_put(report_text, top_df)
        save_bull_put_report_and_notify(report_text, spread_info)

        print(f"\n✨ 【Bull Put 精選價差組合】: {spread_info['symbol']} Exp={spread_info['exp_date_disp']} 賣出 ${spread_info['leg1_strike']} Put / 買入 ${spread_info['leg2_strike']} Put (預期收入: ${spread_info['net_credit']:.2f})")

        # ======================================================================
        # (3) 步驟 3: 調用 ScaleInOrderSkill 下單並處理 Error 201 兩側衝突防禦
        # ======================================================================
        print("\n⚡ 【步驟 3】連線 IBKR 執行 Bull Put Spread 組合單 (整合 ScaleInOrderSkill)")
        print("-" * 70)

        if skip_order:
            print("[INFO] 已略過 IBKR 下單步驟 (--skip-order)")
            return True

        cfg = load_env_settings(ENV_FILE)
        host = cfg.get("IB_HOST", "127.0.0.1")
        port = cfg.get("IB_PORT", 4001)
        target_account = cfg.get("IB_TARGET_ACCOUNT", "")

        ib = None
        try:
            ib = create_fast_ib_connection(host=host, port=port)
            symbol = spread_info["symbol"]
            exp_date = spread_info["exp_date"]
            s1 = spread_info["leg1_strike"]
            s2 = spread_info["leg2_strike"]
            credit = float(spread_info.get("net_credit", 0.5) or 0.5)

            # 建立並驗證期權雙腿合約
            short_put = Option(symbol, exp_date, s1, "P", "SMART")
            long_put = Option(symbol, exp_date, s2, "P", "SMART")

            qualified = ib.qualifyContracts(short_put, long_put)
            if not short_put.conId or not long_put.conId:
                print(f"[WARN] ⚠️ 期權腿合約在 IBKR 查無定義 (short: {short_put.conId}, long: {long_put.conId})，略過下單。")
                return False

            print(f"[SUCCESS] ✅ 雙腿合約通過 IBKR 驗證: Short Put conId={short_put.conId}, Long Put conId={long_put.conId}")

            # 建立 BAG 垂直價差組合單
            c1 = ComboLeg(conId=short_put.conId, ratio=1, action="SELL", exchange="SMART")
            c2 = ComboLeg(conId=long_put.conId, ratio=1, action="BUY", exchange="SMART")
            bag_contract = Contract(
                symbol=symbol,
                secType="BAG",
                currency="USD",
                exchange="SMART",
                comboLegs=[c1, c2],
            )

            # 規劃 Attached 停利停損價格 (賣出價差：0.5倍停利，2倍停損)
            take_profit_price = round(credit * 0.5, 2)
            stop_loss_price = round(credit * 2.0, 2)

            print(f"  -> 附屬單規劃:")
            print(f"     • 🎯 Profit Taker (50%停利單): ${take_profit_price:.2f}")
            print(f"     • 🛑 Stop Loss    (2倍停損單):  ${stop_loss_price:.2f}")

            # 調用 ScaleInOrderSkill 執行下單與加碼防衝突處理
            scale_skill = ScaleInOrderSkill(ib_instance=ib)
            order_res = scale_skill.execute(
                contract=bag_contract,
                symbol=symbol,
                action="SELL",
                quantity=1.0,
                credit=credit,
                take_profit_price=take_profit_price,
                stop_loss_price=stop_loss_price,
                target_account=target_account,
                dry_run=dry_run,
            )

            print(f"[INFO] ScaleInOrderSkill 處理結果: {order_res.get('status')} (總口數: {order_res.get('total_qty', 1)} 口)")

        finally:
            if ib and ib.isConnected():
                ib.disconnect()
                print("[INFO] 已安全斷開 IBKR 連線。")

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    elapsed = time.time() - start_time
    print("\n" + "#" * 70)
    print(f"# ✅ Barchart Bull Put 垂直價差自動化流程完成！(總耗時: {elapsed:.1f} 秒)")
    print("#" * 70 + "\n")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Barchart Bull Put 垂直價差 AI 自動化選股與下單流水線 (Skills 架構版)")
    parser.add_argument("--headless", action="store_true", help="以無瀏覽器視窗模式執行")
    parser.add_argument("--dry-run", action="store_true", help="以模擬模式執行下單（預查市價並計算附屬單，不實際送單至 IBKR）")
    parser.add_argument("--skip-download", action="store_true", help="跳過下載步驟，使用現存 CSV")
    parser.add_argument("--skip-order", action="store_true", help="跳過向 IBKR 下單步驟")
    parser.add_argument("--retries", type=int, default=3, help="Gemini 請求最大重試次數")
    parser.add_argument("--no-line", action="store_true", help="不發送 LINE 通知")
    parser.add_argument("--no-sleep", action="store_true", help="執行完畢後直接退出，不常駐 sleep")
    args = parser.parse_args()

    run_bull_put_pipeline(
        headless=args.headless,
        dry_run=args.dry_run,
        skip_download=args.skip_download,
        skip_order=args.skip_order,
        retries=args.retries,
        no_line=args.no_line,
    )

    if not args.no_sleep and not args.dry_run and sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
        try:
            time.sleep(60 * 60 * 20)
        except KeyboardInterrupt:
            print("\n[INFO] 使用者手動中斷常駐程序。")
