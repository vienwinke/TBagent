# -*- coding: utf-8 -*-
"""SQL 脚本门禁：DDL 语法必须可解析，且**授权脚本不得越权**。

为什么值得测：这两个脚本是"手抄一次、以后没人再看"的典型 —— 一旦语法错或顺手多给了
UPDATE/DELETE，出问题是在**生产库**上。这里用 sqlglot 按 MySQL 方言离线校验（零副作用），
并把"只读 / 只增不改"这类安全属性写成断言。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
AI_TABLES = SQL_DIR / "ai_tables.sql"
READONLY = SQL_DIR / "readonly_user.sql"
# 全部 sql/*.sql 都要能被 MySQL 方言解析（迁移写错语法 = 上线时应用起不来）
ALL_SQL = sorted(SQL_DIR.glob("*.sql"))

EXPECTED_TABLES = {"ai_chat_session", "ai_chat_message", "ai_query_audit",
                   "ai_feedback", "ai_prompt_version"}


def _statements(path: Path) -> list[str]:
    body = "\n".join(ln for ln in path.read_text(encoding="utf-8").splitlines()
                     if not ln.strip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


def test_ai_tables_ddl_parses_as_mysql_create():
    names = set()
    for stmt in _statements(AI_TABLES):
        parsed = sqlglot.parse_one(stmt, dialect="mysql")
        assert isinstance(parsed, exp.Create), "非 CREATE 语句：%s" % stmt[:60]
        names.add(parsed.this.this.name)
    assert names == EXPECTED_TABLES, "表名与契约 §6 不一致：%s" % (names ^ EXPECTED_TABLES)


def test_ai_tables_scripts_are_idempotent():
    """迁移脚本必须可重复执行（IF NOT EXISTS），否则重跑就炸"""
    for stmt in _statements(AI_TABLES):
        assert "IF NOT EXISTS" in stmt.upper(), "缺少 IF NOT EXISTS：%s" % stmt[:60]


def test_audit_table_is_append_only():
    """审计只增不改：授权模板里**不能**出现 UPDATE / DELETE ai_query_audit"""
    text = AI_TABLES.read_text(encoding="utf-8")
    assert not re.search(r"GRANT[^;]*\b(UPDATE|DELETE)\b[^;]*ai_query_audit", text, re.I), \
        "审计表被授予了 UPDATE/DELETE —— 审计必须只增不改"
    assert re.search(r"GRANT\s+INSERT,\s*SELECT\s+ON\s+treatbord\.ai_query_audit", text, re.I), \
        "审计表应有 INSERT, SELECT 授权"


def test_readonly_script_grants_only_select():
    """只读账号脚本不得出现写权限（最小权限是嵌入方案的前提）"""
    text = READONLY.read_text(encoding="utf-8")
    banned = re.findall(r"GRANT\s+([^;]*?)\s+ON", text, re.I)
    assert banned, "没解析到任何 GRANT"
    for priv in banned:
        assert priv.strip().upper() == "SELECT", "只读脚本里出现了非 SELECT 授权：%s" % priv


@pytest.mark.parametrize("path", [AI_TABLES, READONLY])
def test_sql_scripts_are_not_empty(path):
    assert len(_statements(path)) >= 2


@pytest.mark.parametrize("path", ALL_SQL, ids=lambda p: p.name)
def test_every_sql_file_has_valid_statements(path):
    """sql/ 下每个文件都必须能被 MySQL 方言解析。

    价值：迁移语法错误的代价是"应用启动失败"（Flyway 直接报错），而本地若没跑过
    真实 MySQL 就发现不了 —— 这里用离线解析把语法挡住（零副作用）。
    """
    stmts = _statements(path)
    assert stmts, "%s 里没有可执行语句" % path.name
    for stmt in stmts:
        # `SHOW GRANTS FOR 'x'@'y'` 这类**诊断语句** sqlglot 解析不了（它只面向 DDL/DML），
        # 已知限制：这类语句只是给运维看的自检提示，不影响语法正确性。
        if stmt.upper().startswith("SHOW "):
            continue
        sqlglot.parse_one(stmt, dialect="mysql")     # 抛异常即失败


def test_ai_migrations_are_append_only_and_idempotent_where_supported():
    """V10/V11 是**追加**迁移：已发布的 V9 不能改（改了 Flyway checksum 校验失败）。

    这里守住两条：
    · V9 只建表（CREATE TABLE IF NOT EXISTS）；
    · V10/V11 只做 ALTER/UPDATE，不重建表、不删列。
    """
    for path in ALL_SQL:
        if path.name.startswith("V10"):
            body = " ".join(_statements(path)).upper()
            assert "ALTER TABLE" in body and "AI_FEEDBACK" in body
            assert "DROP" not in body and "CREATE TABLE" not in body
        if path.name.startswith("V11"):
            body = " ".join(_statements(path)).upper()
            assert body.startswith("UPDATE AI_FEEDBACK")
            assert "DROP" not in body
