# -*- coding: utf-8 -*-
"""护栏第 1 层（sqlglot 静态校验）单测：危险 SQL 必须全部被拒"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.sql_guard import SqlGuardError, is_safe, validate  # noqa: E402

ALLOWED = [
    "SELECT COUNT(*) FROM task",
    "SELECT id, title, reward FROM task WHERE status = 'OPEN' ORDER BY create_time DESC",
    "SELECT u.nickname, COUNT(t.id) AS cnt FROM user u JOIN task t ON t.publisher_id = u.id GROUP BY u.nickname",
    "WITH c AS (SELECT id FROM task_claim WHERE status='SUBMITTED') SELECT COUNT(*) FROM c",
    "SELECT COUNT(*) FROM task_claim tc JOIN task t ON t.id = tc.task_id WHERE t.status='IN_PROGRESS'",
    "SELECT 1",
]

REJECTED = [
    ("UPDATE task SET title='x' WHERE id=1", "写操作"),
    ("DELETE FROM task_claim", "删除"),
    ("DROP TABLE user", "DDL"),
    ("INSERT INTO task (title) VALUES ('x')", "插入"),
    ("TRUNCATE TABLE audit_log", "截断"),
    ("SELECT 1; DROP TABLE user", "多语句"),
    ("SELECT * FROM mysql.user", "系统库"),
    ("SELECT * FROM information_schema.tables", "系统库"),
    ("SELECT * FROM other_db.user", "跨库"),
    ("SELECT * FROM not_exist_table", "白名单外"),
    ("SELECT SLEEP(10)", "危险函数"),
    ("SELECT BENCHMARK(1000000, MD5('x'))", "危险函数"),
    ("SELECT * FROM user INTO OUTFILE '/tmp/x'", "写文件"),
    ("CREATE TABLE evil (id INT)", "建表"),
    ("", "空 SQL"),
    ("SELECT FROM WHERE", "语法错误"),
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed_sql_passes(sql):
    result = validate(sql)
    assert result.tables is not None
    assert "LIMIT" in result.sql.upper() or sql.strip().upper() == "SELECT 1"


@pytest.mark.parametrize("sql,desc", REJECTED)
def test_dangerous_sql_rejected(sql, desc):
    with pytest.raises(SqlGuardError):
        validate(sql)
    assert not is_safe(sql)


def test_limit_appended_when_missing():
    r = validate("SELECT id FROM task")
    assert r.limit_added is True
    assert r.limit == 200
    assert "LIMIT 200" in r.sql.upper()


def test_limit_capped_when_too_large():
    r = validate("SELECT id FROM task LIMIT 99999")
    assert r.limit_capped is True
    assert r.limit == 200
    assert "99999" not in r.sql


def test_limit_kept_when_within_cap():
    r = validate("SELECT id FROM task LIMIT 10")
    assert r.limit == 10 and not r.limit_added and not r.limit_capped


def test_custom_max_rows():
    r = validate("SELECT id FROM task", max_rows=50)
    assert r.limit == 50


def test_tables_extracted():
    r = validate("SELECT u.nickname FROM user u JOIN task t ON t.publisher_id=u.id")
    assert set(r.tables) == {"user", "task"}


def test_normalized_sql_is_reparseable():
    r = validate("select ID from TASK where status='open'")
    assert is_safe(r.sql)
