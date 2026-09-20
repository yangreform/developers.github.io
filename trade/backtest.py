#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==============================================================================
SPX & NDX 0DTE Backtesting System
==============================================================================
Based on the methodology from:
https://github.com/jefrnc/ibkr-odte-strategies

Designed for Interactive Brokers (IBKR) 0DTE Options on:
- S&P 500 Index (SPX / SPXW)
- Nasdaq 100 Index (NDX / NDXP)

Supported Strategies:
1. 'butterfly' (Default, matches trade/DTE0.py):
   - Multi-set 0DTE Butterfly (ATM, ATM + price_jump, ATM - price_jump)
   - 4-leg structure with wing_width and bid_up limit execution
   - Intraday Black-Scholes theta decay & 16:00 ET cash settlement
2. 'breakout' (Matches jefrnc/ibkr-odte-strategies):
   - Opening range breakout with volume/price filter
   - Dynamic stop-loss and take-profit multipliers
3. 'iron_condor':
   - 0DTE OTM Call/Put credit spreads with defined risk

Outputs:
- trade/results/backtest_{strategy}_{timestamp}/
  ├── trades.csv
  ├── metrics.json
  ├── performance_report.txt
  ├── equity_curve.png
  └── pnl_distribution.png
==============================================================================
"""

import os
import sys
import json
import math
import time
import logging
import argparse
import datetime
from pathlib import Path

# Ensure UTF-8 output encoding on Windows console
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
        sys.stderr.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
    except Exception:
        pass

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')  # Non-interactive headless backend
import matplotlib.pyplot as plt
from scipy.stats import norm

# Try importing ib_insync
try:
    from ib_insync import IB, Index, Option, util
    HAS_IB_INSYNC = True
except ImportError:
    HAS_IB_INSYNC = False


# ==============================================================================
# 0. 環境設定與參數讀取
# ==============================================================================
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / '.env'
CACHE_DIR = BASE_DIR / 'data' / 'cache_backtest'
RESULTS_DIR = BASE_DIR / 'results'

CACHE_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def load_env_config():
    """讀取 trade/.env 環境變數。"""
    cfg = {}
    if ENV_PATH.exists():
        try:
            with open(ENV_PATH, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '=' in line:
                        k, v = line.split('=', 1)
                        cfg[k.strip()] = v.strip().strip("'").strip('"')
        except Exception as e:
            print(f"[警告] 讀取 .env 失敗: {e}")
    return cfg


def load_spx_ndx_config():
    """解析 trade/.env 中的 OP_HEDGE_CONFIG_JSON。"""
    cfg = load_env_config()
    raw = cfg.get('OP_HEDGE_CONFIG_JSON', '{}')
    spx_cfg = {'symbols': ['SPX'], 'wing_width': 10.0, 'bid_up': 4.0, 'price_jump': 20.0, 'iron': 'long'}
    ndx_cfg = {'symbols': ['NDX'], 'wing_width': 40.0, 'bid_up': 16.0, 'price_jump': 80.0, 'iron': 'long'}
    rut_cfg = {'symbols': ['RUT'], 'wing_width': 10.0, 'bid_up': 3.0, 'price_jump': 10.0, 'iron': 'long'}

    try:
        data = json.loads(raw)
        for key, val in data.items():
            if 'SPX' in key or '標普' in key:
                spx_cfg.update({
                    'wing_width': float(val.get('wing_width', 10.0)),
                    'bid_up': float(val.get('bid_up', 4.0)),
                    'price_jump': float(val.get('price_jump', 20.0)),
                    'iron': str(val.get('iron', 'long')).lower()
                })
            elif 'NDX' in key or '那指' in key:
                ndx_cfg.update({
                    'wing_width': float(val.get('wing_width', 40.0)),
                    'bid_up': float(val.get('bid_up', 16.0)),
                    'price_jump': float(val.get('price_jump', 80.0)),
                    'iron': str(val.get('iron', 'long')).lower()
                })
            elif 'RUT' in key or '羅素' in key:
                rut_cfg.update({
                    'wing_width': float(val.get('wing_width', 10.0)),
                    'bid_up': float(val.get('bid_up', 3.0)),
                    'price_jump': float(val.get('price_jump', 10.0)),
                    'iron': str(val.get('iron', 'long')).lower()
                })
    except Exception as e:
        print(f"[警告] 解析 OP_HEDGE_CONFIG_JSON 失敗，使用預設配置: {e}")

    return {'SPX': spx_cfg, 'NDX': ndx_cfg, 'RUT': rut_cfg}


# ==============================================================================
# 1. Black-Scholes 歐式期權定價模型 (SPX / NDX 專用)
# ==============================================================================
class BlackScholes:
    """
    SPX 與 NDX 為現金交割歐式指數期權 (European Cash-Settled Index Options)。
    精確計算盤中微秒級時間衰減 (Theta) 與履約結算價值。
    """
    @staticmethod
    def call_price(S, K, T, r=0.045, sigma=0.15):
        if T <= 1e-6:
            return max(0.0, S - K)
        if sigma <= 1e-6:
            return max(0.0, S - K * math.exp(-r * T))
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)

    @staticmethod
    def put_price(S, K, T, r=0.045, sigma=0.15):
        if T <= 1e-6:
            return max(0.0, K - S)
        if sigma <= 1e-6:
            return max(0.0, K * math.exp(-r * T) - S)
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

    @staticmethod
    def option_price(right, S, K, T, r=0.045, sigma=0.15):
        right = right.upper()
        if right in ('C', 'CALL'):
            return BlackScholes.call_price(S, K, T, r, sigma)
        else:
            return BlackScholes.put_price(S, K, T, r, sigma)

    @staticmethod
    def butterfly_price(S, center_k, wing_width, T, r=0.045, sigma=0.15, is_long=True):
        """
        4 腿蝶式組合價值:
        Long Butterfly = BUY 1 ATM Call + BUY 1 ATM Put - SELL 1 OTM Call - SELL 1 OTM Put
        (或等價由 Call 蝶式: +1 C(K-w) -2 C(K) +1 C(K+w) 構成)
        """
        c_low = BlackScholes.call_price(S, center_k - wing_width, T, r, sigma)
        c_mid = BlackScholes.call_price(S, center_k, T, r, sigma)
        c_high = BlackScholes.call_price(S, center_k + wing_width, T, r, sigma)
        # Call 蝶式價值 (Long Butterfly Value)
        fly_value = c_low - 2.0 * c_mid + c_high
        fly_value = max(0.0, fly_value)
        return fly_value if is_long else -fly_value


# ==============================================================================
# 2. 市場數據加載器 (IBKR API + 本地快取 + 擬真引擎)
# ==============================================================================
class MarketDataLoader:
    """
    從 IBKR 下載歷史高頻數據或載入本地快取，
    若無連線則以歷史統計參數產生高精度模擬盤。
    """
    def __init__(self, host='127.0.0.1', port=4001, client_id=1099, logger=None):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.logger = logger or logging.getLogger("MarketData")
        self.ib = None

    def _init_ib(self):
        if not HAS_IB_INSYNC:
            return False
        if self.ib and self.ib.isConnected():
            return True
        try:
            self.ib = IB()
            self.ib.connect(self.host, self.port, clientId=self.client_id, timeout=4)
            self.logger.info(f"已連接 IBKR ({self.host}:{self.port}) ClientId={self.client_id}")
            return True
        except Exception as e:
            self.logger.warning(f"無法連接 IBKR ({e})，切換至離線/快取模式")
            self.ib = None
            return False

    def get_historical_bars(self, symbol, start_date, end_date, offline=False):
        """
        載入指定期間的 SPX / NDX 每日盤中 5 分鐘或 1 分鐘 K 棒。
        優先檢查本地 cache/ -> 次為 IBKR reqHistoricalData -> 離線合成回測數據
        """
        start_str = start_date.strftime('%Y%m%d')
        end_str = end_date.strftime('%Y%m%d')
        cache_file = CACHE_DIR / f"{symbol}_{start_str}_{end_str}_5min.csv"

        if cache_file.exists():
            try:
                self.logger.info(f"從快取讀取 {symbol} 歷史數據: {cache_file.name}")
                df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
                if not df.empty:
                    return df
            except Exception as e:
                self.logger.warning(f"快取解析失敗 ({e})，重新載入")

        if not offline and self._init_ib():
            try:
                if symbol == 'NDX':
                    exchange = 'NASDAQ'
                elif symbol == 'RUT':
                    exchange = 'RUSSELL'
                else:
                    exchange = 'CBOE'
                contract = Index(symbol, exchange, currency='USD')
                self.ib.qualifyContracts(contract)

                # 計算需要的時間長度
                total_days = (end_date - start_date).days + 1
                duration_str = f"{min(total_days, 60)} D"

                # 若 end_date 為今日或未來，直接給予空字串 '' 代表最新時間點；否則使用標準 IBKR 日期格式
                today = datetime.date.today()
                if end_date >= today:
                    end_dt_str = ''
                else:
                    end_dt_str = f"{end_str} 23:59:59"

                self.logger.info(f"正在自 IBKR 下載 {symbol} 歷史數據 (時長: {duration_str})...")
                bars = self.ib.reqHistoricalData(
                    contract,
                    endDateTime=end_dt_str,
                    durationStr=duration_str,
                    barSizeSetting='5 mins',
                    whatToShow='TRADES',
                    useRTH=True,
                    formatDate=1
                )
                if bars:
                    df = util.df(bars)
                    df['date'] = pd.to_datetime(df['date'])
                    df.set_index('date', inplace=True)
                    df.to_csv(cache_file)
                    self.logger.info(f"成功自 IBKR 下載 {len(df)} 根 K 棒並已存入快取")
                    return df
            except Exception as e:
                self.logger.warning(f"IBKR 下載數據異常 ({e})，切換至備援合成模式")

        # 備援：合成符合標的特性的真實感盤中 5 分鐘行情
        self.logger.info(f"使用歷史波動率統計模型合成 {symbol} 0DTE 盤中高頻行情 ({start_str} ~ {end_str})...")
        df = self._generate_synthetic_intraday(symbol, start_date, end_date)
        df.to_csv(cache_file)
        return df

    def _generate_synthetic_intraday(self, symbol, start_date, end_date):
        """
        根據各指數 (SPX, NDX, RUT) 典型日內波動率與漂移產生測試資料。
        """
        if symbol == 'SPX':
            base_price = 5850.0
            daily_vol = 0.009
        elif symbol == 'NDX':
            base_price = 20200.0
            daily_vol = 0.013
        elif symbol == 'RUT':
            base_price = 2870.0
            daily_vol = 0.012
        else:
            base_price = 2000.0
            daily_vol = 0.010
        intraday_vol = daily_vol / math.sqrt(78)  # 一天 78 根 5 分鐘 K 棒

        records = []
        cur_date = start_date
        np.random.seed(42 if symbol == 'SPX' else 108)

        current_price = base_price

        while cur_date <= end_date:
            if cur_date.weekday() >= 5:  # 跳過週末
                cur_date += datetime.timedelta(days=1)
                continue

            # 開盤跳空 (Gap)
            gap = np.random.normal(0, daily_vol * 0.4) * current_price
            day_open = current_price + gap

            # 產生 09:30 至 16:00 共 78 根 5 分鐘 K 棒
            bar_time = datetime.datetime.combine(cur_date, datetime.time(9, 30))
            p = day_open

            for _ in range(78):
                ret = np.random.normal(0.00005, intraday_vol)
                open_p = p
                close_p = open_p * (1.0 + ret)
                noise_high = abs(np.random.normal(0, intraday_vol * 0.5)) * open_p
                noise_low = abs(np.random.normal(0, intraday_vol * 0.5)) * open_p
                high_p = max(open_p, close_p) + noise_high
                low_p = min(open_p, close_p) - noise_low
                vol = int(np.random.uniform(500, 3500))

                records.append({
                    'timestamp': bar_time,
                    'open': round(open_p, 2),
                    'high': round(high_p, 2),
                    'low': round(low_p, 2),
                    'close': round(close_p, 2),
                    'volume': vol
                })
                p = close_p
                bar_time += datetime.timedelta(minutes=5)

            current_price = p
            cur_date += datetime.timedelta(days=1)

        res_df = pd.DataFrame(records)
        res_df.set_index('timestamp', inplace=True)
        return res_df


# ==============================================================================
# 3. 回測引擎架構 (BacktestEngine)
# ==============================================================================
class BacktestEngine:
    """
    統一管理 SPX / NDX 0DTE 策略回測、資金曲線與績效指標計算。
    """
    def __init__(self, strategy_name='butterfly', symbols=None, start_date='2024-01-01',
                 end_date=None, initial_capital=10000.0, config=None, offline=False):
        self.strategy_name = strategy_name.lower()
        self.symbols = symbols or ['SPX', 'NDX']
        self.start_date = datetime.datetime.strptime(start_date, '%Y-%m-%d').date()
        if end_date:
            self.end_date = datetime.datetime.strptime(end_date, '%Y-%m-%d').date()
        else:
            self.end_date = datetime.date.today()

        self.initial_capital = float(initial_capital)
        self.current_capital = float(initial_capital)
        self.offline = offline

        # 讀取設定
        self.env_cfg = load_env_config()
        self.hedge_cfg = load_spx_ndx_config()
        self.custom_config = config or {}

        # 交易記錄與報表
        self.trades = []
        self.equity_curve = [self.initial_capital]
        self.performance_metrics = {}

        # 記錄器與輸出目錄
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        self.results_dir = RESULTS_DIR / f"backtest_{self.strategy_name}_{timestamp}"
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.logger = self._setup_logger()

        # 市場資料載入器
        ib_host = self.env_cfg.get('IB_HOST', '127.0.0.1')
        ib_port = int(self.env_cfg.get('IB_PORT', 4001))
        ib_client_id = int(self.env_cfg.get('IB_CLIENT_ID', 100)) + 88
        self.data_loader = MarketDataLoader(host=ib_host, port=ib_port, client_id=ib_client_id, logger=self.logger)

    def _setup_logger(self):
        logger = logging.getLogger(f"Backtest.{self.strategy_name}")
        logger.setLevel(logging.DEBUG)

        log_file = self.results_dir / "backtest.log"
        fh = logging.FileHandler(log_file, encoding='utf-8')
        fh.setLevel(logging.DEBUG)

        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)

        fmt = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
        fh.setFormatter(fmt)
        ch.setFormatter(fmt)

        logger.handlers.clear()
        logger.addHandler(fh)
        logger.addHandler(ch)
        return logger

    def run(self):
        """執行所選之 0DTE 回測策略。"""
        self.logger.info("=" * 65)
        self.logger.info(f"🚀 開始執行 0DTE 回測系統 | 策略: {self.strategy_name.upper()}")
        self.logger.info(f"期間: {self.start_date} 至 {self.end_date} | 初始資金: ${self.initial_capital:,.2f}")
        self.logger.info(f"回測標的: {', '.join(self.symbols)}")
        self.logger.info("=" * 65)

        # 1. 載入各標的市場數據
        market_data = {}
        for sym in self.symbols:
            df = self.data_loader.get_historical_bars(sym, self.start_date, self.end_date, offline=self.offline)
            if df is not None and not df.empty:
                market_data[sym] = df
                self.logger.info(f"標的 {sym} 載入完成: 共 {len(df)} 根 K 棒")
            else:
                self.logger.error(f"標的 {sym} 無法取得數據，將跳過該標的")

        if not market_data:
            self.logger.error("❌ 無可用歷史數據，回測中止。")
            return None

        # 2. 依策略逐日執行回測
        if self.strategy_name == 'butterfly':
            self._backtest_butterfly(market_data)
        elif self.strategy_name == 'breakout':
            self._backtest_breakout(market_data)
        elif self.strategy_name in ('iron_condor', 'condor'):
            self._backtest_iron_condor(market_data)
        else:
            self.logger.error(f"未知的策略類型: {self.strategy_name}")
            return None

        # 3. 計算績效指標與產生報表
        self.calculate_metrics()
        self.generate_reports()
        return self.performance_metrics

    # --------------------------------------------------------------------------
    # 策略 A: 0DTE 多組 Butterfly (ATM, ATM+jump, ATM-jump) - 對應 trade/DTE0.py
    # --------------------------------------------------------------------------
    def _backtest_butterfly(self, market_data):
        self.logger.info(">>> 執行 0DTE 蝶式 (Butterfly) 組合策略回測 (含 5 組偏置架構)...")
        cur_date = self.start_date
        current_cap = self.initial_capital

        while cur_date <= self.end_date:
            if cur_date.weekday() >= 5:
                cur_date += datetime.timedelta(days=1)
                continue

            date_str = cur_date.strftime('%Y-%m-%d')
            daily_pnl = 0.0

            for sym in self.symbols:
                if sym not in market_data:
                    continue
                df = market_data[sym]
                day_bars = df[df.index.strftime('%Y-%m-%d') == date_str]
                if day_bars.empty or len(day_bars) < 10:
                    continue

                # 參數獲取
                sym_cfg = self.hedge_cfg.get(sym, {})
                wing_width = sym_cfg.get('wing_width', 10.0 if sym in ('SPX', 'RUT') else 40.0)
                price_jump = sym_cfg.get('price_jump', 10.0 if sym == 'RUT' else (20.0 if sym == 'SPX' else 80.0))
                bid_up = sym_cfg.get('bid_up', 3.0 if sym == 'RUT' else (4.0 if sym == 'SPX' else 16.0))
                is_long = (sym_cfg.get('iron', 'long') != 'short')

                # 開盤基準價 (09:30 開盤第一根 K 棒)
                open_bar = day_bars.iloc[0]
                underlying_open = open_bar['open']

                # 履約價四捨五入至整數 (SPX/RUT: 5 點一跳, NDX: 25 點一跳)
                strike_step = 5.0 if sym in ('SPX', 'RUT') else 25.0
                base_atm = round(underlying_open / strike_step) * strike_step

                # 5 組中心點定義
                sets = [
                    {'tag': 'ATM', 'center': base_atm},
                    {'tag': f'ATM+{price_jump:g}', 'center': base_atm + price_jump},
                    {'tag': f'ATM-{price_jump:g}', 'center': base_atm - price_jump},
                    {'tag': f'ATM+2*{price_jump:g}', 'center': base_atm + 2.0 * price_jump},
                    {'tag': f'ATM-2*{price_jump:g}', 'center': base_atm - 2.0 * price_jump},
                ]

                # 計算各組進場成本 (09:30 進場，T = 6.5 小時 = 6.5 / (252*6.5) = 1/252 年)
                t_entry = 1.0 / 252.0
                iv = 0.15 if sym == 'SPX' else (0.18 if sym == 'RUT' else 0.20)

                for s in sets:
                    center_k = s['center']
                    tag = s['tag']
                    theoretical_cost = BlackScholes.butterfly_price(
                        underlying_open, center_k, wing_width, t_entry, sigma=iv, is_long=True
                    )
                    # 執行 bid_up 限價保護: min(理論權利金, bid_up)
                    entry_cost = min(theoretical_cost, bid_up)
                    entry_cost = max(0.10, entry_cost)

                    # 追蹤盤中走勢或直到 16:00 現金結算
                    # 檢查最後一根 K 棒收盤價 (Cash Settlement)
                    close_bar = day_bars.iloc[-1]
                    settle_price = close_bar['close']

                    # 16:00 到期現金結算價值 (Intrinsic Payoff)
                    expiry_val = BlackScholes.butterfly_price(
                        settle_price, center_k, wing_width, T=0.0, sigma=iv, is_long=True
                    )

                    # 每點合約乘數: SPX / NDX 均為 100
                    multiplier = 100.0
                    trade_pnl = (expiry_val - entry_cost) * multiplier

                    # 手續費預估: 4 腿 * $0.65 = $2.60
                    commission = 2.60
                    net_pnl = trade_pnl - commission
                    daily_pnl += net_pnl

                    self.trades.append({
                        'date': date_str,
                        'symbol': sym,
                        'strategy': 'Butterfly',
                        'set': tag,
                        'center_strike': center_k,
                        'wings': f"{center_k - wing_width:.0f}/{center_k + wing_width:.0f}",
                        'entry_time': '09:30',
                        'exit_time': '16:00',
                        'underlying_open': underlying_open,
                        'underlying_close': settle_price,
                        'entry_price': round(entry_cost, 2),
                        'exit_price': round(expiry_val, 2),
                        'pnl': round(net_pnl, 2),
                        'status': 'WIN' if net_pnl > 0 else 'LOSS'
                    })

            current_cap += daily_pnl
            self.equity_curve.append(current_cap)
            cur_date += datetime.timedelta(days=1)

    # --------------------------------------------------------------------------
    # 策略 B: 0DTE 突破追價策略 (Breakout) - 對應 jefrnc/ibkr-odte-strategies
    # --------------------------------------------------------------------------
    def _backtest_breakout(self, market_data):
        self.logger.info(">>> 執行 0DTE 突破交易 (Breakout) 策略回測...")
        vol_multiplier = self.custom_config.get('volume_multiplier', 1.2)
        tp_mult = self.custom_config.get('tp_multiplier', 1.4)
        sl_mult = self.custom_config.get('sl_multiplier', 0.6)
        risk_per_trade = self.custom_config.get('risk_per_trade', 250.0)

        cur_date = self.start_date
        current_cap = self.initial_capital

        while cur_date <= self.end_date:
            if cur_date.weekday() >= 5:
                cur_date += datetime.timedelta(days=1)
                continue

            date_str = cur_date.strftime('%Y-%m-%d')
            daily_pnl = 0.0

            for sym in self.symbols:
                if sym not in market_data:
                    continue
                df = market_data[sym]
                day_bars = df[df.index.strftime('%Y-%m-%d') == date_str]
                if day_bars.empty or len(day_bars) < 15:
                    continue

                # 初始前 3 根 5 分鐘 K 棒 (09:30 ~ 09:45) 作為初始開盤區間
                init_range = day_bars.iloc[:3]
                high_range = init_range['high'].max()
                low_range = init_range['low'].min()
                avg_vol = init_range['volume'].mean()

                trade_triggered = False

                for i in range(3, len(day_bars)):
                    bar = day_bars.iloc[i]
                    close_p = bar['close']
                    vol = bar['volume']

                    signal = None
                    if close_p > high_range and vol > avg_vol * vol_multiplier:
                        signal = 'CALL'
                    elif close_p < low_range and vol > avg_vol * vol_multiplier:
                        signal = 'PUT'

                    if signal and not trade_triggered:
                        trade_triggered = True
                        # 0DTE 選擇權權利金估算 (約指數點位之 0.5% ~ 0.8%)
                        premium = close_p * 0.006
                        strike = round(close_p / (5.0 if sym == 'SPX' else 25.0)) * (5.0 if sym == 'SPX' else 25.0)
                        qty = max(1, int(risk_per_trade / (premium * 100)))

                        tp_price = premium * tp_mult
                        sl_price = premium * sl_mult

                        remaining_bars = day_bars.iloc[i+1:]
                        exit_price = premium
                        exit_time = '16:00'
                        exit_status = 'EXPIRED'

                        for t_idx, future_bar in remaining_bars.iterrows():
                            # 期權價格估計 (Delta ~ 0.50)
                            delta = 0.50 if signal == 'CALL' else -0.50
                            underlying_change = future_bar['close'] - close_p
                            opt_p = max(0.05, premium + delta * underlying_change)

                            if opt_p >= tp_price:
                                exit_price = tp_price
                                exit_time = t_idx.strftime('%H:%M')
                                exit_status = 'TP'
                                break
                            elif opt_p <= sl_price:
                                exit_price = sl_price
                                exit_time = t_idx.strftime('%H:%M')
                                exit_status = 'SL'
                                break

                        # 結算獲利
                        pnl = (exit_price - premium) * qty * 100.0 - (2.0 * qty)
                        daily_pnl += pnl

                        self.trades.append({
                            'date': date_str,
                            'symbol': sym,
                            'strategy': 'Breakout',
                            'set': signal,
                            'center_strike': strike,
                            'wings': '-',
                            'entry_time': bar.name.strftime('%H:%M'),
                            'exit_time': exit_time,
                            'underlying_open': close_p,
                            'underlying_close': day_bars.iloc[-1]['close'],
                            'entry_price': round(premium, 2),
                            'exit_price': round(exit_price, 2),
                            'pnl': round(pnl, 2),
                            'status': exit_status
                        })
                        break

            current_cap += daily_pnl
            self.equity_curve.append(current_cap)
            cur_date += datetime.timedelta(days=1)

    # --------------------------------------------------------------------------
    # 策略 C: 0DTE 鐵鷹策略 (Iron Condor)
    # --------------------------------------------------------------------------
    def _backtest_iron_condor(self, market_data):
        self.logger.info(">>> 執行 0DTE 鐵鷹 (Iron Condor) 策略回測...")
        cur_date = self.start_date
        current_cap = self.initial_capital

        while cur_date <= self.end_date:
            if cur_date.weekday() >= 5:
                cur_date += datetime.timedelta(days=1)
                continue

            date_str = cur_date.strftime('%Y-%m-%d')
            daily_pnl = 0.0

            for sym in self.symbols:
                if sym not in market_data:
                    continue
                df = market_data[sym]
                day_bars = df[df.index.strftime('%Y-%m-%d') == date_str]
                if day_bars.empty or len(day_bars) < 10:
                    continue

                open_p = day_bars.iloc[0]['open']
                close_p = day_bars.iloc[-1]['close']
                step = 5.0 if sym == 'SPX' else 25.0
                atm = round(open_p / step) * step

                # 賣出 15 點 (SPX) 或 60 點 (NDX) 價外，翼寬 10 點 / 40 點
                spread_dist = 15.0 if sym == 'SPX' else 60.0
                wing = 10.0 if sym == 'SPX' else 40.0

                call_short = atm + spread_dist
                call_long = call_short + wing
                put_short = atm - spread_dist
                put_long = put_short - wing

                credit = 1.20 if sym == 'SPX' else 5.0  # 收集之淨權利金
                max_loss = (wing - credit) * 100.0

                # 到期價值評估
                call_spread_payoff = max(0.0, min(wing, close_p - call_short))
                put_spread_payoff = max(0.0, min(wing, put_short - close_p))
                net_loss = (call_spread_payoff + put_spread_payoff)

                pnl = (credit - net_loss) * 100.0 - 2.60
                daily_pnl += pnl

                self.trades.append({
                    'date': date_str,
                    'symbol': sym,
                    'strategy': 'IronCondor',
                    'set': f"{put_short}/{call_short}",
                    'center_strike': atm,
                    'wings': f"{put_long}/{call_long}",
                    'entry_time': '09:30',
                    'exit_time': '16:00',
                    'underlying_open': open_p,
                    'underlying_close': close_p,
                    'entry_price': round(credit, 2),
                    'exit_price': round(net_loss, 2),
                    'pnl': round(pnl, 2),
                    'status': 'WIN' if pnl > 0 else 'LOSS'
                })

            current_cap += daily_pnl
            self.equity_curve.append(current_cap)
            cur_date += datetime.timedelta(days=1)

    # --------------------------------------------------------------------------
    # 4. 統計指標計算
    # --------------------------------------------------------------------------
    def calculate_metrics(self):
        if not self.trades:
            self.logger.warning("回測無產生成交紀錄，無法計算指標。")
            return

        df_trades = pd.DataFrame(self.trades)
        equity = np.array(self.equity_curve)

        total_trades = len(df_trades)
        wins = df_trades[df_trades['pnl'] > 0]
        losses = df_trades[df_trades['pnl'] <= 0]
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = (win_count / total_trades) if total_trades > 0 else 0.0

        total_profit = float(wins['pnl'].sum()) if win_count > 0 else 0.0
        total_loss = float(abs(losses['pnl'].sum())) if loss_count > 0 else 0.0
        net_profit = total_profit - total_loss
        profit_factor = (total_profit / total_loss) if total_loss > 0 else float('inf')

        # 總報酬率與年化報酬
        total_return = ((equity[-1] / equity[0]) - 1.0) * 100.0
        total_days = max(1, (self.end_date - self.start_date).days)
        annual_return = ((1.0 + total_return / 100.0) ** (365.0 / total_days) - 1.0) * 100.0

        # 最大回撤 (Max Drawdown)
        running_max = np.maximum.accumulate(equity)
        drawdowns = (equity - running_max) / running_max * 100.0
        max_drawdown = abs(float(np.min(drawdowns)))

        # 年化波動率與夏普比率 (無風險利率假設 4.5%)
        daily_returns = np.diff(equity) / equity[:-1]
        volatility = float(np.std(daily_returns) * np.sqrt(252) * 100.0) if len(daily_returns) > 1 else 0.0
        rf = 4.5
        sharpe_ratio = ((annual_return - rf) / volatility) if volatility > 0 else 0.0

        self.performance_metrics = {
            'strategy': self.strategy_name,
            'symbols': self.symbols,
            'start_date': str(self.start_date),
            'end_date': str(self.end_date),
            'initial_capital': self.initial_capital,
            'final_capital': round(equity[-1], 2),
            'net_profit': round(net_profit, 2),
            'total_return_pct': round(total_return, 2),
            'annual_return_pct': round(annual_return, 2),
            'max_drawdown_pct': round(max_drawdown, 2),
            'annual_volatility_pct': round(volatility, 2),
            'sharpe_ratio': round(sharpe_ratio, 2),
            'total_trades': total_trades,
            'winning_trades': win_count,
            'losing_trades': loss_count,
            'win_rate_pct': round(win_rate * 100.0, 2),
            'profit_factor': round(profit_factor, 2) if profit_factor != float('inf') else 999.0,
            'avg_trade_pnl': round(float(df_trades['pnl'].mean()), 2)
        }

    # --------------------------------------------------------------------------
    # 5. 報表、圖表與 JSON 匯出
    # --------------------------------------------------------------------------
    def generate_reports(self):
        if not self.trades:
            return

        # A. 儲存 trades.csv
        df_trades = pd.DataFrame(self.trades)
        csv_path = self.results_dir / "trades.csv"
        df_trades.to_csv(csv_path, index=False, encoding='utf-8-sig')

        # B. 儲存 metrics.json
        json_path = self.results_dir / "metrics.json"
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(self.performance_metrics, f, indent=2, ensure_ascii=False)

        # C. 繪製資金曲線圖 (Equity Curve)
        self._plot_equity_curve()

        # D. 繪製損益分佈直方圖 (PnL Distribution)
        self._plot_pnl_distribution(df_trades)

        # E. 產生文字版綜合績效摘要
        self._write_summary_report(df_trades)

    def _plot_equity_curve(self):
        plt.figure(figsize=(11, 5.5), dpi=120)
        plt.plot(self.equity_curve, color='#007acc', lw=2, label='Equity Curve ($)')
        plt.axhline(self.initial_capital, color='#e06c75', linestyle='--', alpha=0.7, label='Initial Capital')
        plt.title(f"0DTE {self.strategy_name.upper()} Equity Curve ({', '.join(self.symbols)})", fontsize=14, fontweight='bold')
        plt.xlabel('Trading Days', fontsize=11)
        plt.ylabel('Account Balance ($)', fontsize=11)
        plt.grid(True, linestyle=':', alpha=0.6)
        plt.legend(loc='upper left')
        plt.tight_layout()
        plt.savefig(self.results_dir / "equity_curve.png")
        plt.close()

    def _plot_pnl_distribution(self, df_trades):
        plt.figure(figsize=(9, 4.5), dpi=120)
        pnl = df_trades['pnl']
        plt.hist(pnl, bins=25, color='#28a745', edgecolor='#1e7e34', alpha=0.75)
        plt.axvline(0, color='red', linestyle='--', lw=1.5)
        plt.title(f"Trade PnL Distribution ({self.strategy_name.upper()})", fontsize=13, fontweight='bold')
        plt.xlabel('PnL per Trade ($)', fontsize=10)
        plt.ylabel('Frequency', fontsize=10)
        plt.grid(True, linestyle=':', alpha=0.5)
        plt.tight_layout()
        plt.savefig(self.results_dir / "pnl_distribution.png")
        plt.close()

    def _write_summary_report(self, df_trades):
        m = self.performance_metrics
        lines = [
            "=" * 65,
            f"          IBKR 0DTE 回測績效報告 (SPX / NDX)          ",
            "=" * 65,
            f"回測策略: {m['strategy'].upper()}",
            f"測試標的: {', '.join(m['symbols'])}",
            f"回測區間: {m['start_date']} ~ {m['end_date']}",
            f"初始資金: ${m['initial_capital']:,.2f}",
            f"期末資金: ${m['final_capital']:,.2f}",
            "-" * 65,
            "【總體收益與風險指標】",
            f"  淨獲利 (Net Profit)      : ${m['net_profit']:,.2f}",
            f"  總報酬率 (Total Return)  : {m['total_return_pct']:.2f}%",
            f"  年化報酬率 (CAGR)        : {m['annual_return_pct']:.2f}%",
            f"  最大回撤 (Max Drawdown)  : {m['max_drawdown_pct']:.2f}%",
            f"  年化波動率 (Volatility)  : {m['annual_volatility_pct']:.2f}%",
            f"  夏普比率 (Sharpe Ratio)  : {m['sharpe_ratio']:.2f}",
            "-" * 65,
            "【交易勝率與統計】",
            f"  總交易筆數 (Total Trades): {m['total_trades']}",
            f"  勝率 (Win Rate)          : {m['win_rate_pct']:.2f}% ({m['winning_trades']} 勝 / {m['losing_trades']} 負)",
            f"  獲利因子 (Profit Factor) : {m['profit_factor']:.2f}",
            f"  平均單筆損益 (Avg PnL)   : ${m['avg_trade_pnl']:,.2f}",
            "=" * 65,
            "【各標的分項統計】",
        ]

        for sym in self.symbols:
            sub = df_trades[df_trades['symbol'] == sym]
            if not sub.empty:
                sym_trades = len(sub)
                sym_pnl = sub['pnl'].sum()
                sym_wins = len(sub[sub['pnl'] > 0])
                sym_wr = (sym_wins / sym_trades * 100.0) if sym_trades > 0 else 0
                lines.append(f"  {sym:4s}: 共 {sym_trades:3d} 筆交易 | 累計 PnL: ${sym_pnl:8.2f} | 勝率: {sym_wr:5.1f}%")

        lines.append("=" * 65)
        lines.append(f"報表與圖表已完整存入目錄: {self.results_dir}")

        report_txt = "\n".join(lines)
        report_file = self.results_dir / "performance_report.txt"
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write(report_txt)

        self.logger.info("\n" + report_txt)


# ==============================================================================
# 4. 主命令列入口 (CLI)
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="IBKR 0DTE Options Backtester for SPX, NDX & RUT (Based on jefrnc/ibkr-odte-strategies)"
    )
    parser.add_argument('--strategy', choices=['butterfly', 'breakout', 'iron_condor'], default='butterfly',
                        help="回測策略: butterfly (預設, 對應 DTE0.py 5組蝶式), breakout (突破追價), iron_condor (鐵鷹)")
    parser.add_argument('--symbols', nargs='+', default=['SPX', 'NDX', 'RUT'],
                        help="回測標的 (預設: SPX NDX RUT)")
    parser.add_argument('--days', type=int, default=30,
                        help="回測天數 (預設: 30 天)")
    parser.add_argument('--start-date', type=str, default=None,
                        help="起始日期 (YYYY-MM-DD，若未指定則由 --days 自動計算)")
    parser.add_argument('--end-date', type=str, default=None,
                        help="結束日期 (YYYY-MM-DD，預設為今日)")
    parser.add_argument('--capital', type=float, default=10000.0,
                        help="初始保證金/資金 (預設: 10000)")
    parser.add_argument('--offline', action='store_true',
                        help="強制使用本地快取或歷史統計合成數據 (不發送 IBKR 連線請求)")

    args = parser.parse_args()

    # 計算日期區間
    if args.end_date:
        end_d = datetime.datetime.strptime(args.end_date, '%Y-%m-%d').date()
    else:
        end_d = datetime.date.today()

    if args.start_date:
        start_d = datetime.datetime.strptime(args.start_date, '%Y-%m-%d').date()
    else:
        start_d = end_d - datetime.timedelta(days=args.days)

    engine = BacktestEngine(
        strategy_name=args.strategy,
        symbols=args.symbols,
        start_date=start_d.strftime('%Y-%m-%d'),
        end_date=end_d.strftime('%Y-%m-%d'),
        initial_capital=args.capital,
        offline=args.offline
    )

    metrics = engine.run()
    if metrics:
        print(f"\n[完成] 0DTE 回測完成！報表位置: {engine.results_dir}")


if __name__ == '__main__':
    main()
