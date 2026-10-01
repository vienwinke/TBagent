# -*- coding: utf-8 -*-
"""编排层：Streamlit 与 FastAPI 共用同一条链路（接口契约 §3.2 / §2.3）

为什么必须单独一层：app.py 里那套「路由 → 分支 → 组织结果」的逻辑，与将来 FastAPI/SSE
需要的是**同一条链路**。各写一份必然漂移 —— 这个仓库已经吃过三次同类亏：
prompts 有两套、prompts_user 建好了却长期没接线、policy 实现了却没被生产代码调用。

本层只做编排与事件产出，不含任何 UI / 传输细节：
调用方拿到 `(event, data)` 流；Streamlit 直接渲染，FastAPI 原样转成 SSE 帧。

事件顺序（契约 §2.3，delta 可多次）：
    meta → scope → route → sql → table → chart → delta → citations → guard → done / error

关键约束：
  · **拒答发生在生成之前**：scope 判定不通过时只发 delta + done，零模型调用；
  · `sql` 事件只对运营及以上下发 SQL 明文，普通用户只收到 has_sql=True；
  · 失败走 error 事件（带 code/retryable），不用异常穿透到调用方。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from loguru import logger

import llm as llm_mod
from config import LLM
from agent import answer as answer_mod
from agent import chart as chart_mod
from agent import nl2sql, policy, prompts_user, rag, router
from agent import scope as scope_mod
from agent.policy import Principal

Event = tuple[str, dict[str, Any]]
LlmFn = Callable[[list[dict[str, str]]], dict[str, Any]]
TextFn = Callable[[list[dict[str, str]]], str]

# 闲聊回复：与 Streamlit 界面保持同一句，避免两处文案漂移
CHAT_REPLY = ("我是你的 treatbord 助手：可以**查数据**（如「待接取的任务有几个？」），"
              "也可以**查业务规则**（如「任务有哪些状态？」）。")


@dataclass
class Deps:
    """可注入的模型函数：把"调哪个模型"从编排里拆出去。

    离线测试只需注入这几个桩，完全不碰网络；服务化时换成带租户配额与追踪的封装。
    """

    nl2sql_llm: LlmFn | None = None                     # 生成 SQL
    scope_llm: LlmFn | None = None                      # 语义层兜底（规则未命中时）
    classify_llm: Callable[[str], str] | None = None    # 路由分类兜底
    rag_llm: LlmFn | None = None                        # 知识分支作答
    summary_llm: TextFn | None = None                   # 结果转述（纯文本）


def _usage_delta(before: dict[str, Any]) -> dict[str, Any]:
    after = llm_mod.usage().summary()
    return {
        "tokens": after["total_tokens"] - before.get("total_tokens", 0),
        "cost_yuan": round(after["cost_yuan"] - before.get("cost_yuan", 0.0), 6),
        "cache_tokens": after.get("cache_read_tokens", 0) - before.get("cache_read_tokens", 0),
    }


def _done(started: float, before: dict[str, Any], *, route: str | None = None,
          denied: bool = False, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"elapsed_ms": int((time.time() - started) * 1000),
                               "route": route, "denied": denied}
    payload.update(_usage_delta(before))
    payload.update(extra)
    return payload


def error_event(message: str | None) -> Event:
    """把失败翻译成契约里的错误码（契约 §2.4：可重试性由前端决定）。

    顺序有讲究：**先判模型侧**再判超时 —— 否则"模型调用超时"（APITimeoutError）会被
    当成 SQL 执行超时（实测踩到：错误码发成 SQL_TIMEOUT，前端会去重试 SQL，方向就错了）。
    """
    text = (message or "").lower()
    raw = message or ""
    model_side = ("生成阶段" in raw or "模型" in raw or "llm" in text
                  or "ratelimit" in text or "429" in text or "apitimeout" in text)
    if model_side:
        return "error", {"code": "LLM_UNAVAILABLE", "message": raw or "模型不可用",
                         "retryable": True}
    if "timeout" in text or "超时" in text:
        return "error", {"code": "SQL_TIMEOUT", "message": raw or "执行超时", "retryable": True}
    return "error", {"code": "INTERNAL", "message": raw or "未知错误", "retryable": True}


def _guard_events(result: nl2sql.Nl2SqlResult) -> Iterator[Event]:
    """护栏动作事件：前端只展示提示条，不需要理解内部机制"""
    query = result.query or {}
    if result.guard.get("limit_added"):
        yield "guard", {"action": "limit_added",
                        "note": "原 SQL 无 LIMIT，已自动追加 LIMIT %s" % result.guard.get("limit")}
    if query.get("masked_columns"):
        yield "guard", {"action": "masked",
                        "note": "结果中的敏感列已脱敏：%s" % ", ".join(query["masked_columns"])}
    if query.get("truncated"):
        yield "guard", {"action": "truncated", "note": "结果超过行数上限，已截断"}


def answer_stream(question: str, principal: Principal, *,
                  session_id: str | None = None,
                  trace_id: str | None = None,
                  history: str = "",
                  deps: Deps | None = None,
                  execute: bool = True,
                  top_k: int | None = None,
                  use_cache: bool = True) -> Iterator[Event]:
    """跑完整条链路并逐个产出事件（契约 §3.2 的 answer_stream）。

    每次调用自带**请求级用量作用域**：并发请求（服务化后）各自的 tokens/成本互不污染，
    done 事件里的数字才是本请求的真实用量。作用域可重入，服务层再包一层也不会把用量吞掉。
    """
    with llm_mod.isolated_usage():
        yield from _answer_stream(question, principal, session_id=session_id, trace_id=trace_id,
                                  history=history, deps=deps, execute=execute, top_k=top_k,
                                  use_cache=use_cache)


def _answer_stream(question: str, principal: Principal, *,
                   session_id: str | None = None,
                   trace_id: str | None = None,
                   history: str = "",
                   deps: Deps | None = None,
                   execute: bool = True,
                   top_k: int | None = None,
                   use_cache: bool = True) -> Iterator[Event]:
    """编排实现体（由 answer_stream 包上用量作用域后调用）。"""
    deps = deps or Deps()
    trace = trace_id or uuid.uuid4().hex
    started = time.time()
    before = llm_mod.usage().summary()

    yield "meta", {"trace_id": trace, "session_id": session_id,
                   "prompt_version": prompts_user.PROMPT_VERSION,
                   "policy_version": policy.POLICY_VERSION, "model": LLM.model}

    # 1) 语义层范围判定：必须在生成之前 —— 拒答题目不消耗模型调用（契约 §2.3 scope）
    decision = scope_mod.judge(question, principal, history=history, llm_fn=deps.scope_llm)
    yield "scope", {"scope": decision.scope, "allowed": decision.allowed,
                    "reason": decision.reason, "source": decision.source}
    if not decision.allowed:
        yield "delta", {"text": prompts_user.deny_text(decision.deny_reason)}
        logger.info("[pipeline] 拒答 {}（{}）trace={}", decision.deny_reason, decision.source, trace)
        yield "done", _done(started, before, route=None, denied=True,
                            deny_reason=decision.deny_reason, scope=decision.scope,
                            cache_hit=False, repaired=False, attempts=0)
        return

    # 2) 路由（chat / knowledge / data）
    # 普通用户关闭"直接输入 SQL"入口（设计文档 §5.3）；运营及以上保留
    route = router.route(question, classify_fn=deps.classify_llm or router.llm_classify,
                         allow_raw_sql=principal.is_privileged)
    yield "route", {"route": route}

    if route == router.CHAT:
        yield "delta", {"text": CHAT_REPLY}
        yield "done", _done(started, before, route=route, cache_hit=False, repaired=False,
                            attempts=0)
        return

    if route == router.KNOWLEDGE:
        r = rag.answer(question, top_k=top_k, llm_fn=deps.rag_llm)
        if r.answer:
            yield "delta", {"text": r.answer}
        if r.citations:
            # 契约里 citations 是 [{title, snippet}]；snippet 取检索到的原文片段，便于核查
            snippets = {h.get("heading"): h.get("text", "") for h in (r.hits or [])}
            yield "citations", [{"title": c, "snippet": (snippets.get(c) or "")[:160]}
                                for c in r.citations]
        if r.error:
            yield error_event(r.error)
        yield "done", _done(started, before, route=route, insufficient=r.insufficient,
                            cache_hit=False, repaired=False, attempts=0)
        return

    # 3) 数据分支：编排层已经判过范围，这里不再重复调用兜底模型（避免判两次）
    r = nl2sql.answer(question, principal=principal, llm_fn=deps.nl2sql_llm,
                      scope_llm_fn=None, history=history,
                      execute=execute, top_k=top_k, use_cache=use_cache)

    if r.denied:
        # 拒答是正常业务结果（语义层 / 策略层 / 模型主动拒答），不是错误
        yield "delta", {"text": r.deny_message()}
        yield "guard", {"action": "denied", "note": r.deny_reason or "denied"}
        yield "done", _done(started, before, route=route, denied=True, scope=r.scope,
                            deny_reason=r.deny_reason, cache_hit=False,
                            repaired=False, attempts=r.attempts)
        return

    if r.sql:
        # §2.3：SQL 明文仅 ADMIN 下发；USER 只收 has_sql。
        # tables = **SQL 实际引用的表**（紧邻 sql，供运营核查）；retrieved = Schema 检索命中的 Top-K。
        if principal.is_privileged:
            yield "sql", {"sql": r.sql, "tables": list(r.guard.get("tables") or []),
                          "retrieved": list(r.tables)}
        else:
            yield "sql", {"has_sql": True}

    if r.query:
        yield "table", {"columns": list(r.query.get("columns") or []),
                        "rows": [list(x.values()) for x in r.rows],
                        "row_count": r.query.get("row_count"),
                        "truncated": bool(r.query.get("truncated")),
                        "masked_columns": list(r.query.get("masked_columns") or [])}
        if r.rows:
            spec = chart_mod.choose_spec(r.query.get("columns") or [],
                                         [tuple(x.values()) for x in r.rows],
                                         masked_columns=r.query.get("masked_columns") or ())
            yield "chart", spec.as_dict()

    if r.ok and r.query:
        text = answer_mod.summarize(question, r.query.get("columns") or [],
                                    [tuple(x.values()) for x in r.rows], llm_fn=deps.summary_llm)
        if text:
            yield "delta", {"text": text}

    yield from _guard_events(r)

    if r.stage == "failed":
        yield error_event(r.error)

    yield "done", _done(started, before, route=route, denied=False, scope=r.scope,
                        cache_hit=r.cache_hit, repaired=r.repaired, attempts=r.attempts,
                        isolated=r.isolated, stage=r.stage)
