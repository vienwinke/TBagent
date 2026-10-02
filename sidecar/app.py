# -*- coding: utf-8 -*-
"""AI 边车（P0 的另一半）：把编排层包成 HTTP + SSE 服务。

契约：docs/treatbord嵌入-接口契约.md §2.1（端点）/ §2.3（SSE 事件）/ §2.4（错误码）

已实现
    POST /v1/ai/chat   SSE 事件流（与 Streamlit 共用 agent/pipeline.py 同一条链路）
    GET  /healthz      进程存活
    GET  /readyz       依赖就绪（LLM 配置 / 数据库只读 / 策略版本 / 鉴权是否配置）
    GET  /metrics      Prometheus 文本（自有的请求计数，不依赖进程级用量单例）

未实现（P1 / P2，契约里已排期）
    JWT 校验、会话与审计落库（ai_* 四表）、Redis 限流/幂等、配额

⚠️ 身份为什么**默认拒绝服务**（fail-closed）
    契约 §2.2 要求"只信内部 JWT，绝不接受客户端自带的 user_id"。
    在 P1 的 JWT 校验落地之前，这个服务不能对外提供问答 —— 否则它就是一个
    任何人可调用、且以管理员身份查库的接口。因此：
      · 未配置 SIDECAR_DEV_PRINCIPAL 时，/v1/ai/chat 返回 **503** 并说明原因；
      · 本地联调可设 SIDECAR_DEV_PRINCIPAL=7:USER（仅开发用，日志会告警）。
    这样 P1 只需把 principal_from_request() 换成 JWT 解析，其余代码不用动。
"""
from __future__ import annotations

import json
import contextvars
import os
import threading
import time
from typing import Any, Iterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

import llm as llm_mod
from config import LLM
from agent import audit as audit_mod
from agent import session as session_mod
from agent import pipeline, policy, router
from sidecar import auth
from sidecar.denylist import make_denylist
from sidecar.limiter import make_limiter
from agent.pipeline import Deps
from agent.policy import Principal

app = FastAPI(title="treatbord AI 边车", version="0.1.0")

# 可注入的模型函数：测试注入桩即可完全离线；服务化时换成带租户配额与追踪的封装
DEPS: Deps | None = None

def auth_configured() -> bool:
    """是否已配置 JWT 密钥（动态读取：部署时注入环境变量即可，不必改代码）"""
    return bool(os.getenv("SIDECAR_JWT_SECRET", "").strip())


