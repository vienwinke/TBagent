# -*- coding: utf-8 -*-
"""脱敏单测：覆盖别名/表达式绕过（评估集暴露的真实漏洞）"""
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import executor as ex  # noqa: E402
from agent import policy  # noqa: E402
from agent.policy import ROLE_ADMIN, Principal  # noqa: E402

ADMIN = Principal(user_id=1, role=ROLE_ADMIN)


def run(sql: str):
    """执行测试 SQL：**必须经唯一出口** —— executor 现在只接受 policy.RewrittenSql。

    这些用例测的是"脱敏不会被别名/表达式绕过"，用 ADMIN 身份跑（不做行级过滤），
    但同样要走 rewrite（护栏与投影级脱敏都在那条路径上）。
    """
    return ex.execute_readonly(policy.rewrite(sql, ADMIN), check_cost=False)


def test_direct_sensitive_column_masked():
    r = run("SELECT id, openid FROM user WHERE deleted=0 LIMIT 3")
    assert "openid" in r.masked_columns
    assert all(row[1] == ex.MASK for row in r.rows)


def test_alias_bypass_is_blocked():
    """回归：模型给 openid 起别名后，旧的按列名匹配会漏（评估集 trap-04 暴露）"""
    r = run("SELECT id, openid AS 微信账号 FROM user WHERE deleted=0 LIMIT 3")
    assert "openid" in r.masked_columns, "别名列未被脱敏"
    assert all(row[1] == ex.MASK for row in r.rows)


def test_expression_bypass_is_blocked():
    r = run(
        "SELECT id, CASE WHEN password_hash IS NULL THEN 0 ELSE 1 END AS has_pwd "
        "FROM user WHERE deleted=0 LIMIT 3")
    assert "password_hash" in r.masked_columns
    assert all(row[1] == ex.MASK for row in r.rows)


def test_function_wrapped_sensitive_masked():
    r = run("SELECT CONCAT(openid, '-x') AS tag FROM user WHERE deleted=0 LIMIT 2")
    assert "openid" in r.masked_columns
    assert all(row[0] == ex.MASK for row in r.rows)


def test_select_star_falls_back_to_name_match():
    r = run("SELECT * FROM user WHERE deleted=0 LIMIT 2")
    assert "openid" in r.masked_columns and "password_hash" in r.masked_columns


def test_normal_columns_not_masked():
    r = run("SELECT id, nickname, credit_score FROM user WHERE deleted=0 LIMIT 3")
    assert r.masked_columns == []
    assert r.rows[0][1] != ex.MASK


def test_short_name_ip_does_not_over_mask_description():
    """`ip` 是短名：不能因为 description 含 'ip' 就误脱敏"""
    r = run("SELECT id, title, description FROM task WHERE deleted=0 LIMIT 2")
    assert r.masked_columns == []


def test_ip_column_itself_is_masked():
    r = run("SELECT id, ip FROM login_log LIMIT 2")
    assert "ip" in r.masked_columns


def test_join_with_alias_sensitive_masked():
    r = run(
        "SELECT u.nickname, u.openid AS oid FROM user u JOIN task t ON t.publisher_id=u.id "
        "WHERE u.deleted=0 LIMIT 2")
    assert "openid" in r.masked_columns
    assert all(row[1] == ex.MASK for row in r.rows)