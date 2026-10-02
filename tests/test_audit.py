# -*- coding: utf-8 -*-
"""审计落库（契约 §4）：字段与 DDL 一致、best-effort、以及最关键的**服务端专属**语义。

最容易被写错的一条：普通用户的 SSE 里只有 `has_sql:true`（SQL 明文不下发），
但审计表必须留下 generated_sql / rewritten_sql 以便追溯 —— 这两件事同时成立靠的是
pipeline 内部那条 `_audit` 事件：只在服务端流转、公开流里被过滤掉。这里把它钉死。
"""
from __future__ import annotations

import json
import re
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 测试写库统一走 TEST_DB：CI 的库名不是 treatbord_test（空库 fixture）
# 使用方式：TEST_DB=treatbord_test（本地）· TEST_DB=tb_ci（CI）
TEST_DB = os.getenv("TEST_DB_NAME", "treatbord_test")

from agent import audit as audit_mod  # noqa: E402
from agent import pipeline  # noqa: E402
from agent.pipeline import Deps  # noqa: E402
from agent.policy import Principal  # noqa: E402
from config import IS_SQLITE  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SQL = "SELECT COUNT(*) AS 任务数 FROM task WHERE deleted = 0"
GOOD = {"sql": SQL, "reason": "统计任务数", "tables": ["task"]}
USER = Principal(user_id=7)
ADMIN = Principal(user_id=1, role="ADMIN")


def _ddl_columns(table: str) -> list[str]:
    """从 sql/ai_tables.sql 里解析某张表的列名（用于和 AuditRow 对齐）"""
    text = (ROOT / "sql" / "ai_tables.sql").read_text(encoding="utf-8")
    body = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("--"))
    match = re.search(r"CREATE TABLE IF NOT EXISTS %s \((.*?)\n\)" % table, body, re.S)
    assert match, "未找到 %s 的建表语句" % table
    cols = []
    for line in match.group(1).splitlines():
        line = line.strip().rstrip(",")
        if not line or line.upper().startswith(("PRIMARY KEY", "KEY", "UNIQUE")):
            continue
        cols.append(line.split()[0])
    return cols


# ------------------------------------------------------------------ 契约对齐
def test_audit_columns_match_ddl():
    """代码里的列名必须与建表脚本一致，否则线上就是"插入失败"级别的故障。

    允许脚本多出两类列：自增主键 `id` 与有默认值的 `created_at` —— 它们由数据库自己填，
    代码不显式插入；除此之外**多一列少一列都算不一致**。
    """
    ddl = _ddl_columns("ai_query_audit")
    assert set(audit_mod.COLUMNS) <= set(ddl)
    assert set(ddl) - set(audit_mod.COLUMNS) == {"id", "created_at"}


def test_as_params_serializes_json_and_booleans():
    row = audit_mod.AuditRow(trace_id="t1", user_id=7, question="q",
                             detected_tables=["task"], masked_columns=["openid"],
                             truncated=True, cache_hit=False, repaired=True)
    params = row.as_params()
    assert len(params) == len(audit_mod.COLUMNS)
    data = dict(zip(audit_mod.COLUMNS, params))
    assert json.loads(data["detected_tables"]) == ["task"]
    assert json.loads(data["masked_columns"]) == ["openid"]
    assert data["truncated"] == 1 and data["cache_hit"] == 0 and data["repaired"] == 1


# ------------------------------------------------------------------ 默认关闭与 best-effort
def test_disabled_by_default_never_touches_db(monkeypatch):
    monkeypatch.delenv("AUDIT_ENABLED", raising=False)

    def boom():
        raise AssertionError("关闭状态下不应建立任何连接")

    monkeypatch.setattr(audit_mod, "_connect", boom)
    assert audit_mod.enabled() is False
    assert audit_mod.record(audit_mod.AuditRow(trace_id="t", user_id=1, question="q")) is False


