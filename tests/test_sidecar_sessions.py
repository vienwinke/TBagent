# -*- coding: utf-8 -*-
"""契约 §2.1 的会话端点 + 多轮持久化（真实 MySQL / JWT 鉴权）。

除了"能用"，这里重点钉三条**安全语义**：
1. 四个端点都必须鉴权（没有 JWT 一律 401）；
2. 自己的会话只能自己看/删 —— 拿别人的 id 要 **404**（不泄露"这个会话存在"）；
3. 多轮追问不能绕过上一轮的范围判定（"那上个月呢" → 消解成平台级 → 仍拒答）。
"""
from __future__ import annotations

import json
import re
import os
import sys
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 测试写库统一走 TEST_DB：CI 的库名不是 treatbord_test（空库 fixture）
# 使用方式：TEST_DB=treatbord_test（本地）· TEST_DB=tb_ci（CI）
TEST_DB = os.getenv("TEST_DB_NAME", "treatbord_test")

from agent import session as sess  # noqa: E402
from agent.pipeline import Deps  # noqa: E402
from config import IS_SQLITE  # noqa: E402
from sidecar import app as sidecar  # noqa: E402
from sidecar import auth as auth_mod  # noqa: E402

SECRET = "sessions-secret"
needs_mysql = pytest.mark.skipif(IS_SQLITE, reason="会话端点需要真实 MySQL（CI 用 sqlite 快照）")

SQL = "SELECT COUNT(*) AS c FROM task WHERE deleted = 0"


def token(user_id: int, role: str = "USER") -> str:
    now = time.time()
    return auth_mod.sign({"sub": str(user_id), "role": role, "jti": "j-" + uuid.uuid4().hex[:6],
                          "iat": now, "exp": now + 120, "aud": "ai-sidecar"}, SECRET)


def headers(user_id: int, role: str = "USER") -> dict:
    return {"Authorization": "Bearer " + token(user_id, role)}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SIDECAR_JWT_SECRET", SECRET)
    monkeypatch.setenv("SESSION_ENABLED", "true")
    monkeypatch.setenv("SESSION_DB_NAME", TEST_DB)
    monkeypatch.delenv("SIDECAR_DEV_PRINCIPAL", raising=False)
    monkeypatch.setattr(sidecar, "DEPS", Deps(
        nl2sql_llm=lambda m: {"sql": SQL, "reason": "t", "tables": ["task"]},
        summary_llm=lambda m: "共 8 个任务。",
        rewrite_llm=lambda m: {"standalone_question": "平台上个月总成交额是多少",
                               "need_clarify": False}))
    sidecar.LIMITER.reset()
    sidecar.STATS.reset()
    ext = "pytest-http-%s" % uuid.uuid4().hex[:8]
    yield TestClient(sidecar.app), ext
    # 清理：删掉本用例造的会话与消息
    try:
        with sess._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM ai_chat_session WHERE external_id=%s", (ext,))
            for (sid,) in cur.fetchall():
                cur.execute("DELETE FROM ai_chat_message WHERE session_id=%s", (sid,))
                cur.execute("DELETE FROM ai_chat_session WHERE id=%s", (sid,))
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ 鉴权与开关
@needs_mysql
def test_session_endpoints_require_auth(client):
    c, _ = client
    assert c.get("/v1/ai/sessions").status_code == 401
    assert c.get("/v1/ai/sessions/1/messages").status_code == 401
    assert c.delete("/v1/ai/sessions/1").status_code == 401
    assert c.post("/v1/ai/feedback", json={"message_id": 1, "rating": 1}).status_code == 401


@needs_mysql
def test_endpoints_report_disabled_session_store(client, monkeypatch):
    """没开会话存储时如实说 503，而不是回空列表让人误以为真没有历史"""
    c, _ = client
    monkeypatch.delenv("SESSION_ENABLED", raising=False)
    r = c.get("/v1/ai/sessions", headers=headers(7))
    assert r.status_code == 503 and r.json()["code"] == "SESSION_DISABLED"


