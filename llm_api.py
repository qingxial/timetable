"""
统一的 LLM API 客户端（供 llm_preflight_check.py / llm_failure_analysis.py 共用）。

与 specialneeds.py 保持一致的 DashScope 兼容模式（OpenAI SDK），配置读取顺序：
  1. 环境变量 DASHSCOPE_API_KEY / DASHSCOPE_BASE_URL / DASHSCOPE_MODEL
  2. 回退到 specialneeds.py 中的配置

核心接口：
  - chat(system, user)        -> str   普通对话，带重试
  - chat_json(system, user)   -> Any   要求模型输出 JSON 并解析（自动剥离 ```json 围栏，
                                       解析失败自动追加提醒重试）
  - has_api_key()             -> bool  供流水线判断是否跳过 LLM 环节
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Optional


def _load_fallback_config():
    try:
        import specialneeds
        return specialneeds.API_KEY, specialneeds.BASE_URL, specialneeds.MODEL
    except Exception:
        return None, None, None


_FB_KEY, _FB_URL, _FB_MODEL = _load_fallback_config()

API_KEY = os.getenv("DASHSCOPE_API_KEY") or _FB_KEY
BASE_URL = os.getenv("DASHSCOPE_BASE_URL") or _FB_URL or "https://dashscope.aliyuncs.com/compatible-mode/v1"
# 默认 qwen-plus（specialneeds 所用的 qwen3.5-35b-a3b 免费额度已耗尽，2026-06 验证）
MODEL = os.getenv("DASHSCOPE_MODEL") or "qwen-plus"

_client = None


class FatalLLMError(RuntimeError):
    """计费/鉴权类错误（欠费、额度耗尽、Key 无效），重试无意义，调用方应立即中止。"""


# 命中这些关键词的错误不再重试
_FATAL_MARKERS = (
    "arrearage",            # 账号欠费
    "allocationquota",      # 免费额度耗尽
    "invalid_api_key",
    "invalidapikey",
    "incorrect api key",
    "access denied",
)


def has_api_key() -> bool:
    return bool(API_KEY)


def _get_client():
    global _client
    if _client is None:
        if not API_KEY:
            raise RuntimeError("未配置 LLM API：请设置环境变量 DASHSCOPE_API_KEY")
        from openai import OpenAI
        _client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
    return _client


def chat(
    system: str,
    user: str,
    model: Optional[str] = None,
    temperature: float = 0.2,
    max_retries: int = 3,
    retry_wait: float = 5.0,
) -> str:
    """单轮对话，网络/限流错误自动重试。"""
    client = _get_client()
    last_err: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            completion = client.chat.completions.create(
                model=model or MODEL,
                temperature=temperature,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            return completion.choices[0].message.content or ""
        except Exception as e:  # noqa: BLE001 网络/限流等都走统一重试
            msg = str(e)
            if any(m in msg.lower() for m in _FATAL_MARKERS):
                raise FatalLLMError(
                    f"LLM 账号/额度类错误（欠费或 Key 失效），停止重试：{msg}"
                ) from e
            last_err = e
            if attempt < max_retries:
                print(f"  LLM 调用失败（第{attempt}次）：{e}，{retry_wait}s 后重试")
                time.sleep(retry_wait)
    raise RuntimeError(f"LLM 调用失败（已重试 {max_retries} 次）：{last_err}")


def _extract_json_text(text: str) -> str:
    """剥离 ```json 围栏；若仍混有说明文字，截取最外层 [] 或 {}。"""
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, flags=re.S)
    if fence:
        t = fence.group(1).strip()
    if t.startswith("[") or t.startswith("{"):
        return t
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        start = t.find(open_ch)
        end = t.rfind(close_ch)
        if start != -1 and end > start:
            return t[start : end + 1]
    return t


def chat_json(
    system: str,
    user: str,
    model: Optional[str] = None,
    temperature: float = 0.2,
    max_parse_retries: int = 2,
) -> Any:
    """对话并解析 JSON。解析失败时带着错误提示重新请求。"""
    prompt = user
    last_err: Optional[Exception] = None
    for _ in range(max_parse_retries + 1):
        text = chat(system, prompt, model=model, temperature=temperature)
        try:
            return json.loads(_extract_json_text(text))
        except (json.JSONDecodeError, ValueError) as e:
            last_err = e
            prompt = (
                user
                + "\n\n注意：上一次回复无法被 json.loads 解析"
                + f"（错误：{e}）。请严格只输出合法 JSON，不要带任何解释文字或 Markdown 围栏。"
            )
    raise RuntimeError(f"LLM 输出始终无法解析为 JSON：{last_err}")
