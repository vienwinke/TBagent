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
import os
import threading
from typing import Any, Iterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

from config import LLM
from agent import nl2sql, pipeline, policy
from sidecar import auth
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
        return auth.principal_from_bearer(request.headers.get("authorization"), secret=secret)

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


def _sse(event: str, data: dict[str, Any]) -> str:
    """SSE 帧：event: <name> + data: <json>（契约 §2.3）"""
    return "event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False))


def _stream(question: str, principal: Principal, session_id: str) -> Iterator[str]:
    """把 pipeline 的事件流转成 SSE 帧，并按 done 事件记账"""
    denied = error = False
    tokens, cost = 0, 0.0
    yield ": connected\n\n"                      # 注释帧：让前端尽早拿到首字节
    for event, data in pipeline.answer_stream(question, principal, session_id=session_id,
                                              deps=DEPS):
        if event == "done":
            denied = bool(data.get("denied"))
            tokens, cost = int(data.get("tokens", 0)), float(data.get("cost_yuan", 0.0))
        if event == "error":
            error = True
        yield _sse(event, data)
    STATS.record(denied=denied, error=error, tokens=tokens, cost=cost)


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
    return StreamingResponse(_stream(req.question, principal, req.session_id),
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
