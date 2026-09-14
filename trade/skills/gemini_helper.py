#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gemini API Helper for Agentic Skills (trade/skills/gemini_helper.py)
"""

import time
import requests

CANDIDATE_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
]


def call_gemini_for_skill(prompt, api_key, candidate_models=None, timeout=60, min_chars=50):
    """
    通用 Gemini REST API 呼叫器：
      - 依序嘗試候選模型
      - 不硬編碼特定分析格式（適用於各種獨立 Skill）
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
                "temperature": 0.2,
                "maxOutputTokens": 4096,
            },
        }

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
                    if len(text) >= min_chars:
                        print(f"[SUCCESS] [GeminiHelper] 模型 {m_name} 呼叫成功 (長度: {len(text)} 字)！")
                        return text
                    else:
                        print(f"[WARN] [GeminiHelper] 模型 {m_name} 回傳字數過少 ({len(text)} 字 < {min_chars})，嘗試下一模型...")
            elif r.status_code == 429 or r.status_code == 503:
                print(f"[WARN] [GeminiHelper] 模型 {m_name} 繁忙或速率限制 (狀態碼: {r.status_code})，切換備用模型...")
            else:
                err_msg = r.text[:150]
                print(f"[WARN] [GeminiHelper] 模型 {m_name} 錯誤 ({r.status_code}): {err_msg}")
        except Exception as e:
            print(f"[WARN] [GeminiHelper] 模型 {m_name} 連線異常: {e}")

        time.sleep(1.5)

    return None
