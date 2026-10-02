# -*- coding: utf-8 -*-
"""会话存储（P3）：多轮持久化 + **用户隔离**。

最要紧的两条：
1. **越权读取**：用户 8 不能借用户 7 的 `external_id` 读到 7 的历史 —— 每个查询都带 user_id；
2. **best-effort**：会话库出问题只降级为"无历史"，绝不让问答失败。
其余是契约 §2.1 四个会话端点的读写语义（真实 MySQL / treatbord_test，CI 用 sqlite 时跳过）。
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import session as sess  # noqa: E402
from config import IS_SQLITE  # noqa: E402

needs_mysql = pytest.mark.skipif(IS_SQLITE, reason="会话落库需要真实 MySQL（CI 用 sqlite 快照）")


@pytest.fixture
def store(monkeypatch):
    """打开会话开关 + 指向测试库，并在结束后清理本用例造的数据"""
    monkeypatch.setenv("SESSION_ENABLED", "true")
    monkeypatch.setenv("SESSION_DB_NAME", "treatbord_test")
    ext = "pytest-sess-%s" % uuid.uuid4().hex[:8]
    yield ext
    # 清理：按 external_id 找到会话，删消息与会话
    try:
        with sess._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM ai_chat_session WHERE external_id=%s", (ext,))
            ids = [r[0] for r in cur.fetchall()]
            for sid in ids:
                cur.execute("DELETE FROM ai_chat_message WHERE session_id=%s", (sid,))
                cur.execute("DELETE FROM ai_chat_session WHERE id=%s", (sid,))
    except Exception:  # noqa: BLE001
        pass


def test_disabled_by_default_never_touches_db(monkeypatch):
    monkeypatch.delenv("SESSION_ENABLED", raising=False)

    def boom():
        raise AssertionError("关闭状态下不应建立任何连接")

    monkeypatch.setattr(sess, "_connect", boom)
    assert sess.enabled() is False
    assert sess.resolve("s1", 7, question="q") is None
    assert sess.history_text("s1", 7) == ""
    assert sess.append_turn(1, 7, "q", "a") is False


def test_persist_failure_is_swallowed(monkeypatch):
    monkeypatch.setenv("SESSION_ENABLED", "true")

    def boom():
        raise RuntimeError("会话库挂了")

    monkeypatch.setattr(sess, "_connect", boom)
    assert sess.resolve("s1", 7, question="q") is None      # 降级为"无会话"
    assert sess.history_text("s1", 7) == ""                 # 降级为"无历史"
    assert sess.append_turn(1, 7, "q", "a") is False


@needs_mysql
def test_resolve_is_idempotent_and_history_round_trips(store):
    ext = store
    sid = sess.resolve(ext, 7, question="我接了几个任务？")
    assert sid, "应创建会话"
    assert sess.resolve(ext, 8, question="我接了几个任务？") != sid, "不同用户必须是不同会话"

    sid7 = sess.resolve(ext, 7, question="再问一次")
    assert sid7 == sid, "同一用户 + 同一 external_id 必须复用会话"

    assert sess.append_turn(sid, 7, "我接了几个任务？", "你接了 8 个任务。") is True
    text = sess.history_text(ext, 7)
    assert "用户：我接了几个任务？" in text and "助手：你接了 8 个任务。" in text
    assert text.index("用户：") < text.index("助手："), "历史必须按时间正序"


@needs_mysql
def test_history_never_leaks_across_users(store):
    """越权读取：用户 8 拿用户 7 的 external_id 也读不到任何历史"""
    ext = store
    sid7 = sess.resolve(ext, 7, question="我的结算金额是多少？")
    sess.append_turn(sid7, 7, "我的结算金额是多少？", "你本月结算 ¥320。")

    assert sess.history_text(ext, 8) == "", "别人的历史一条都不能返回"
    assert sess.resolve(ext, 8) != sid7, "解析出的必须是自己的会话"


@needs_mysql
def test_session_endpoints_semantics(store):
    ext = store
    sid7 = sess.resolve(ext, 7, question="任务有哪些状态？")
    sess.append_turn(sid7, 7, "任务有哪些状态？", "有 OPEN / IN_PROGRESS 等。")

    # GET /v1/ai/sessions —— 只列自己的
    mine = sess.list_sessions(7)
    assert any(s["id"] == sid7 for s in mine)
    assert all(s["id"] != sid7 for s in sess.list_sessions(8)), "不得列出他人会话"

    # GET /v1/ai/sessions/{id}/messages
    msgs = sess.list_messages(sid7, 7)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"].startswith("有 OPEN")
    assert sess.list_messages(sid7, 8) is None, "非本人的会话要当作不存在（404）"

    # DELETE /v1/ai/sessions/{id} —— 先拒他人，再删本人
    assert sess.delete_session(sid7, 8) is False
    assert sess.list_messages(sid7, 7) is not None, "别人的删除请求不得生效"
    assert sess.delete_session(sid7, 7) is True
    assert sess.list_messages(sid7, 7) is None


@needs_mysql
def test_feedback_upsert(store):
    assert sess.save_feedback(999001, 7, 1, "有用") is True
    assert sess.save_feedback(999001, 7, -1, "改主意了") is True      # 同一 message 覆盖
    assert sess.save_feedback(999001, 7, 5) is False, "rating 只接受 1/-1"
    with sess._connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT rating, comment FROM ai_feedback WHERE message_id=%s", (999001,))
        assert cur.fetchone() == (-1, "改主意了")
        cur.execute("DELETE FROM ai_feedback WHERE message_id=%s", (999001,))
