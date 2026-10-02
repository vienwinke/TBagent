# -*- coding: utf-8 -*-
"""会话存储（P3）：多轮对话落到 `ai_chat_session` / `ai_chat_message`。

**设计决策（2026-10-01，方案 A）**：会话由**边车**持有。
· L1/L2 契约里的 `session_id` 是**字符串**（小程序侧），本模块用
  `ai_chat_session.external_id` 承接它，表内关联一律用 BIGINT 主键；
· 契约 §2.1 的 `/v1/ai/sessions*` 本来就是边车端点 —— 会话归边车才自洽。
（备选方案：Java 持有会话、L2 请求里带 history —— 需要改已冻结的契约，故未采用。）

**安全（最容易写错的地方）**：所有读写都**必须带 user_id 过滤**。
会话是用户私有数据，history 泄漏就等于把甲的对话上下文喂给乙。
这里每个查询都带 user_id，并有测试专门钉住"用户 8 读不到用户 7 的历史"。

**可用性**：与 audit 同样的 best-effort —— 失败只记日志、不抛。
多轮是增强能力，不该成为问答链路上的新故障点。
开关：`SESSION_ENABLED`（默认关闭；打开前需建好 `ai_*` 表）。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from loguru import logger

from agent import dbwrite

MAX_TITLE = 64


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default) or default


def enabled() -> bool:
    """是否开启会话持久化（默认关闭）"""
    return _env("SESSION_ENABLED", "false").lower() in ("1", "true", "yes")


def _connect() -> Any:
    return dbwrite.connect("SESSION")


def _title_of(question: str) -> str:
    text = " ".join((question or "").split())
    return text[:MAX_TITLE]


def resolve(external_id: str, user_id: int, *, question: str = "") -> int | None:
    """把外部字符串 session_id 解析成会话主键；不存在则创建。

    **带 user_id 条件**：即使用户拿别人的 external_id 来问，也只会创建/命中自己的会话，
    不会读到别人的历史（越权读取在这里被挡掉）。
    """
    if not enabled() or not external_id:
        return None
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM ai_chat_session WHERE external_id=%s AND user_id=%s",
                        (external_id, user_id))
            row = cur.fetchone()
            if row:
                return int(row[0])
            cur.execute("INSERT INTO ai_chat_session (external_id, user_id, title)"
                        " VALUES (%s,%s,%s)", (external_id, user_id, _title_of(question)))
            return int(cur.lastrowid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[session] 会话解析失败（本次按无会话处理）：{}: {}",
                       type(exc).__name__, str(exc)[:140])
        return None


def history_text(external_id: str, user_id: int, *, limit: int = 6) -> str:
    """取最近 N 轮对话，拼成给"指代消解"用的历史文本（只取**本人**的会话）"""
    if not enabled() or not external_id:
        return ""
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT m.role, m.content FROM ai_chat_message m"
                " JOIN ai_chat_session s ON s.id = m.session_id"
                " WHERE s.external_id=%s AND s.user_id=%s AND m.user_id=%s"
                " ORDER BY m.id DESC LIMIT %s",
                (external_id, user_id, user_id, max(1, limit) * 2))
            rows = list(reversed(cur.fetchall()))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[session] 历史读取失败（本次按无历史处理）：{}: {}",
                       type(exc).__name__, str(exc)[:140])
        return ""

    lines = []
    for role, content in rows:
        who = "用户" if role == "user" else "助手"
        lines.append("%s：%s" % (who, " ".join((content or "").split())[:200]))
    return "\n".join(lines)


def append_turn(session_id: int, user_id: int, question: str, answer: str,
                *, payload: dict | None = None) -> int | None:
    """落一轮对话（用户消息 + 助手消息），并推进会话的 updated_at。

    返回**助手消息 id**（`ai_chat_message.id`）—— 用户反馈（👍/👎）要按它落库，
    没有这个 id，"反馈"就无从关联到具体回答。失败返回 None。
    """
    if not enabled() or not session_id:
        return None
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO ai_chat_message (session_id, user_id, role, content)"
                        " VALUES (%s,%s,%s,%s)", (session_id, user_id, "user", question))
            cur.execute("INSERT INTO ai_chat_message (session_id, user_id, role, content, payload)"
                        " VALUES (%s,%s,%s,%s,%s)",
                        (session_id, user_id, "assistant", answer,
                         json.dumps(payload or {}, ensure_ascii=False)))
            message_id = int(cur.lastrowid)
            cur.execute("UPDATE ai_chat_session SET updated_at=NOW()"
                        " WHERE id=%s AND user_id=%s", (session_id, user_id))
        return message_id
    except Exception as exc:  # noqa: BLE001
        logger.warning("[session] 消息落库失败（不影响本次回答）：{}: {}",
                       type(exc).__name__, str(exc)[:140])
        return None


# ------------------------------------------------------------------ 契约 §2.1 的会话端点
def list_sessions(user_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
    """`GET /v1/ai/sessions` —— 只列**本人**的会话"""
    if not enabled():
        return []
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, title, updated_at FROM ai_chat_session"
                    " WHERE user_id=%s ORDER BY updated_at DESC LIMIT %s",
                    (user_id, max(1, min(limit, 100))))
        return [{"id": r[0], "title": r[1], "updated_at": str(r[2])} for r in cur.fetchall()]


def list_messages(session_id: int, user_id: int, *, limit: int = 100) -> list[dict[str, Any]] | None:
    """`GET /v1/ai/sessions/{id}/messages` —— 非本人的会话返回 None（上层转 404，不泄露存在性）"""
    if not enabled():
        return []
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM ai_chat_session WHERE id=%s AND user_id=%s",
                    (session_id, user_id))
        if cur.fetchone() is None:
            return None
        cur.execute("SELECT role, content, payload, created_at FROM ai_chat_message"
                    " WHERE session_id=%s AND user_id=%s ORDER BY id LIMIT %s",
                    (session_id, user_id, max(1, min(limit, 500))))
        out = []
        for role, content, payload, created in cur.fetchall():
            try:
                payload = json.loads(payload) if payload else {}
            except (TypeError, ValueError):
                payload = {}
            out.append({"role": role, "content": content, "payload": payload,
                        "created_at": str(created)})
        return out


def delete_session(session_id: int, user_id: int) -> bool:
    """`DELETE /v1/ai/sessions/{id}` —— 只删**本人**的会话，返回是否删掉了"""
    if not enabled():
        return False
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM ai_chat_session WHERE id=%s AND user_id=%s",
                    (session_id, user_id))
        if cur.fetchone() is None:
            return False
        cur.execute("DELETE FROM ai_chat_message WHERE session_id=%s AND user_id=%s",
                    (session_id, user_id))
        cur.execute("DELETE FROM ai_chat_session WHERE id=%s AND user_id=%s",
                    (session_id, user_id))
    return True


# 评论脱敏（保守兜底，不是完备的 DLP）：评论是**用户自由文本**，
# 完全可能贴手机号、邮箱或一串密钥 —— 与"审计类表不记录敏感值"的原则冲突。
# 这里只挡住最典型的三类；真正的 DLP 应在更上游做。
_COMMENT_REDACTIONS = [
    (re.compile(r"[A-Za-z0-9_\-]{32,}"), "[已脱敏]"),          # 长不透明串（密钥/token）
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[已脱敏]"),      # 邮箱
    (re.compile(r"1[3-9]\d{9}"), "[已脱敏]"),                    # 手机号
]


def sanitize_comment(comment: str, *, limit: int = 255) -> str:
    """评论入库前清洗：去控制字符 → 遮蔽典型敏感值 → 截断"""
    text = "".join(ch for ch in (comment or "") if ch == "\n" or ch >= " ")
    for pattern, replacement in _COMMENT_REDACTIONS:
        text = pattern.sub(replacement, text)
    return text[:limit]


def save_feedback(message_id: int, user_id: int, rating: int, comment: str = "") -> str:
    """`POST /v1/ai/feedback` —— best-effort，返回状态字符串供上层映射状态码。

    返回值：`ok` / `not_owner`（这条回答不是该用户的）/ `disabled`（未开会话存储）/ `failed`。

    ★ 归属校验是必须的：`ai_feedback` 的主键是 `message_id` 且用 `ON DUPLICATE KEY UPDATE`
    覆盖，不校验归属的话，用户 A 拿别人的 message_id 提交就能**改掉 B 的评价**
    （行内 user_id 还留在 B 名下，数据被改却记在别人头上）。
    """
    if not enabled():
        return "disabled"
    if rating not in (1, -1):
        return "failed"
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM ai_chat_message WHERE id=%s AND user_id=%s",
                        (message_id, user_id))
            if cur.fetchone() is None:
                logger.warning("[session] 反馈被拒：消息 {} 不属于用户 {}", message_id, user_id)
                return "not_owner"
            cur.execute("INSERT INTO ai_feedback (message_id, user_id, rating, comment)"
                        " VALUES (%s,%s,%s,%s)"
                        " ON DUPLICATE KEY UPDATE rating=VALUES(rating), comment=VALUES(comment)",
                        (message_id, user_id, rating, sanitize_comment(comment)))
        return "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning("[session] 反馈落库失败：{}: {}", type(exc).__name__, str(exc)[:140])
        return "failed"
