#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NVIDIA API Helper for Agentic Skills (trade/skills/nv_helper.py)
================================================================================
提供全系統所有交易技能 (Skills) 共享之 NVIDIA NIM (OpenAI 相容) 模型呼叫與金鑰解析引擎：
  1. Base URL: https://integrate.api.nvidia.com/v1
  2. 預設旗艦模型: deepseek-ai/deepseek-v4.1-flash (備援支援 deepseek-ai/deepseek-r1 等)
  3. 支援內容完整性檢驗 (validate_func) 與最小字數檢驗 (min_chars)
  4. 支援高負載 (503) 與速率限制 (429) 自動短延遲重試
  5. 自動自 trade/.env 讀取 nv_api 金鑰，並支援系統環境變數 fallback
================================================================================
"""

import os
import sys
import time
import json
import requests

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

# 預設候選模型順序：以 DeepSeek Flash 模型為首選
CANDIDATE_MODELS = [
    "deepseek-ai/deepseek-v4.1-flash",
    "deepseek-ai/deepseek-r1",
    "meta/llama-3.3-70b-instruct",
]


def load_nv_api_key(env_path=None):
    """
    從環境檔 (.env) 或系統環境變數解析 NVIDIA API Key (nv_api)。
    支援以下格式：
      nv_api:nvapi-...
      nv_api=nvapi-...
      NV_API_KEY=nvapi-...
      NV_API=nvapi-...
    """
    # 1. 優先檢查系統環境變數
    env_keys = ["nv_api", "NV_API", "nv_api_key", "NV_API_KEY", "nvidia_api_key", "NVIDIA_API_KEY"]
    for k in env_keys:
        val = os.environ.get(k)
        if val and val.strip():
            return val.strip().strip('"').strip("'")

    # 2. 檢查 .env 檔案
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
                    if k in ["nv_api", "nv_api_key", "nv_key", "nvidia_api_key", "nvidia_api"]:
                        v_str = v.strip()
                        res = None
                        # 若值以引號包覆，優先擷取成對引號內的字串 (排除行尾註解如 #...)
                        if v_str.startswith(('"', "'")):
                            q = v_str[0]
                            end_idx = v_str.find(q, 1)
                            if end_idx != -1:
                                res = v_str[1:end_idx].strip()
                        if res is None:
                            # 若無引號或引號未閉合，去除行尾 # 註解後清除兩端引號與空白
                            if "#" in v_str:
                                v_str = v_str.split("#", 1)[0].strip()
                            res = v_str.strip().strip('"').strip("'")
                        if res:
                            os.environ["nv_api"] = res
                            return res
    return None


def call_nv_for_skill(
    prompt,
    api_key=None,
    candidate_models=None,
    timeout=60,
    min_chars=50,
    validate_func=None,
    max_output_tokens=262144,
    temperature=1.0,
    top_p=0.95,
    retry_on_busy=True,
):
    """
    通用 NVIDIA API (OpenAI Client 相容) 模型呼叫器：
      - 依序嘗試候選模型 (預設首選 deepseek-ai/deepseek-v4.1-flash)
      - 支援內容完整性檢驗 (validate_func)
      - 支援 503 高負載與 429 速率限制自動重試
      - 支援 max_tokens 自動降級調適
      - 回傳完整文字報告字串
    """
    key = api_key or load_nv_api_key()
    if not key:
        raise ValueError("NVIDIA API Key (nv_api) 未提供！請在 trade/.env 中設定 nv_api='nvapi-...'")

    models = candidate_models or CANDIDATE_MODELS
    # 若傳入之模型包含 gemini，自動過濾並保留合法的 NVIDIA 模型；若無則使用預設 DeepSeek/NVIDIA 模型
    valid_nv_models = [m for m in models if "gemini" not in m.lower()]
    models = valid_nv_models if valid_nv_models else CANDIDATE_MODELS
    base_url = "https://integrate.api.nvidia.com/v1"

    # 初始化 OpenAI Client
    client = None
    if OpenAI:
        try:
            client = OpenAI(base_url=base_url, api_key=key, timeout=timeout)
        except Exception as ce:
            print(f"[WARN] [NVHelper] 初始化 OpenAI 客戶端失敗: {ce}，將改用 requests 直接連線。")
            client = None

    for m_name in models:
        max_attempts = 2 if retry_on_busy else 1
        current_max_tokens = max_output_tokens

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                print(f"[INFO] [NVHelper] 模型 {m_name} 進行第 {attempt} 次嘗試...")
            else:
                print(f"[INFO] [NVHelper] 嘗試呼叫 NVIDIA 模型: {m_name} (max_tokens={current_max_tokens})...")

            try:
                text = None

                # 優先使用 OpenAI Client
                if client:
                    try:
                        completion = client.chat.completions.create(
                            model=m_name,
                            messages=[{"role": "user", "content": prompt}],
                            temperature=temperature,
                            top_p=top_p,
                            max_tokens=current_max_tokens,
                            stream=False,
                        )
                        if completion and completion.choices:
                            text = completion.choices[0].message.content or ""
                    except Exception as oai_err:
                        err_str = str(oai_err)
                        # 檢查是否為 max_tokens 超出模型上限的錯誤 (例如限制 4096 / 8192 等)
                        if "max_tokens" in err_str.lower() and current_max_tokens > 4096:
                            print(f"[WARN] [NVHelper] 模型 {m_name} max_tokens 限制過高 ({current_max_tokens})，自動調降為 4096 重試...")
                            current_max_tokens = 4096
                            continue
                        raise oai_err

                # 若無 OpenAI Client 則使用 requests REST API
                else:
                    headers = {
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    }
                    payload = {
                        "model": m_name,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": temperature,
                        "top_p": top_p,
                        "max_tokens": current_max_tokens,
                        "stream": False,
                    }
                    r = requests.post(f"{base_url}/chat/completions", headers=headers, json=payload, timeout=timeout)
                    if r.status_code == 200:
                        res_json = r.json()
                        choices = res_json.get("choices", [])
                        if choices:
                            text = choices[0].get("message", {}).get("content", "")
                    elif r.status_code == 400 and "max_tokens" in r.text and current_max_tokens > 4096:
                        print(f"[WARN] [NVHelper] 模型 {m_name} 報錯 max_tokens 過大，調降至 4096 重試...")
                        current_max_tokens = 4096
                        continue
                    elif r.status_code == 401:
                        print(f"[ERROR] [NVHelper] NVIDIA API Key 憑證無效 (HTTP 401)。請檢查 trade/.env 之 nv_api。")
                        return None
                    elif r.status_code in [429, 503]:
                        print(f"[WARN] [NVHelper] 模型 {m_name} 伺服器繁忙 (HTTP {r.status_code})。")
                        if attempt < max_attempts:
                            time.sleep(2.5)
                            continue
                        break
                    else:
                        print(f"[WARN] [NVHelper] 模型 {m_name} 回應錯誤 ({r.status_code}): {r.text[:150]}")
                        break

                if text is not None:
                    text = text.strip()

                    # 檢查字數門檻
                    if len(text) < min_chars:
                        print(f"[WARN] [NVHelper] 模型 {m_name} 回傳字數過少 ({len(text)} 字 < {min_chars})，嘗試下一模型...")
                        break

                    # 檢查完整性回呼函式
                    if validate_func and not validate_func(text):
                        print(f"[WARN] [NVHelper] 模型 {m_name} 回傳內容未通過完整性校驗 (長度: {len(text)} 字)，嘗試下一模型...")
                        break

                    print(f"[SUCCESS] [NVHelper] 模型 {m_name} 呼叫成功且報告完整 (長度: {len(text)} 字)！")
                    return text

            except Exception as e:
                err_text = str(e)
                if "401" in err_text or "unauthorized" in err_text.lower():
                    print(f"[ERROR] [NVHelper] NVIDIA API 金鑰無效或授權失敗 (401): {err_text[:120]}")
                    print(f"[ERROR] [NVHelper] 請確認 trade/.env 中的 nv_api 是否正確。")
                    return None
                elif "429" in err_text or "503" in err_text or "rate limit" in err_text.lower():
                    print(f"[WARN] [NVHelper] 模型 {m_name} 繁忙或達速率限制: {err_text[:120]}")
                    if attempt < max_attempts:
                        time.sleep(2.5)
                        continue
                    else:
                        break
                else:
                    print(f"[WARN] [NVHelper] 模型 {m_name} 呼叫異常: {err_text[:120]}")
                    break

            time.sleep(1.0)

    return None


# 向後相容別名 (提供無痛替換與雙向相容)
load_gemini_api_key = load_nv_api_key
call_gemini_for_skill = call_nv_for_skill
