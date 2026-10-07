#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trade Skills Package (trade/skills)
================================================================================
匯出所有模組化交易 Skills：
  - SelectionMemory: 歷史標的記錄、冷卻期管理與防重複過濾技能
  - InsiderSelectionSkill: 內部人交易籌碼數據清洗與 AI 選股技能
  - OptionsFlowSkill: 期權大單流向分析與最佳 CALL 挑選技能
  - UoaAnalysisSkill: 異常期權大單與價差量化分析技能
  - 共享 Gemini 調用與金鑰解析引擎 (call_gemini_for_skill, load_gemini_api_key)
================================================================================
"""

from .memory_skill import SelectionMemory
from .insider_skill import InsiderSelectionSkill
from .flow_skill import OptionsFlowSkill
from .uoa_skill import UoaAnalysisSkill
from .call_put_flow_skill import CallPutFlowSkill
from .scale_in_order_skill import ScaleInOrderSkill
from .long_call_skill import LongCallSelectionSkill
from .opinion_skill import BarchartOpinionSkill
from .position_order_skill import PositionOrderSkill
from .download_skill import (
    init_driver,
    dismiss_popups,
    login_if_needed,
    load_credentials_from_env,
    wait_for_file_download,
    download_page_csv,
    download_uoa_stocks_csv,
    download_uoa_etfs_csv,
    download_bull_put_csv,
    download_options_flow_csv,
    download_stock_vertical_spread_csv,
    PROFILE_DIR,
)
from .ibkr_skill import (
    load_env_settings,
    create_fast_ib_connection,
    on_ib_error,
    check_ibkr_contract_validity,
    execute_adaptive_option_bracket,
)
from .gemini_helper import call_gemini_for_skill, load_gemini_api_key
from .walk_up_skill import WalkUpOrderSkill, walk_up_limit_price, execute_walk_up_order, round_to_tick, is_valid_price, extract_valid_price, determine_min_tick_and_step, fmt_price

__all__ = [
    "SelectionMemory",
    "InsiderSelectionSkill",
    "OptionsFlowSkill",
    "UoaAnalysisSkill",
    "CallPutFlowSkill",
    "ScaleInOrderSkill",
    "LongCallSelectionSkill",
    "BarchartOpinionSkill",
    "PositionOrderSkill",
    "WalkUpOrderSkill",
    "walk_up_limit_price",
    "execute_walk_up_order",
    "round_to_tick",
    "is_valid_price",
    "extract_valid_price",
    "determine_min_tick_and_step",
    "fmt_price",

    "init_driver",
    "dismiss_popups",
    "login_if_needed",
    "load_credentials_from_env",
    "wait_for_file_download",
    "download_page_csv",
    "download_uoa_stocks_csv",
    "download_uoa_etfs_csv",
    "download_bull_put_csv",
    "download_options_flow_csv",
    "download_stock_vertical_spread_csv",
    "PROFILE_DIR",
    "load_env_settings",
    "create_fast_ib_connection",
    "on_ib_error",
    "check_ibkr_contract_validity",
    "execute_adaptive_option_bracket",
    "call_gemini_for_skill",
    "load_gemini_api_key",
]