class _Stats:
    """服务自身的计数（与 llm.USAGE 无关：请求级用量在 pipeline 的作用域里结算）"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests = 0
        self.denied = 0
        self.errors = 0
        self.tokens = 0
        self.cost_yuan = 0.0

    def record(self, *, denied: bool, error: bool, tokens: int, cost: float) -> None:
        with self.lock:
            self.requests += 1
            self.denied += int(denied)
            self.errors += int(error)
            self.tokens += max(0, tokens)
            self.cost_yuan += max(0.0, cost)

    def reset(self) -> None:
        with self.lock:
            self.requests = self.denied = self.errors = self.tokens = 0
            self.cost_yuan = 0.0


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default




LIMITER = make_limiter()
DENYLIST = make_denylist()

STATS = _Stats()


class ChatRequest(BaseModel):
    """契约 §2.1：上行只有这三个字段，不接受任何客户端自带的身份字段"""

    session_id: str = Field(min_length=1)
    question: str = Field(min_length=1, max_length=2000)
    client_msg_id: str | None = None


def _assert_no_identity_fields() -> None:
    """契约 §2.2：启动时断言请求 schema 里不存在任何身份字段"""
    banned = {"user_id", "userId", "uid", "role"}
    extra = set(ChatRequest.model_fields) & banned
    if extra:
        raise RuntimeError("请求 schema 不得包含身份字段：%s" % ", ".join(sorted(extra)))


def principal_from_request(request: Request) -> Principal:
    """从请求构造身份；无法确认身份时抛 AuthError（调用方按契约 §2.4 映射状态码）。

    优先级是**故意的 fail-safe**：
      · 配置了 SIDECAR_JWT_SECRET → **只认 JWT**，开发身份一律忽略
        （避免生产环境里残留的 SIDECAR_DEV_PRINCIPAL 变成后门）；
      · 未配置密钥 → 仅开发身份可用；两者都没有 → AuthError(AUTH_NOT_CONFIGURED)。
    身份只来自 Authorization 头：请求体里的 user_id 根本没有对应字段（启动时已断言）。
    """
    secret = os.getenv("SIDECAR_JWT_SECRET", "").strip()
    if secret:
        if os.getenv("SIDECAR_DEV_PRINCIPAL", "").strip():
            logger.warning("[sidecar] 同时配置了 JWT 密钥与开发身份：以 JWT 为准，开发身份被忽略")
        return auth.principal_from_bearer(request.headers.get("authorization"), secret=secret,
                                          is_revoked=DENYLIST.is_revoked)

    dev = os.getenv("SIDECAR_DEV_PRINCIPAL", "").strip()
    if dev:
        uid, _, role = dev.partition(":")
        try:
            return Principal(user_id=int(uid), role=(role or "USER").upper())
        except (TypeError, ValueError) as exc:
            logger.error("[sidecar] SIDECAR_DEV_PRINCIPAL 格式错误（应为 7:USER）：{}", dev)
            raise auth.AuthError("SIDECAR_DEV_PRINCIPAL 格式错误（应为 7:USER）") from exc

    raise auth.AuthError(
        "边车尚未配置身份校验（P1 未完成）：请配置 SIDECAR_JWT_SECRET，"
        "或在本地联调时设置 SIDECAR_DEV_PRINCIPAL=7:USER",
        code="AUTH_NOT_CONFIGURED")


_assert_no_identity_fields()      # 启动即断言（契约 §2.2）


def _json_fallback(obj: Any) -> str:
    """最后一道保险：出现非 JSON 原生值时**不要崩掉整条流**。

    执行层已统一归一化（`agent/executor.py: jsonable`），这里只兜"新代码路径漏了某个类型"。
    宁可少一个字段的精度，也不能让用户看到"边车不可达" —— 实测崩溃就是这样发生的。
    """
    logger.warning("[sidecar] SSE 帧里出现非 JSON 原生值 {}，已兜底成字符串（请修上游归一化）",
                   type(obj).__name__)
    return str(obj)


def _sse(event: str, data: dict[str, Any]) -> str:
    """SSE 帧：event: <name> + data: <json>（契约 §2.3）"""
    return "event: %s\ndata: %s\n\n" % (
        event, json.dumps(data, ensure_ascii=False, default=_json_fallback))


def _deps_for(budget_s: float) -> Deps | None:
    """按端到端预算给每次模型调用设上限。

    为什么要传 timeout：模型侧偶发卡顿（实测单次 25s）会直接穿透 8s 预算。
    调用方注入的桩（测试）原样使用，不覆盖。
    """
    if DEPS is not None:
        return DEPS
    remaining = max(1.0, budget_s - 1.0)      # 给 SQL 执行与转述留 1s
    # ★ 总截止时刻也要给：只设单次 timeout 挡不住"重试" —— 实测 8s 预算会跑到 ~11s，
    #   把上游（Java 读超时 / SseEmitter）的耐心耗光，上游报连接被关闭而边车还在傻等。
    #   有了 deadline，每次尝试的超时被剩余时间夹住，且预算耗尽后不再重试。
    deadline = time.time() + budget_s
    return Deps(
        # 单次生成本身也设上限（8s）：否则一次"回环修复"能把剩余 15s 全吃掉，
        # 用户等 20s 才拿到结果 —— 实测就是这么发生的。生成通常 3~6s，8s 足够。
        nl2sql_llm=lambda messages: llm_mod.chat_json(messages, tag="nl2sql",
                                                      timeout=min(remaining, 8.0),
                                                      deadline=deadline),
        summary_llm=lambda messages: llm_mod.chat(messages, temperature=0, max_tokens=256,
                                                  tag="summary", model=LLM.model_cheap,
                                                  timeout=remaining, deadline=deadline),
        # 多轮指代消解也是"便宜档"的活：输入是短对话，输出一个 JSON
        # 改写同样是辅助小活：给它独立的 5s 子预算（含重试），失败就退回原问题
        rewrite_llm=lambda messages: llm_mod.chat_json(
            messages, tag="rewrite", model=LLM.model_cheap, timeout=4.0,
            deadline=min(deadline, time.time() + 5.0)),
        # 路由判定同样是模型调用，必须也受预算约束（实测它在总耗时里白吃 3.5s）。
        # ⚠️ 契约是 **question → 路由字符串**（与 router.llm_classify 同构），
        #    不是 messages → dict：传 chat_json 会让 route() 拿到 dict、判定失败并静默退回
        #    知识库分支（实测把"我现在可以接取哪些任务"答成了资料检索）。
        classify_llm=lambda question: _classify_within_budget(question, remaining, deadline),
    )


def _audit_failure(trace_id: str | None, principal: Principal, question: str,
                   session_ref: int | None, *, reason: str, latency_ms: int,
                   tokens: int = 0, cost: float = 0.0) -> None:
    """为**崩溃/超时**的请求补一条最小审计行。

    正常路径的审计由 pipeline 的 `_audit` 事件驱动，而那条事件在流末尾 ——
    请求中途崩掉时它永远不会发出，于是"失败的请求恰恰在审计表里查不到"（实测）。
    best-effort：写失败只记日志，绝不能影响正在收尾的响应。
    """
    if not audit_mod.enabled():
        return
    try:
        audit_mod.record(audit_mod.AuditRow(
            trace_id=trace_id or "-", user_id=principal.user_id, session_id=session_ref,
            question=question, verdict=audit_mod.VERDICT_FAILED, deny_reason=reason,
            latency_ms=latency_ms, prompt_tokens=tokens or None, cost_yuan=cost or None))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[sidecar] 补审计失败（不影响响应）：{}: {}", type(exc).__name__, str(exc)[:120])


def _classify_within_budget(question: str, remaining: float, deadline: float) -> str:
    """路由分类（便宜档 + 预算上限）：question → DATA / KNOWLEDGE / CHAT。

    与 `router.llm_classify` 同构，但把单次上限压到 2.5s 并受 deadline 约束 ——
    路由只是"三选一"的小活，没理由吃掉端到端预算的一大块（实测 3.5s）。
    失败时不抛：退回 DATA（与 router.llm_classify 的兜底一致），让数据链路去尝试。
    """

    # ★ 给分类器一个**独立的子预算（含重试）**：只设单次 timeout 挡不住重试 ——
    #   实测 3 次超时 + 退避（1.4+2.5+4.3s）就吃掉了 17s，把主生成阶段的预算耗光。
    #   路由只是"三选一"的小活，失败就该立刻退回规则默认值。
    import time as _time
    sub_deadline = min(deadline, _time.time() + 3.0)
    try:
        text = llm_mod.chat([{"role": "system", "content": router.ROUTE_SYSTEM},
                             {"role": "user", "content": question}],
                            temperature=0, max_tokens=8, tag="route",
                            model=LLM.model_cheap, timeout=2.5,
                            deadline=sub_deadline).strip().lower()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[sidecar] 路由分类失败（{}），退回 data 由规则兜底", type(exc).__name__)
        return router.DATA
    for key in (router.DATA, router.KNOWLEDGE, router.CHAT):
        if key in text:
            return key
    return router.DATA


def _stream(req: ChatRequest, principal: Principal, trace_id: str | None, idem_key: str,
            session_token: str) -> Iterator[str]:
    """把 pipeline 的事件流转成 SSE 帧；同时负责：预算、串行、幂等、记账、释放并发坑位。"""
    budget_s = max(0.5, _env_int("SIDECAR_TIMEOUT_MS", 8000) / 1000.0)
    deadline = time.monotonic() + budget_s
    # 收尾宽限：模型调用在 budget_s 处已停止（见 _deps_for 的 deadline），
    # 但 pipeline 可能正好在这一刻拿到结果（实测差 1ms —— 用户看到超时，而答案已就绪）。
    # 宽限 1.5s 仍小于上游读超时的余量（timeout-ms + 2000ms），所以不会把上游拖爆。
    hard_deadline = deadline + 1.5
    started_at = time.monotonic()
    chunks: list[str] = []
    denied = error = False
    tokens, cost = 0, 0.0
    route: str | None = None
    pending_done: dict[str, Any] | None = None

    def emit(event: str, data: dict[str, Any]) -> str:
        frame = _sse(event, data)
        chunks.append(frame)
        return frame

    # 会话（SESSION_ENABLED，默认关闭）：把 L1 的字符串 session_id 解析成会话主键，
    # 取最近几轮历史用于指代消解；同样带 user_id 过滤 —— 拿别人的 id 也读不到别人的历史。
    session_ref = session_mod.resolve(req.session_id, principal.user_id, question=req.question)
    history = session_mod.history_text(req.session_id, principal.user_id)
    answer_parts: list[str] = []

    try:
        # 会话锁已在端点取得、由本生成器在 finally 释放（见 chat 端点的 acquire_session）
        chunks.append(": connected\n\n")     # 首字节：尽早给前端反馈
        yield chunks[-1]                    # 也进幂等缓冲，重放才能逐字节一致
        # ★ 必须在**同一个 Context** 里推进生成器（而不是 `for ... in` 直接迭代）。
        #   原因：Starlette 会把这个同步生成器放到线程池里**分步**推进，而每一步都在
        #   "当前调用上下文的一份拷贝"里执行 —— 于是 pipeline 在第一步用
        #   `isolated_usage()` 设置的 ContextVar，到第二步就看不见了，`llm.usage()`
        #   回落到**进程级全局 USAGE**。现象极其反直觉：一个 8ms、压根没调模型的
        #   "你好"请求，done 里却报出全局累计的 3015 tokens / ¥0.0089；
        #   而且并发时各请求的数字会互相污染。
        #   钉住一个 Context 之后，每次 `next()` 都在同一作用域里，账目才对得上。
        ctx = contextvars.copy_context()
        # 服务层显式持有本请求的用量作用域：这样"超时/异常"等没有 done 事件的路径
        # 也能如实报账（模型已经烧掉的 token 不会被吞掉）。
        request_usage = llm_mod.Usage()
        ctx.run(llm_mod.bind_usage, request_usage)
        events = pipeline.answer_stream(
                req.question, principal,
                session_id=req.session_id,
                session_ref=session_ref,
                history=history,
                trace_id=trace_id,
                deps=_deps_for(budget_s),
                # 审计落库（AUDIT_ENABLED，默认关闭）：sink 抛异常不影响问答，见 agent/audit.py
                audit_sink=audit_mod.record if audit_mod.enabled() else None)
        while True:
            try:
                event, data = ctx.run(next, events)
            except StopIteration:
                break
            except Exception as exc:  # noqa: BLE001
                # pipeline 中途抛异常（实测过 Decimal 序列化、DB 抖动等）：
                # 原先异常冲出生成器 → Starlette 掐掉连接 → 上游只看到"边车不可达: closed"，
                # 排查方向被完全带偏；而且这类失败**不落审计**。
                # 现在：记审计 + 给可读的 error/done，让客户端按协议收尾。
                logger.error("[sidecar] 事件流中断：{}: {}", type(exc).__name__, str(exc)[:200])
                error = True
                _audit_failure(trace_id, principal, req.question, session_ref,
                               reason="STREAM_ERROR",
                               latency_ms=int((time.monotonic() - started_at) * 1000))
                yield emit("error", {"code": "INTERNAL",
                                     "message": "AI 服务处理异常，请稍后重试",
                                     "retryable": True})
                yield emit("done", {"elapsed_ms": int((time.monotonic() - started_at) * 1000),
                                    "route": route, "denied": False, "tokens": tokens,
                                    "cost_yuan": cost, "cache_hit": False, "repaired": False,
                                    "attempts": 0})
                break
            if event == "done":
                denied = bool(data.get("denied"))
                tokens, cost = int(data.get("tokens", 0)), float(data.get("cost_yuan", 0.0))
                route = data.get("route")
            elif event == "route":
                route = data.get("route")
            elif event == "delta":
                answer_parts.append(data.get("text", ""))
            elif event == "error":
                error = True
            if event == "done":
                # ★ `done` 是契约里的收尾帧，必须最后发：先暂存，等会话落库（saved 事件）之后再发。
                #   否则前端按"见到 done 就收工"实现时会漏掉 saved（拿不到 message_id，反馈就没法用）。
                pending_done = data
                break
            yield emit(event, data)
            # ⚠️ 不能因为 error 就 break：pipeline 在错误路径上**仍会**发 `_audit` 与 `done`
            #    （契约要求 done 收尾）。原先 break 会把 done 吞掉，客户端只能等连接关闭。
            #    真正的"卡死"由下面的 deadline 兜底。
            if time.monotonic() > hard_deadline:
                # 预算耗尽：给诚实降级（不是 500，也不假装成功）
                error = True
                # 给用户的还是人话（技术细节在日志里）：内部的"预算耗尽/第 N 次失败"措辞
                # 直接甩到界面上，用户只会一脸问号（实测）。
                logger.warning("[sidecar] 端到端预算 {}ms 耗尽，向用户降级", int(budget_s * 1000))
                _audit_failure(trace_id, principal, req.question, session_ref,
                               reason="TIMEOUT",
                               latency_ms=int((time.monotonic() - started_at) * 1000),
                               tokens=tokens, cost=cost)
                yield emit("error", {"code": "LLM_UNAVAILABLE",
                                     "message": "模型响应超时了，请再试一次",
                                     "retryable": True})
                # 超时也要**如实报账**：模型已经在烧 token 了，报 0 会让成本统计偏小。
                # request_usage 是服务层直接持有的对象（不依赖 ContextVar 可见性）。
                timeout_tokens, timeout_cost = tokens, cost
                try:
                    summary = request_usage.summary()
                    timeout_tokens = summary["total_tokens"]
                    timeout_cost = summary["cost_yuan"]
                except Exception:  # noqa: BLE001  取不到就退回原值，别因为统计再抛错
                    pass
                pending_done = {"elapsed_ms": int(budget_s * 1000), "route": route,
                                "denied": False, "tokens": timeout_tokens,
                                "cost_yuan": timeout_cost,
                                "timeout": True, "cache_hit": False,
                                "repaired": False, "attempts": 0}
                break
        if session_ref:
            message_id = session_mod.append_turn(
                    session_ref, principal.user_id, req.question,
                    "\n".join(t for t in answer_parts if t),
                    payload={"trace_id": trace_id, "route": route,
                             "denied": denied, "tokens": tokens, "cost_yuan": cost})
            if message_id:
                # 补充事件：让前端拿到"这条回答的消息 id"，反馈（👍/👎）才关联得上。
                # 旧版本客户端忽略未知事件即可（Java 收集器与小程序解析器都是这样）。
                yield emit("saved", {"session_id": session_ref, "message_id": message_id})

        if pending_done is not None:
            yield emit("done", pending_done)
        if idem_key:
            LIMITER.remember(idem_key, "".join(chunks))
    finally:
        STATS.record(denied=denied, error=error, tokens=tokens, cost=cost)
        LIMITER.release_session(req.session_id, session_token)   # 释放会话锁
        LIMITER.release()                                        # 释放并发坑位（含客户端断开）


@app.post("/v1/ai/chat")
def chat(req: ChatRequest, request: Request) -> Any:
    try:
        principal = principal_from_request(request)
    except auth.AuthError as exc:
        # 契约 §2.4：缺/坏/过期 JWT → 401 UNAUTHENTICATED；密钥没配 → 503（fail-closed）
        status = 503 if exc.code == "AUTH_NOT_CONFIGURED" else 401
        logger.info("[sidecar] 拒绝请求：{}（{}）", exc.code, exc)
        return JSONResponse(status_code=status,
                            content={"code": exc.code, "message": str(exc), "retryable": False})
    if not LIMITER.try_acquire():
        # 契约 §2.4：配额/并发超限 → 429（让前端退避重试，而不是排队堵住线程池）
        return JSONResponse(status_code=429,
                            content={"code": "QUOTA_EXCEEDED",
                                     "message": "当前并发已满，请稍后重试", "retryable": True})

    try:
        # 契约 §0：同 session 串行。锁在端点里拿（拿不到就干净地 429），
        # 由流式生成器在 finally 释放 —— 放到生成器里拿会让超时变成"流中途 500"。
        session_token = LIMITER.acquire_session(req.session_id)
    except TimeoutError:
        LIMITER.release()
        return JSONResponse(status_code=429,
                            content={"code": "QUOTA_EXCEEDED",
                                     "message": "该会话正在处理上一条消息，请稍后重试",
                                     "retryable": True})

    # 契约 §0：trace 由 Java 侧生成，边车**原样沿用**（否则线上排障要两边对数）
    trace_id = (request.headers.get("X-Trace-Id") or "").strip() or None
    idem_key = ("%s|%s" % (principal.user_id, req.client_msg_id)) if req.client_msg_id else ""

    replay = LIMITER.replay(idem_key)
    if replay is not None:
        LIMITER.release()
        logger.info("[sidecar] 幂等命中：client_msg_id={} 直接重放上次结果（不重复计费）",
                    req.client_msg_id)
        return StreamingResponse(iter([replay]), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Idempotent-Replay": "1"})

    return StreamingResponse(_stream(req, principal, trace_id, idem_key, session_token),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """进程存活（契约 §2.1）"""
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict[str, Any]:
    """依赖就绪：LLM 是否配置、数据库是否只读、策略版本、鉴权是否已配置"""
    from agent import executor

    try:
        db = executor.health()
        db_ok, readonly = True, bool(db.get("read_only"))
    except Exception as exc:  # noqa: BLE001
        db, db_ok, readonly = {"error": str(exc)[:120]}, False, False
    return {"status": "ok" if (LLM.configured and db_ok) else "degraded",
            "llm_configured": LLM.configured,
            "db_readonly": readonly,
            "db": db,
            "policy_version": policy.POLICY_VERSION,
            "auth_configured": auth_configured(),
            "limiter": LIMITER.backend(),
            "denylist": DENYLIST.backend(),          # memory（单副本）或 redis（多副本共享）
            "audit_enabled": audit_mod.enabled(),
            "session_enabled": session_mod.enabled(),
            "audit": audit_mod.stats(),
            "dev_principal": bool(os.getenv("SIDECAR_DEV_PRINCIPAL", "").strip())}


@app.get("/metrics")
def metrics() -> Any:
    """Prometheus 文本（P0 先给必要的几个；P2 再接成本按用户归集）"""
    lines = [
        "# TYPE tb_ready gauge",
        "tb_ready %d" % int(LLM.configured),
        "# TYPE tb_policy_version_info gauge",
        'tb_policy_version_info{version="%s"} 1' % policy.POLICY_VERSION,
        "# TYPE tb_auth_configured gauge",
        "tb_auth_configured %d" % int(auth_configured()),
        "# TYPE tb_requests_total counter",
        "tb_requests_total %d" % STATS.requests,
        "# TYPE tb_requests_denied_total counter",
        "tb_requests_denied_total %d" % STATS.denied,
        "# TYPE tb_requests_error_total counter",
        "tb_requests_error_total %d" % STATS.errors,
        "# TYPE tb_tokens_total counter",
        "tb_tokens_total %d" % STATS.tokens,
        "# TYPE tb_cost_yuan_total counter",
        "tb_cost_yuan_total %.6f" % STATS.cost_yuan,
        "",
    ]
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse("\n".join(lines), media_type="text/plain; version=0.0.4")


class FeedbackRequest(BaseModel):
    """契约 §2.1：`{message_id, rating(1/-1), comment?}`"""

    message_id: int
    rating: int
    comment: str | None = Field(default=None, max_length=255)


def _principal_or_error(request: Request) -> tuple[Principal | None, Any]:
    """会话类端点的统一鉴权：401（认证失败）/ 503（未配置密钥）"""
    try:
        return principal_from_request(request), None
    except auth.AuthError as exc:
        status = 503 if exc.code == "AUTH_NOT_CONFIGURED" else 401
        return None, JSONResponse(status_code=status,
                                  content={"code": exc.code, "message": str(exc),
                                           "retryable": False})


def _session_disabled() -> JSONResponse:
    """会话存储没打开时如实说，而不是返回空列表让人误以为真的没有历史。"""
    return JSONResponse(status_code=503,
                        content={"code": "SESSION_DISABLED",
                                 "message": "会话存储未启用（SESSION_ENABLED=false）；"
                                            "建好 ai_* 表后打开该开关",
                                 "retryable": False})


@app.get("/v1/ai/sessions")
def list_sessions(request: Request, limit: int = 20) -> Any:
    """会话列表（只列**本人**的）"""
    principal, err = _principal_or_error(request)
    if err is not None:
        return err
    if not session_mod.enabled():
        return _session_disabled()
    return session_mod.list_sessions(principal.user_id, limit=limit)


@app.get("/v1/ai/sessions/{session_id}/messages")
def list_messages(session_id: int, request: Request) -> Any:
    """会话消息；非本人的会话一律 **404**（不泄露"这个会话存在"）"""
    principal, err = _principal_or_error(request)
    if err is not None:
        return err
    if not session_mod.enabled():
        return _session_disabled()
    messages = session_mod.list_messages(session_id, principal.user_id)
    if messages is None:
        return JSONResponse(status_code=404,
                            content={"code": "NOT_FOUND", "message": "会话不存在",
                                     "retryable": False})
    return messages


@app.delete("/v1/ai/sessions/{session_id}")
def delete_session(session_id: int, request: Request) -> Any:
    """删除会话（含消息）；非本人的会话 404"""
    principal, err = _principal_or_error(request)
    if err is not None:
        return err
    if not session_mod.enabled():
        return _session_disabled()
    if not session_mod.delete_session(session_id, principal.user_id):
        return JSONResponse(status_code=404,
                            content={"code": "NOT_FOUND", "message": "会话不存在",
                                     "retryable": False})
    # 204 必须【无 body】：JSONResponse 会写出 null 四个字节，真实 uvicorn 直接抛
    # RuntimeError: Response content longer than Content-Length
    # （TestClient 容忍它，所以单测发现不了 —— 这条是真实服务器日志抓到的）
    return Response(status_code=204)


@app.post("/v1/ai/feedback")
def feedback(req: FeedbackRequest, request: Request) -> Any:
    """用户反馈 👍/👎（rating 只接受 1 / -1）"""
    principal, err = _principal_or_error(request)
    if err is not None:
        return err
    if req.rating not in (1, -1):
        return JSONResponse(status_code=400,
                            content={"code": "BAD_REQUEST", "message": "rating 只能是 1 或 -1",
                                     "retryable": False})
    if not session_mod.enabled():
        return _session_disabled()
    status = session_mod.save_feedback(req.message_id, principal.user_id, req.rating,
                                       req.comment or "")
    if status == "ok":
        # 204 必须【无 body】：JSONResponse 会写出 null 四个字节，真实 uvicorn 直接抛
        # RuntimeError: Response content longer than Content-Length
        # （TestClient 容忍它，所以单测发现不了 —— 这条是真实服务器日志抓到的）
        return Response(status_code=204)
    if status == "not_owner":
        # 与 sessions/messages 一致：不是本人的消息一律 404（不泄露"这条消息存在"）
        return JSONResponse(status_code=404,
                            content={"code": "NOT_FOUND", "message": "回答不存在",
                                     "retryable": False})
    # 落库失败不再假装成功：用户点了评价却没存上，必须让他知道
    return JSONResponse(status_code=500,
                        content={"code": "FEEDBACK_FAILED", "message": "反馈保存失败，请稍后重试",
                                 "retryable": True})


def main() -> None:
    """本地启动：python -m sidecar.app

    默认只绑 127.0.0.1 —— 对外暴露必须等 P1 的 JWT 落地（并上 mTLS），
    否则任何人调 /v1/ai/chat 都以 SIDECAR_DEV_PRINCIPAL 的身份查库。
    """
    import uvicorn

    host = os.getenv("SIDECAR_HOST", "127.0.0.1")
    port = int(os.getenv("SIDECAR_PORT", "8080") or 8080)
    if not auth_configured() and host not in ("127.0.0.1", "localhost"):
        logger.warning("[sidecar] 未配置 SIDECAR_JWT_SECRET 却绑定 {}：/v1/ai/chat 会一律 503，"
                       "请先完成 P1 的鉴权再对外暴露", host)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