# ------------------------------------------------------------------ 多轮持久化与越权
@needs_mysql
def test_multiturn_persists_and_rejudges_scope(client):
    """★ 两轮：第一轮被拒也落库；第二轮的追问消解成平台级后 **仍然拒答**"""
    c, ext = client
    first = c.post("/v1/ai/chat", headers=headers(7),
                   json={"session_id": ext, "question": "平台一共有多少条审计记录",
                         "client_msg_id": "m1"})
    assert first.status_code == 200 and '"denied": true' in first.text

    second = c.post("/v1/ai/chat", headers=headers(7),
                    json={"session_id": ext, "question": "那上个月呢", "client_msg_id": "m2"})
    assert second.status_code == 200
    assert '"scope": "PLATFORM"' in second.text and '"denied": true' in second.text, \
        "追问不得绕过上一轮被判定的范围"

    sessions = c.get("/v1/ai/sessions", headers=headers(7)).json()
    mine = next(s for s in sessions if s["title"].startswith("平台一共有多少条"))
    messages = c.get("/v1/ai/sessions/%d/messages" % mine["id"], headers=headers(7)).json()
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert "全平台统计" in messages[1]["content"], "拒答话术也要留痕"


@needs_mysql
def test_sessions_are_not_visible_to_other_users(client):
    c, ext = client
    c.post("/v1/ai/chat", headers=headers(7),
           json={"session_id": ext, "question": "待接取的任务有几个？", "client_msg_id": "m1"})
    sid = next(s["id"] for s in c.get("/v1/ai/sessions", headers=headers(7)).json()
               if s["title"].startswith("待接取"))

    assert all(s["id"] != sid for s in c.get("/v1/ai/sessions", headers=headers(8)).json())
    assert c.get("/v1/ai/sessions/%d/messages" % sid, headers=headers(8)).status_code == 404
    assert c.delete("/v1/ai/sessions/%d" % sid, headers=headers(8)).status_code == 404
    assert c.get("/v1/ai/sessions/%d/messages" % sid, headers=headers(7)).status_code == 200


@needs_mysql
def test_delete_session_removes_messages(client):
    c, ext = client
    c.post("/v1/ai/chat", headers=headers(7),
           json={"session_id": ext, "question": "待接取的任务有几个？", "client_msg_id": "m1"})
    sid = next(s["id"] for s in c.get("/v1/ai/sessions", headers=headers(7)).json()
               if s["title"].startswith("待接取"))

    assert c.delete("/v1/ai/sessions/%d" % sid, headers=headers(7)).status_code == 204
    assert c.get("/v1/ai/sessions/%d/messages" % sid, headers=headers(7)).status_code == 404


# ------------------------------------------------------------------ 反馈
def _saved_message_id(text: str) -> int:
    """从 SSE 文本里取 saved 事件的 message_id"""
    match = re.search(r"event: saved\ndata: (.*)", text)
    assert match, "没有 saved 帧：\n" + text[:300]
    return int(json.loads(match.group(1))["message_id"])


@needs_mysql
def test_feedback_end_to_end_with_ownership(client):
    """👍 走完整链路：saved 事件给出 message_id → 本人 204 · 他人 **404** · 非法 rating 400"""
    c, ext = client
    res = c.post("/v1/ai/chat", headers=headers(7),
                 json={"session_id": ext, "question": "待接取的任务有几个？",
                       "client_msg_id": "fb-1"})
    mid = _saved_message_id(res.text)
    try:
        assert c.post("/v1/ai/feedback", headers=headers(7),
                      json={"message_id": mid, "rating": 1, "comment": "有用"}).status_code == 204
        assert c.post("/v1/ai/feedback", headers=headers(7),
                      json={"message_id": mid, "rating": -1}).status_code == 204
        # 他人不得改这条评价（也不该知道它存在）
        others = c.post("/v1/ai/feedback", headers=headers(8),
                        json={"message_id": mid, "rating": -1, "comment": "踩别人一下"})
        assert others.status_code == 404 and others.json()["code"] == "NOT_FOUND"
        bad = c.post("/v1/ai/feedback", headers=headers(7),
                     json={"message_id": mid, "rating": 5})
        assert bad.status_code == 400
        with sess._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT user_id, rating FROM ai_feedback WHERE message_id=%s", (mid,))
            assert cur.fetchone() == (7, -1), "他人的提交不得改动这一行"
    finally:
        with sess._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM ai_feedback WHERE message_id=%s", (mid,))