def test_record_is_best_effort_on_failure(monkeypatch):
    """审计失败绝不能把在线问答带崩：只记计数与日志"""
    monkeypatch.setenv("AUDIT_ENABLED", "true")

    def boom():
        raise RuntimeError("数据库连接失败")

    monkeypatch.setattr(audit_mod, "_connect", boom)
    before = audit_mod.stats()["failed"]
    assert audit_mod.record(audit_mod.AuditRow(trace_id="t", user_id=1, question="q")) is False
    assert audit_mod.stats()["failed"] == before + 1


# ------------------------------------------------------------------ 服务端专属（安全语义）
def test_audit_receives_sql_while_client_stream_does_not():
    captured: list[dict] = []
    events = list(pipeline.answer_stream("待接取的任务有几个？", USER, use_cache=False,
                                         deps=Deps(nl2sql_llm=lambda m: GOOD,
                                                   summary_llm=lambda m: "共 8 个任务。"),
                                         audit_sink=captured.append))

    # 客户端侧：普通用户只能看到 has_sql，没有 SQL 明文，也没有 _audit 事件
    sql_events = [d for e, d in events if e == "sql"]
    assert sql_events == [{"has_sql": True}]
    assert all(e != "_audit" for e, _ in events), "_audit 绝不能发给客户端"

    # 服务端侧：审计拿到了 SQL 明文与裁决
    assert len(captured) == 1
    row = captured[0]
    assert row["generated_sql"] == SQL and row["rewritten_sql"]
    assert row["trace_id"] and row["user_id"] == 7
    assert row["verdict"] == "ok" and row["scope"] and row["route"] == "data"
    assert "task" in row["detected_tables"], "detected_tables 记的是检索命中的表"
    assert row["prompt_tokens"] is not None and row["policy_version"]


def test_audit_sink_failure_does_not_break_the_stream():
    def boom(payload):
        raise RuntimeError("审计库挂了")

    events = list(pipeline.answer_stream("待接取的任务有几个？", USER, use_cache=False,
                                         deps=Deps(nl2sql_llm=lambda m: GOOD,
                                                   summary_llm=lambda m: "共 8 个任务。"),
                                         audit_sink=boom))
    assert [e for e, _ in events][-1] == "done", "审计抛异常也要把问答跑完"


def test_denied_question_is_audited_with_reason():
    captured: list[dict] = []
    list(pipeline.answer_stream("平台一共有多少条审计记录", USER, use_cache=False,
                                deps=Deps(), audit_sink=captured.append))
    row = captured[0]
    assert row["verdict"] == "denied" and row["deny_reason"] == "DENY_PLATFORM"
    assert row["scope"] == "PLATFORM" and row["route"] is None


# ------------------------------------------------------------------ 真实落库（需要 MySQL）
needs_mysql = pytest.mark.skipif(IS_SQLITE, reason="审计落库需要真实 MySQL（CI 用 sqlite 快照）")


@needs_mysql
def test_record_writes_real_row(monkeypatch):
    """真写一行到 treatbord_test 再读回来（不给生产库添数据）"""
    monkeypatch.setenv("AUDIT_ENABLED", "true")
    monkeypatch.setenv("AUDIT_DB_NAME", TEST_DB)
    row = audit_mod.AuditRow(trace_id="pytest-audit-1", user_id=7, question="审计自检",
                             route="data", scope="SELF", verdict="ok",
                             detected_tables=["task"], generated_sql=SQL, rewritten_sql=SQL,
                             policy_version="pol-test", row_count=1, latency_ms=12,
                             prompt_tokens=100, completion_tokens=10, cost_yuan=0.001,
                             model="test-model")
    try:
        assert audit_mod.record(row) is True
        with audit_mod._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT user_id, verdict, detected_tables, cost_yuan FROM ai_query_audit"
                        " WHERE trace_id=%s", ("pytest-audit-1",))
            got = cur.fetchone()
        assert got is not None, "审计行没有落库"
        assert got[0] == 7 and got[1] == "ok"
        assert json.loads(got[2]) == ["task"]
    finally:
        with audit_mod._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM ai_query_audit WHERE trace_id=%s", ("pytest-audit-1",))
