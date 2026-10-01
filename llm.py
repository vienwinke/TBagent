# -*- coding: utf-8 -*-
"""大模型调用封装：指数退避重试 + 统一异常 + token/成本统计

设计要点（面试可讲）：
1. 429/5xx/超时/连接错误 → 指数退避 + 抖动重试（默认 3 次），4xx 参数错误不重试
2. 每次调用都累计 usage（prompt/completion/total token）并按单价折算成本
3. JSON 模式：优先 response_format，解析失败降级为「提取首个 {...}」+ 抛出可诊断异常
"""
from __future__ import annotations

import contextlib
import contextvars
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
    cache_read_tokens: int = 0        # 命中 provider 前缀缓存的输入 token（实测跨问题 75~95%）
    retries: int = 0
    seconds: float = 0.0
    _by_model: dict[str, int] = field(default_factory=dict)
    # model -> [prompt, completion, cached]
    _tokens_by_model: dict[str, list[int]] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cost_yuan(self) -> float:
        """按模型分别计价，并把"命中前缀缓存的输入"按缓存单价计。

        原先用单一单价乘总 token，有两个偏差：
        · 主档/便宜档混用时，便宜档被按主档计价；
        · 缓存命中的输入被按全价计（provider 实际按缓存价计费，通常远低于正常输入价）。
        未配置 LLM_PRICE_IN_CACHED 时缓存价退回正常输入价 —— 估算偏保守、不会低估。
        """
        total = 0.0
        for model, (prompt, completion, cached) in self._tokens_by_model.items():
            pin, pout = LLM.price_for(model)
            cached = min(cached, prompt)
            total += (prompt - cached) / 1e6 * pin
            total += cached / 1e6 * LLM.cached_price_in(model)
            total += completion / 1e6 * pout
        return total

    def add(self, model: str, prompt: int, completion: int, seconds: float,
            *, cached: int = 0) -> None:
        cached = min(max(0, cached), max(0, prompt))     # 缓存读不可能超过输入量
        self.calls += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.cache_read_tokens += cached
        self.seconds += seconds
        self._by_model[model] = self._by_model.get(model, 0) + 1
        slot = self._tokens_by_model.setdefault(model, [0, 0, 0])
        slot[0] += prompt
        slot[1] += completion
        slot[2] += cached

    def summary(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "total_tokens": self.total_tokens,
            "retries": self.retries,
            "seconds": round(self.seconds, 2),
            "cost_yuan": round(self.cost_yuan, 6),
            "by_model": dict(self._by_model),
        }


USAGE = Usage()
_client: OpenAI | None = None

# ---------------------------------------------------------------------------
# 按请求隔离的用量作用域
# ---------------------------------------------------------------------------
# 为什么需要：服务化后一个进程同时处理多个请求，而 USAGE 是模块级单例 ——
# A 请求的 done 事件会把 B 请求的 token/成本算进自己的增量里（成本面板、审计金额全错）。
# 单机 Streamlit 场景下没有并发，作用域不存在时自动落到全局单例，行为不变。
_CURRENT_USAGE: contextvars.ContextVar[Usage | None] = contextvars.ContextVar(
    "llm_usage", default=None)


def usage() -> Usage:
    """当前上下文的用量累计器（未开隔离时就是全局单例）"""
    return _CURRENT_USAGE.get() or USAGE


@contextlib.contextmanager
def isolated_usage() -> Iterator[Usage]:
    """为一次请求开独立的用量作用域（**可重入**）。

    用法：with isolated_usage(): ...

    两个实现细节都是踩过坑才定下来的：
    · **可重入**：编排层已开了一层，服务层再包一层时不能把用量吞进内层（外层看到 0）；
    · **不用 ContextVar token/reset**：SSE 这类流式响应会在**另一个 Context** 里迭代生成器，
      reset(token) 会抛 `Token was created in a different Context`（边车测试实测踩到）。
      改为直接设置/清除，并且只在"当前作用域仍是我"时清除，避免误清别人的。
    """
    existing = _CURRENT_USAGE.get()
    if existing is not None:
        yield existing
        return
    scope = Usage()
    _CURRENT_USAGE.set(scope)
    try:
        yield scope
    finally:
        if _CURRENT_USAGE.get() is scope:
            _CURRENT_USAGE.set(None)

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
    timeout: float | None = None,
) -> str:
    """一次对话调用（含重试与用量统计），返回模型文本"""
    use_model = model or LLM.model
    kwargs: dict[str, Any] = {"model": use_model, "messages": messages, "temperature": temperature}
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if timeout:
        # 每请求超时：边车要按端到端预算（契约 8s）给单次调用设上限，
        # 否则模型侧一次 25s 的卡顿会直接穿透预算（实测出现过）
        kwargs["timeout"] = timeout

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
                # provider 会返回命中前缀缓存的 token 数（OpenAI 兼容字段）；
                # 实测跨问题命中 75~95%，必须单独记账，否则成本估算会把缓存读按全价计。
                details = getattr(resp.usage, "prompt_tokens_details", None)
                cached = int(getattr(details, "cached_tokens", 0) or 0)
                usage().add(use_model, resp.usage.prompt_tokens or 0,
                          resp.usage.completion_tokens or 0, latency, cached=cached)
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
            usage().retries += 1
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
    return usage().summary()
