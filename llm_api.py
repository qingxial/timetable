"""
统一的 LLM API 客户端（供 llm_preflight_check.py / llm_failure_analysis.py 共用）。

DashScope/Qwen OpenAI 兼容模式配置：
  DASHSCOPE_API_KEY（必填）、DASHSCOPE_BASE_URL（可选）、DASHSCOPE_MODEL（可选）。

核心接口：
  - LLMClient(...)             -> 可显式注入配置的 OpenAI 兼容客户端
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


DEFAULT_BASE_URL = "https://maas.qianwenaiapi.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen3.7-plus"

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


class LLMClient:
    """Small OpenAI-compatible client with explicit, environment-backed config.

    It owns no agent policy. Callers must validate any structured model output
    before it is allowed to select tools or affect a timetable workflow.
    """

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 model: Optional[str] = None, stream: bool = True,
                 enable_thinking: bool = False):
        # Resolve environment variables when the instance is created, not at import time.
        self.api_key = os.getenv("DASHSCOPE_API_KEY") if api_key is None else api_key
        self.base_url = base_url or os.getenv("DASHSCOPE_BASE_URL") or DEFAULT_BASE_URL
        self.model = model or os.getenv("DASHSCOPE_MODEL") or DEFAULT_MODEL
        self.stream = stream
        self.enable_thinking = enable_thinking
        self._client = None

    def has_api_key(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        if self._client is None:
            if not self.api_key:
                raise RuntimeError("未配置 LLM API：请设置环境变量 DASHSCOPE_API_KEY")
            from openai import OpenAI
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=30.0, max_retries=0)
        return self._client

    def chat(
        self,
        system: str,
        user: str,
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_retries: int = 3,
        retry_wait: float = 1.0,
        stream: Optional[bool] = None,
    ) -> str:
        """Single-turn request with bounded retry for transient failures."""
        if max_retries < 1 or retry_wait < 0:
            raise ValueError("max_retries must be positive and retry_wait non-negative")
        client = self._get_client()
        last_err: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            try:
                completion = client.chat.completions.create(
                    model=model or self.model,
                    temperature=temperature,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    extra_body={"enable_thinking": self.enable_thinking},
                    stream=self.stream if stream is None else stream,
                )
                if self.stream if stream is None else stream:
                    pieces = []
                    for chunk in completion:
                        if not getattr(chunk, "choices", None):
                            continue
                        delta = getattr(chunk.choices[0], "delta", None)
                        content = getattr(delta, "content", None) if delta is not None else None
                        if isinstance(content, str):
                            pieces.append(content)
                    return "".join(pieces)
                return completion.choices[0].message.content or ""
            except Exception as error:  # noqa: BLE001 - normalize provider errors
                message = self._safe_error(error)
                if any(marker in message.lower() for marker in _FATAL_MARKERS):
                    raise FatalLLMError(
                        f"LLM 账号/额度类错误（欠费或 Key 失效），停止重试：{message}"
                    ) from error
                last_err = error
                if attempt < max_retries:
                    time.sleep(retry_wait)
        raise RuntimeError(f"LLM 调用失败（已重试 {max_retries} 次）：{self._safe_error(last_err)}")

    def _safe_error(self, error: Optional[Exception]) -> str:
        message = str(error or "unknown error")
        if self.api_key:
            message = message.replace(self.api_key, "[REDACTED]")
        return re.sub(r"(?i)(sk-[A-Za-z0-9._-]{8,})", "[REDACTED]", message)[:500]

    def chat_json(
        self,
        system: str,
        user: str,
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_parse_retries: int = 2,
        max_call_retries: int = 3,
    ) -> Any:
        """Request JSON, then retry only when the response is not parseable."""
        prompt = user
        last_err: Optional[Exception] = None
        for _ in range(max_parse_retries + 1):
            text = self.chat(system, prompt, model=model, temperature=temperature, max_retries=max_call_retries)
            try:
                return json.loads(_extract_json_text(text))
            except (json.JSONDecodeError, ValueError) as error:
                last_err = error
                prompt = (
                    user
                    + "\n\n注意：上一次回复无法被 json.loads 解析"
                    + f"（错误：{error}）。请严格只输出合法 JSON，不要带任何解释文字或 Markdown 围栏。"
                )
        raise RuntimeError(f"LLM 输出始终无法解析为 JSON：{last_err}")


_default_client: Optional[LLMClient] = None


def get_default_client() -> LLMClient:
    global _default_client
    if _default_client is None:
        _default_client = LLMClient()
    return _default_client


def has_api_key() -> bool:
    return bool(os.getenv("DASHSCOPE_API_KEY")) if _default_client is None else _default_client.has_api_key()


def _get_client():
    return get_default_client()._get_client()


def chat(
    system: str,
    user: str,
    model: Optional[str] = None,
    temperature: float = 0.2,
    max_retries: int = 3,
    retry_wait: float = 5.0,
) -> str:
    """Backward-compatible default-client wrapper."""
    return get_default_client().chat(system, user, model=model, temperature=temperature,
                                max_retries=max_retries, retry_wait=retry_wait)


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
    """Backward-compatible default-client wrapper for structured replies."""
    return get_default_client().chat_json(system, user, model=model, temperature=temperature,
                                     max_parse_retries=max_parse_retries)
