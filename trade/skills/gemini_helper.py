#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gemini API Helper for Agentic Skills (trade/skills/gemini_helper.py)
================================================================================
提供全系統所有交易技能 (Skills) 共享之 Gemini 模型呼叫與金鑰解析引擎：
  1. 多模型鏈式容錯備援 (Model Fallback Chain)：優先使用快速穩定的 gemini-3.6-flash
  2. 支援完整性校驗回呼 (Validation Callback)
  3. 支援高負載 (503) 與速率限制 (429) 自動短延遲重試
  4. 集中管理 API Key 解析，避免各模組重複實作
================================================================================
"""

import os
import sys
import time
import requests

# 預設候選模型順序：以回應最為迅速 (~1s) 且具高可用性的 flash-lite 模型優先，依序降級
CANDIDATE_MODELS = [
    "deepseek-ai/deepseek-v4.1-flash",
    "deepseek-ai/deepseek-r1",
    "meta/llama-3.3-70b-instruct",
]

try:
    from .nv_helper import load_nv_api_key, call_nv_for_skill
except Exception:
    load_nv_api_key = None
    call_nv_for_skill = None


def load_gemini_api_key(env_path=None):
    """
    從環境檔 (.env) 解析 API Key，優先回傳 nv_api (NVIDIA NIM)，其次回傳 gemini_api。
    """
    if load_nv_api_key:
        nv_key = load_nv_api_key(env_path)
        if nv_key:
            return nv_key

    if env_path is None:
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        env_path = os.path.join(base_dir, ".env")

    if not os.path.exists(env_path):
        return None

    with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            for delim in [":", "="]:
                if delim in line:
                    k, v = line.split(delim, 1)
                    k = k.strip().lower()
                    if k in ["nv_api", "nv_api_key", "gemini_api", "gemini_api_key", "gemini_key", "gemini"]:
                        v_str = v.strip()
                        # 若值以引號包覆，優先擷取成對引號內的字串 (排除行尾註解如 #...)
                        if v_str.startswith(('"', "'")):
                            q = v_str[0]
                            end_idx = v_str.find(q, 1)
                            if end_idx != -1:
                                return v_str[1:end_idx].strip()
                        # 若無引號或引號未閉合，去除行尾 # 註解後清除兩端引號與空白
                        if "#" in v_str:
                            v_str = v_str.split("#", 1)[0].strip()
                        return v_str.strip().strip('"').strip("'")
    return None


def call_gemini_for_skill(
    prompt,
    api_key,
    candidate_models=None,
    timeout=50,
    min_chars=50,
    validate_func=None,
    max_output_tokens=80096,
    temperature=0.2,
    retry_on_busy=True,
):
    """
    通用 AI 呼叫器：
      - 優先自動路由至 NVIDIA NIM / nv_helper (OpenAI 相容 API)
      - 若無 nv_helper 則退回使用 Gemini REST API
    """
    if call_nv_for_skill:
        return call_nv_for_skill(
            prompt=prompt,
            api_key=api_key,
            candidate_models=candidate_models,
            timeout=timeout,
            min_chars=min_chars,
            validate_func=validate_func,
            max_output_tokens=max_output_tokens if max_output_tokens > 8000 else 262144,
            temperature=temperature if temperature >= 0.5 else 1.0,
            retry_on_busy=retry_on_busy,
        )

    if not api_key:
        raise ValueError("AI API Key 未提供！")

    models = candidate_models or CANDIDATE_MODELS

    for m_name in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m_name}:generateContent?key={api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_output_tokens,
            },
        }

        # 每個模型最多嘗試 2 次 (針對 503 高負載或短暫超時提供一次即時重試)
        max_attempts = 2 if retry_on_busy else 1
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                print(f"[INFO] [GeminiHelper] 模型 {m_name} 進行第 {attempt} 次嘗試...")
            else:
                print(f"[INFO] [GeminiHelper] 嘗試呼叫模型: {m_name} ...")

            try:
                r = requests.post(url, json=payload, timeout=timeout)
                if r.status_code == 200:
                    res_json = r.json()
                    cands = res_json.get("candidates", [])
                    if cands:
                        parts = cands[0].get("content", {}).get("parts", [])
                        text_parts = [p.get("text", "") for p in parts if "text" in p]
                        text = "".join(text_parts).strip()

                        # 檢查字數
                        if len(text) < min_chars:
                            print(f"[WARN] [GeminiHelper] 模型 {m_name} 回傳字數過少 ({len(text)} 字 < {min_chars})，嘗試下一模型...")
                            break

                        # 檢查完整性回呼函式
                        if validate_func and not validate_func(text):
                            print(f"[WARN] [GeminiHelper] 模型 {m_name} 回傳內容未通過完整性校驗 (長度: {len(text)} 字)，嘗試下一模型...")
                            break

                        print(f"[SUCCESS] [GeminiHelper] 模型 {m_name} 呼叫成功且報告完整 (長度: {len(text)} 字)！")
                        return text
                    else:
                        print(f"[WARN] [GeminiHelper] 模型 {m_name} 未返回候選內容 (candidates 為空)")
                        break

                elif r.status_code == 401:
                    err_msg = r.json().get("error", {}).get("message", r.text[:150])
                    print(f"[ERROR] [GeminiHelper] Gemini API 憑證無效或金鑰錯誤 (HTTP 401): {err_msg}")
                    print(f"[ERROR] [GeminiHelper] 請檢查 .env 中的 gemini_api 設定是否正確或過期。中斷所有模型嘗試。")
                    return None

                elif r.status_code in [429, 503]:
                    err_msg = r.json().get("error", {}).get("message", f"HTTP {r.status_code}")
                    print(f"[WARN] [GeminiHelper] 模型 {m_name} 伺服器繁忙 (代碼 {r.status_code}): {err_msg[:120]}")
                    if attempt < max_attempts:
                        time.sleep(2.5)
                        continue
                    else:
                        print(f"[WARN] [GeminiHelper] 模型 {m_name} 重試後仍繁忙，切換至備用模型...")
                        break
                else:
                    err_msg = r.json().get("error", {}).get("message", r.text[:150])
                    print(f"[WARN] [GeminiHelper] 模型 {m_name} 回應錯誤 ({r.status_code}): {err_msg}")
                    break

            except requests.exceptions.Timeout:
                print(f"[WARN] [GeminiHelper] 模型 {m_name} 連線逾時 (超時設定: {timeout} 秒)")
                if attempt < max_attempts:
                    time.sleep(1.5)
                    continue
                else:
                    print(f"[WARN] [GeminiHelper] 模型 {m_name} 連續逾時，切換至備用模型...")
                    break
            except Exception as e:
                print(f"[WARN] [GeminiHelper] 模型 {m_name} 連線異常: {e}")
                break

            time.sleep(1.0)

    return None

