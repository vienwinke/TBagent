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
        # 必须超过「预算 + 1.5s 收尾宽限」，否则结果会在宽限内就绪、被正常返回
        time.sleep(2.0)
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


# ------------------------------------------------------------------ 预算耗尽：也要如实报账
def test_timeout_reports_tokens_already_burned(monkeypatch):
    """边车预算耗尽时，模型已经烧掉的 token 必须报出来（报 0 会让成本统计偏小）。

    用"先记账、再慢慢睡"的桩把预算耗光：pipeline 侧没有 done 可复用，
    所以这条路径只能靠"从固定 Context 里读本请求用量"来补账 —— 这里把它钉住。
    """
    import llm as llm_mod
    from agent.pipeline import Deps

    def slow_accounting_llm(messages):
        scope = llm_mod.usage()          # 先记账（模拟模型已消耗）
        scope.calls += 1
        scope.prompt_tokens += 800
        scope.completion_tokens += 40
        time.sleep(2.0)                  # 拖过「预算 + 收尾宽限」才算真超时
        return {"sql": "SELECT COUNT(*) AS c FROM task WHERE deleted = 0",
                "reason": "慢", "tables": ["task"]}

    monkeypatch.setenv("SIDECAR_DEV_PRINCIPAL", "2:USER")
    monkeypatch.setenv("SIDECAR_TIMEOUT_MS", "300")
    monkeypatch.delenv("SIDECAR_JWT_SECRET", raising=False)
    monkeypatch.setattr(sidecar, "DEPS", Deps(nl2sql_llm=slow_accounting_llm,
                                              summary_llm=lambda m: "ok"))
    sidecar.LIMITER.reset()
    sidecar.STATS.reset()

    res = TestClient(sidecar.app).post("/v1/ai/chat",
                                       json={"session_id": "s-timeout", "question": "待接取的任务有几个？",
                                             "client_msg_id": "t1"})
    done = None
    for line in res.text.splitlines():
        if done is None and line.startswith("data:"):
            pass
    import json as _json
    import re as _re
    match = _re.search(r"event: done\s*\ndata: (.*)", res.text)
    assert match, res.text[:400]
    done = _json.loads(match.group(1))
    assert done.get("timeout") is True, done
    assert done["tokens"] >= 840, "超时也要报出已消耗的 token：%s" % done["tokens"]


# ------------------------------------------------------------------ 204 必须无 body（真实服务器才会报）
def test_204_responses_have_no_body(monkeypatch):
    """204 带 body 会让真实 uvicorn 抛 `Response content longer than Content-Length`
    （TestClient 容忍它，所以这条必须显式断言 body 为空 —— 实测踩到过）。"""
    monkeypatch.setenv("SIDECAR_DEV_PRINCIPAL", "2:USER")
    monkeypatch.delenv("SIDECAR_JWT_SECRET", raising=False)
    monkeypatch.setenv("SESSION_ENABLED", "true")
    monkeypatch.setenv("SESSION_DB_NAME", "treatbord_test")
    sidecar.LIMITER.reset()
    c = TestClient(sidecar.app)

    deleted = c.delete("/v1/ai/sessions/999999")          # 不存在 → 404（有 body 是正常的）
    assert deleted.status_code == 404 and deleted.content

    # 反馈：先造一条真实消息再点赞，拿到 204
    import json as _json
    import re as _re
    from agent.pipeline import Deps
    monkeypatch.setattr(sidecar, "DEPS", Deps(
        nl2sql_llm=lambda m: {"sql": "SELECT COUNT(*) AS c FROM task WHERE deleted = 0",
                              "reason": "t", "tables": ["task"]},
        summary_llm=lambda m: "共 8 个任务。"))
    res = c.post("/v1/ai/chat", json={"session_id": "s-204-body",
                                      "question": "待接取的任务有几个？",
                                      "client_msg_id": "n204"})
    solved = _re.search(r"event: saved\ndata: (.*)", res.text)
    if solved is None:                                     # 未开启会话存储时跳过
        pytest.skip("会话存储未开启，拿不到 message_id")
    mid = int(_json.loads(solved.group(1))["message_id"])
    fb = c.post("/v1/ai/feedback", json={"message_id": mid, "rating": 1})
    assert fb.status_code == 204
    assert fb.content == b"", "204 不能带 body：%r" % fb.content


def test_error_frame_is_still_followed_by_done(monkeypatch):
    """契约要求 `done` 收尾：错误路径也不能少。

    边车原先"见到 error 就 break"，把 pipeline 随后发的 done 吞掉了 ——
    客户端只能干等连接关闭（实测真实链路里看到只有 error 没有 done）。
    """
    from agent.pipeline import Deps

    def failing_llm(messages):
        raise RuntimeError("生成阶段失败: 边端预算已耗尽")

    monkeypatch.setenv("SIDECAR_DEV_PRINCIPAL", "2:USER")
    monkeypatch.delenv("SIDECAR_JWT_SECRET", raising=False)
    monkeypatch.setattr(sidecar, "DEPS", Deps(nl2sql_llm=failing_llm))
    sidecar.LIMITER.reset()
    sidecar.STATS.reset()

    res = TestClient(sidecar.app).post("/v1/ai/chat",
                                       json={"session_id": "s-done", "question": "待接取的任务有几个？",
                                             "client_msg_id": "d1"})
    events = [ln.split(":", 1)[1].strip() for ln in res.text.splitlines() if ln.startswith("event:")]
    assert "error" in events, events
    assert events[-1] == "done", "error 之后必须仍有 done 收尾：%s" % events


def test_result_ready_at_deadline_is_still_delivered(monkeypatch):
    """收尾宽限：pipeline 恰好在预算边界拿到结果时，要**把答案给用户**，不能抢先报超时。

    实测真实链路里差 1ms：pipeline 完成回退、答案已就绪，边车却先判定"预算耗尽"。
    """
    import time as _time

    from agent.pipeline import Deps

    def slow_but_ok(messages):
        _time.sleep(0.45)                      # 比预算略慢，但会成功
        return {"sql": "SELECT COUNT(*) AS c FROM task WHERE deleted = 0",
                "reason": "慢但成功", "tables": ["task"]}

    monkeypatch.setenv("SIDECAR_DEV_PRINCIPAL", "2:USER")
    monkeypatch.setenv("SIDECAR_TIMEOUT_MS", "300")     # 预算 0.3s < 0.45s
    monkeypatch.delenv("SIDECAR_JWT_SECRET", raising=False)
    monkeypatch.setattr(sidecar, "DEPS", Deps(nl2sql_llm=slow_but_ok,
                                              summary_llm=lambda m: "共 8 个任务。"))
    sidecar.LIMITER.reset()
    sidecar.STATS.reset()

    res = TestClient(sidecar.app).post("/v1/ai/chat",
                                       json={"session_id": "s-grace", "question": "待接取的任务有几个？",
                                             "client_msg_id": "g1"})
    events = [ln.split(":", 1)[1].strip() for ln in res.text.splitlines() if ln.startswith("event:")]
    assert "done" in events, events
    assert "error" not in events, "结果已就绪就不该报超时：%s" % events
    assert "共 8 个任务" in res.text or "查询结果" in res.text
