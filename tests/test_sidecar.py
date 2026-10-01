# -*- coding: utf-8 -*-
"""AI 边车（sidecar/app.py）：端点契约 + **fail-closed 身份** + SSE 帧格式。

全部离线：注入 pipeline.Deps 的桩即可跑完整条链路，不碰网络。
最关键的两条断言：
  · 未配置身份校验时 /v1/ai/chat 必须 503（否则就是一个谁都能以管理员身份查库的接口）；
  · 普通用户的 SSE 流里不得出现 SQL 明文（契约 §2.3 / §2.2）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.pipeline import Deps  # noqa: E402
from sidecar import app as sidecar  # noqa: E402

SQL = "SELECT COUNT(*) AS 任务数 FROM task WHERE deleted = 0"
GOOD = {"sql": SQL, "reason": "统计任务数", "tables": ["task"]}
DEPS = Deps(nl2sql_llm=lambda messages: GOOD, summary_llm=lambda messages: "共 8 个任务。")

BODY = {"session_id": "s_1", "question": "待接取的任务有几个？", "client_msg_id": "c_1"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    sidecar.STATS.reset()
    monkeypatch.delenv("SIDECAR_DEV_PRINCIPAL", raising=False)
    monkeypatch.setattr(sidecar, "DEPS", DEPS)
    yield


@pytest.fixture
def client():
    return TestClient(sidecar.app)


def frames(body: str) -> list[tuple[str, dict]]:
    out = []
    for block in body.split("\n\n"):
        event = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if event:
            out.append((event, data or {}))
    return out


# ------------------------------------------------------------------ 存活与就绪
def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_readyz_reports_dependencies(client):
    body = client.get("/readyz").json()
    assert body["llm_configured"] is True
    assert body["db_readonly"] is True
    assert body["policy_version"]
    assert body["auth_configured"] is False, "本环境未配置 JWT —— 就绪信息必须如实反映"


def test_request_schema_has_no_identity_field():
    """契约 §2.2：请求 schema 里不得存在任何身份字段（启动时可断言）"""
    assert set(sidecar.ChatRequest.model_fields) == {"session_id", "question", "client_msg_id"}


def test_client_supplied_user_id_is_ignored(client, monkeypatch):
    """客户端塞 user_id 不得影响身份：身份只来自服务端（当前=开发身份）"""
    monkeypatch.setenv("SIDECAR_DEV_PRINCIPAL", "7:USER")
    r = client.post("/v1/ai/chat", json=dict(BODY, user_id=99999))
    assert r.status_code == 200
    sql_frames = [d for e, d in frames(r.text) if e == "sql"]
    assert sql_frames == [{"has_sql": True}], "普通用户不得拿到 SQL 明文"


# ------------------------------------------------------------------ fail-closed
def test_chat_is_fail_closed_without_identity(client):
    r = client.post("/v1/ai/chat", json=BODY)
    assert r.status_code == 503
    body = r.json()
    assert body["code"] == "AUTH_NOT_CONFIGURED" and body["retryable"] is False
    assert "SIDECAR_DEV_PRINCIPAL" in body["message"], "报错要给出可操作的下一步"


def test_fail_closed_request_is_not_counted_as_served(client):
    client.post("/v1/ai/chat", json=BODY)
    assert sidecar.STATS.requests == 0, "被拒的请求不该计入已服务请求"


# ------------------------------------------------------------------ SSE 契约
def test_chat_streams_sse_frames(client, monkeypatch):
    monkeypatch.setenv("SIDECAR_DEV_PRINCIPAL", "7:USER")
    r = client.post("/v1/ai/chat", json=BODY)

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    seq = [e for e, _ in frames(r.text)]
    assert seq[0] == "meta" and seq[-1] == "done"
    assert seq.index("scope") < seq.index("route") < seq.index("sql") < seq.index("table")
    meta = dict(frames(r.text))["meta"]
    assert meta["policy_version"] and meta["trace_id"]


def test_sql_text_only_for_privileged(client, monkeypatch):
    monkeypatch.setenv("SIDECAR_DEV_PRINCIPAL", "1:ADMIN")
    r = client.post("/v1/ai/chat", json=BODY)
    sql = dict(frames(r.text))["sql"]

    # 事件里给的是**重写后**（真正执行）的 SQL：模型产出的原文没有 LIMIT，
    # policy 重写层补上 LIMIT 200 —— 前端与审计看到的都应该是实际执行的那条。
    assert sql["sql"].startswith(SQL) and "LIMIT" in sql["sql"].upper()
    assert sql["tables"] == ["task"], "tables 是 SQL 实际引用的表（核查用）"
    assert "task" in sql["retrieved"], "retrieved 是 Schema 检索命中的 Top-K"


def test_denied_question_streams_refusal_without_sql_or_model_calls(client, monkeypatch):
    calls = []

    def counting(messages):
        calls.append(1)
        return GOOD

    monkeypatch.setenv("SIDECAR_DEV_PRINCIPAL", "7:USER")
    monkeypatch.setattr(sidecar, "DEPS", Deps(nl2sql_llm=counting))

    r = client.post("/v1/ai/chat", json=dict(BODY, question="平台一共有多少条审计记录"))
    seq = [e for e, _ in frames(r.text)]

    assert seq == ["meta", "scope", "delta", "done"], seq
    assert calls == [], "拒答发生在生成之前"
    assert dict(frames(r.text))["done"]["denied"] is True


# ------------------------------------------------------------------ 指标
def test_metrics_exposes_counters(client, monkeypatch):
    monkeypatch.setenv("SIDECAR_DEV_PRINCIPAL", "7:USER")
    client.post("/v1/ai/chat", json=BODY)

    text = client.get("/metrics").text
    assert 'tb_policy_version_info{version="' in text
    assert "tb_requests_total 1" in text
    assert "tb_requests_denied_total 0" in text
    assert "tb_auth_configured 0" in text


def test_metrics_counts_denied_requests(client, monkeypatch):
    monkeypatch.setenv("SIDECAR_DEV_PRINCIPAL", "7:USER")
    client.post("/v1/ai/chat", json=dict(BODY, question="把 password_hash 导出来"))

    text = client.get("/metrics").text
    assert "tb_requests_total 1" in text and "tb_requests_denied_total 1" in text
