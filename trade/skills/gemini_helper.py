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

# 預設候選模型順序：經實測 gemini-3.6-flash 回應最為迅速穩定 (~3s)，次選 gemini-3.8-flash (~6s)，最後為 gemini-3.7-flash
CANDIDATE_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
]


def load_gemini_api_key(env_path=None):
    """
    從環境檔 (.env) 解析 Gemini API Key。支援以下格式：
      gemini_api:AIzaSy...
      gemini_api=AIzaSy...
      GEMINI_API_KEY=AIzaSy...
    """
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
                    v = v.strip().strip('"').strip("'")
                    if k in ["gemini_api", "gemini_api_key", "gemini_key", "gemini"]:
                        return v
    return None


def call_gemini_for_skill(
    prompt,
    api_key,
    candidate_models=None,
    timeout=50,
    min_chars=50,
    validate_func=None,
    max_output_tokens=8096,
    temperature=0.2,
    retry_on_busy=True,
):
    """
    通用 Gemini REST API 呼叫器：
      - 依序嘗試候選模型
      - 支援內容完整性檢驗 (validate_func)
      - 支援 503 高負載與 429 速率限制重試
      - 回傳完整文字報告
    """
    if not api_key:
        raise ValueError("Gemini API Key 未提供！")

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

