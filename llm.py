# -*- coding: utf-8 -*-
"""大模型调用封装：指数退避重试 + 统一异常 + token/成本统计

设计要点（面试可讲）：
1. 429/5xx/超时/连接错误 → 指数退避 + 抖动重试（默认 3 次），4xx 参数错误不重试
2. 每次调用都累计 usage（prompt/completion/total token）并按单价折算成本
3. JSON 模式：优先 response_format，解析失败降级为「提取首个 {...}」+ 抛出可诊断异常
"""
from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from openai import OpenAI

from config import LLM


class LLMError(RuntimeError):
    """模型调用失败（已重试仍失败，或配置缺失）"""


class EmptyResponseError(LLMError):
    """provider 偶发「HTTP 200 + 空内容」。

    实测：60 题全量评估里有一条 `time-12` 拿到的原始输出是空字符串，
    被当成"模型输出不合法"烧掉了回环次数，最后整题失败。
    空内容属于瞬时故障，应该在 chat() 内部退避重试，而不是污染上层链路。
    """


@dataclass
class Usage:
    """token 与成本累计"""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    retries: int = 0
    seconds: float = 0.0
    _by_model: dict[str, int] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cost_yuan(self) -> float:
        return self.prompt_tokens / 1e6 * LLM.price_in + self.completion_tokens / 1e6 * LLM.price_out

    def add(self, model: str, prompt: int, completion: int, seconds: float) -> None:
        self.calls += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.seconds += seconds
        self._by_model[model] = self._by_model.get(model, 0) + 1

    def summary(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "retries": self.retries,
            "seconds": round(self.seconds, 2),
            "cost_yuan": round(self.cost_yuan, 6),
            "by_model": dict(self._by_model),
        }


USAGE = Usage()
_client: OpenAI | None = None

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
RETRYABLE_NAMES = ("RateLimitError", "APITimeoutError", "APIConnectionError", "InternalServerError")


def client() -> OpenAI:
    global _client
    if _client is None:
        if not LLM.configured:
            raise LLMError("未配置 LLM_API_KEY（或 DEEPSEEK_API_KEY）：请在 .env 中填写后再调用模型")
        _client = OpenAI(api_key=LLM.api_key, base_url=LLM.base_url, timeout=LLM.timeout, max_retries=0)
    return _client


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, EmptyResponseError):
        return True                      # 空响应是瞬时故障，必须重试
    name = type(exc).__name__
    if name in RETRYABLE_NAMES:
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in RETRYABLE_STATUS:
        return True
    return isinstance(exc, (TimeoutError, ConnectionError))


def chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = 2048,
    json_mode: bool = False,
    tag: str = "",
) -> str:
    """一次对话调用（含重试与用量统计），返回模型文本"""
    use_model = model or LLM.model
    kwargs: dict[str, Any] = {"model": use_model, "messages": messages, "temperature": temperature}
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    if not LLM.configured:
        raise LLMError("未配置 LLM_API_KEY（或 DEEPSEEK_API_KEY）：请在 .env / Secrets 中填写后再调用模型")
    problems = LLM.issues()
    if problems:
        raise LLMError("大模型配置有误：%s" % "；".join(problems))

    attempts = 0
    last_exc: BaseException | None = None
    for attempt in range(LLM.max_retries + 1):
        attempts = attempt + 1
        started = time.time()
        try:
            resp = client().chat.completions.create(**kwargs)
            latency = time.time() - started
            if resp.usage:
                USAGE.add(use_model, resp.usage.prompt_tokens or 0, resp.usage.completion_tokens or 0, latency)
            logger.debug("[llm] {} model={} {}s tokens={}", tag or "chat", use_model, latency,
                         getattr(resp.usage, "total_tokens", "-"))
            content = (resp.choices[0].message.content or "").strip()
            if not content:
                # 见 EmptyResponseError：空内容当瞬时故障重试（否则上层会误判为"输出不合法"）
                raise EmptyResponseError("模型返回空内容")
            return content
        except BaseException as exc:  # noqa: BLE001
            # httpx/urllib3 对请求头做 ASCII 编码，密钥含中文时报的错很难懂 → 直接给出可操作提示
            if isinstance(exc, UnicodeEncodeError):
                raise LLMError("请求头编码失败（UnicodeEncodeError）：LLM_API_KEY/LLM_BASE_URL/LLM_MODEL "
                               "里有非 ASCII 字符，请检查是否把占位符当成了密钥") from exc
            last_exc = exc
            if attempt >= LLM.max_retries or not _retryable(exc):
                break
            USAGE.retries += 1
            wait = min(2 ** attempt + random.uniform(0, 0.5), 20)
            logger.warning("[llm] 第 {} 次失败({})，{}s 后重试: {}", attempt + 1, type(exc).__name__, wait,
                           str(exc)[:120])
            time.sleep(wait)
    raise LLMError("模型调用失败（尝试 %d 次）: %s: %s" % (attempts, type(last_exc).__name__,
                                                              str(last_exc)[:200])) from last_exc


_JSON_RE = re.compile(r"\{.*\}", re.S)


def chat_json(messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
    """要求模型返回 JSON 并解析；解析失败时抛出带原始输出的异常，便于诊断"""
    raw = chat(messages, json_mode=True, **kwargs)
    if not raw:
        raise LLMError("模型返回空内容（json_mode）")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = _JSON_RE.search(raw)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        # 区分「被截断」与「夹杂多余文本」：前者要精简输出，后者要提示只输出 JSON
        hint = "" if raw.rstrip().endswith("}") else "（JSON 未闭合，疑似被 max_tokens 截断）"
        raise LLMError("模型未返回合法 JSON%s，原始输出前 300 字：%s" % (hint, raw[:300]))


def stats() -> dict[str, Any]:
    return USAGE.summary()
