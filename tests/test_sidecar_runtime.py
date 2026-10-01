# -*- coding: utf-8 -*-
"""边车运行时契约：trace 透传 / 端到端预算 / 幂等 / 并发与串行（契约 §0）。

这些都是"契约里写死了、但运行时不实现就等于没有"的条款：
· `X-Trace-Id` 由 Java 侧生成，边车必须原样沿用（否则线上排障要两边对数）；
· 端到端 8s 预算：模型侧一次卡顿（实测 25s）就能穿透，所以要给单次调用设上限；
· `client_msg_id` 幂等：网络重发不能重复计费；
· 同 session 串行 + 全局并发上限：超限直接 429，而不是把请求堆在队列里。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.pipeline import Deps  # noqa: E402
from sidecar import app as sidecar  # noqa: E402

SQL = "SELECT COUNT(*) AS 任务数 FROM task WHERE deleted = 0"
GOOD = {"sql": SQL, "reason": "统计任务数", "tables": ["task"]}
BODY = {"session_id": "s_1", "question": "待接取的任务有几个？", "client_msg_id": "c_1"}


@pytest.fixture
def client(monkeypatch):
    sidecar.STATS.reset()
    sidecar.LIMITER.reset()
    monkeypatch.setenv("SIDECAR_DEV_PRINCIPAL", "7:USER")
    monkeypatch.delenv("SIDECAR_JWT_SECRET", raising=False)
    monkeypatch.delenv("SIDECAR_MAX_CONCURRENCY", raising=False)
    monkeypatch.delenv("SIDECAR_TIMEOUT_MS", raising=False)
    monkeypatch.setattr(sidecar, "DEPS", Deps(nl2sql_llm=lambda m: GOOD,
                                              summary_llm=lambda m: "共 8 个任务。"))
    return TestClient(sidecar.app)


def _meta(body: str) -> dict:
    import json
    for block in body.split("\n\n"):
        if block.startswith("event: meta"):
            return json.loads(block.split("data: ", 1)[1])
    raise AssertionError("没有 meta 帧：%s" % body[:200])


# ------------------------------------------------------------------ trace 透传
def test_x_trace_id_is_reused_not_regenerated(client):
    """契约 §0：Java 侧生成的 trace 必须被沿用（边车自己再生成一个就断了链路）"""
    r = client.post("/v1/ai/chat", json=BODY, headers={"X-Trace-Id": "java-trace-001"})
    assert _meta(r.text)["trace_id"] == "java-trace-001"


def test_trace_id_generated_when_absent(client):
    meta = _meta(client.post("/v1/ai/chat", json=BODY).text)
    assert meta["trace_id"] and len(meta["trace_id"]) >= 16


# ------------------------------------------------------------------ 幂等
def test_same_client_msg_id_replays_without_second_model_call(client, monkeypatch):
    calls = []

    def counting(messages):
        calls.append(1)
        return GOOD

    monkeypatch.setattr(sidecar, "DEPS", Deps(nl2sql_llm=counting,
                                              summary_llm=lambda m: "共 8 个任务。"))
    first = client.post("/v1/ai/chat", json=BODY)
    second = client.post("/v1/ai/chat", json=BODY)

    assert first.status_code == 200 and second.status_code == 200
    assert second.text == first.text, "重发必须返回同一结果"
    assert len(calls) == 1, "重发不得再次调用模型（不重复计费）"
    assert second.headers.get("X-Idempotent-Replay") == "1"


def test_different_client_msg_id_is_not_a_replay(client):
    """不同 client_msg_id 不得被当成重发。

    注意：这里不能拿"模型调用次数"当判据 —— 同问题的第二次会被**问题→SQL 缓存**接住
    （缓存按角色共享、不按 client_msg_id），模型本来就不会被调用。所以直接断言幂等语义。
    """
    client.post("/v1/ai/chat", json=BODY)
    other = client.post("/v1/ai/chat", json=dict(BODY, client_msg_id="c_2"))
    assert other.headers.get("X-Idempotent-Replay") is None


def test_idempotency_is_per_user(client, monkeypatch):
    """同一 client_msg_id 在不同用户之间不得互相串（否则会把甲的答案发给乙）"""
    client.post("/v1/ai/chat", json=BODY)                       # user 7
    monkeypatch.setenv("SIDECAR_DEV_PRINCIPAL", "8:USER")
    other = client.post("/v1/ai/chat", json=BODY)               # user 8，同 client_msg_id
    assert other.headers.get("X-Idempotent-Replay") is None, "幂等键必须含用户维度"


def test_idempotency_key_includes_user():
    """直接钉住键的构成：同 id 不同用户互不命中"""
    sidecar.LIMITER.reset()
    sidecar.LIMITER.remember("7|c_9", "frames-of-user-7")
    assert sidecar.LIMITER.replay("7|c_9") == "frames-of-user-7"
    assert sidecar.LIMITER.replay("8|c_9") is None


# ------------------------------------------------------------------ 并发与串行
def test_saturated_concurrency_returns_429(client, monkeypatch):
    monkeypatch.setenv("SIDECAR_MAX_CONCURRENCY", "1")
    sidecar.LIMITER.reset()
    assert sidecar.LIMITER.try_acquire() is True        # 手动占住唯一坑位
    try:
        r = client.post("/v1/ai/chat", json=BODY)
        assert r.status_code == 429
        assert r.json()["code"] == "QUOTA_EXCEEDED" and r.json()["retryable"] is True
    finally:
        sidecar.LIMITER.release()


def test_slot_is_released_after_request(client, monkeypatch):
    monkeypatch.setenv("SIDECAR_MAX_CONCURRENCY", "1")
    sidecar.LIMITER.reset()
    assert client.post("/v1/ai/chat", json=BODY).status_code == 200
    assert client.post("/v1/ai/chat", json=dict(BODY, client_msg_id="c_2")).status_code == 200, \
        "上一个请求结束后必须释放坑位"


def test_same_session_lock_is_shared_and_isolated(monkeypatch):
    """同一 session 互斥、不同 session 互不影响；等待上限到了抛 TimeoutError（端点转 429）"""
    monkeypatch.setenv("SIDECAR_SESSION_LOCK_WAIT_MS", "50")
    sidecar.LIMITER.reset()

    token_a = sidecar.LIMITER.acquire_session("s-A")
    try:
        with pytest.raises(TimeoutError):
            sidecar.LIMITER.acquire_session("s-A")        # 同 session：等不到
        token_b = sidecar.LIMITER.acquire_session("s-B")  # 不同 session：不受影响
        sidecar.LIMITER.release_session("s-B", token_b)
    finally:
        sidecar.LIMITER.release_session("s-A", token_a)

    again = sidecar.LIMITER.acquire_session("s-A")        # 释放后可再次获取
    sidecar.LIMITER.release_session("s-A", again)


def test_same_session_requests_do_not_overlap(client, monkeypatch):
    """同 session 串行：两个请求的处理区间不能交叉"""
    timeline: list[tuple[str, float]] = []
    lock = threading.Lock()

    def slow(messages):
        with lock:
            timeline.append(("start", time.monotonic()))
        time.sleep(0.15)
        with lock:
            timeline.append(("end", time.monotonic()))
        return GOOD

    monkeypatch.setattr(sidecar, "DEPS", Deps(nl2sql_llm=slow,
                                              summary_llm=lambda m: "共 8 个任务。"))
    # 两个请求用**不同问题**：否则第二个会被问题→SQL 缓存接住，根本不进模型桩，
    # 也就观测不到"是否重叠"（实测踩到）
    questions = ["待接取的任务有几个？", "已结算的任务有几个？"]
    threads = [threading.Thread(target=lambda i=i: client.post(
        "/v1/ai/chat", json=dict(BODY, question=questions[i],
                                 client_msg_id="c_%d" % i))) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    spans = []
    pending_start = None
    for kind, ts in sorted(timeline, key=lambda x: x[1]):
        if kind == "start":
            pending_start = ts
        else:
            spans.append((pending_start, ts))
    assert len(spans) == 2
    (s1, e1), (s2, e2) = spans
    assert s2 >= e1 - 1e-6, "同 session 的两个请求发生了重叠"


# ------------------------------------------------------------------ 端到端预算
def test_budget_exhaustion_degrades_honestly(client, monkeypatch):
    """超预算时给 error + done(timeout)，既不 500 也不假装成功"""
    monkeypatch.setenv("SIDECAR_TIMEOUT_MS", "300")

    def slow(messages):
        time.sleep(0.5)
        return GOOD

    monkeypatch.setattr(sidecar, "DEPS", Deps(nl2sql_llm=slow,
                                              summary_llm=lambda m: "共 8 个任务。"))
    r = client.post("/v1/ai/chat", json=BODY)

    assert r.status_code == 200, "预算耗尽不该是 500"
    assert "event: error" in r.text and "LLM_UNAVAILABLE" in r.text
    assert '"timeout": true' in r.text
    assert r.text.rstrip().endswith("}"), "必须以 done 帧收尾，前端据此收流"


def test_within_budget_no_timeout_event(client, monkeypatch):
    monkeypatch.setenv("SIDECAR_TIMEOUT_MS", "8000")
    r = client.post("/v1/ai/chat", json=BODY)
    assert "event: error" not in r.text and '"timeout": true' not in r.text
