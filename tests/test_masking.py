# -*- coding: utf-8 -*-
"""脱敏单测：覆盖别名/表达式绕过（评估集暴露的真实漏洞）"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import executor as ex  # noqa: E402


def test_direct_sensitive_column_masked():
    r = ex.execute_readonly("SELECT id, openid FROM user WHERE deleted=0 LIMIT 3")
    assert "openid" in r.masked_columns
    assert all(row[1] == ex.MASK for row in r.rows)


def test_alias_bypass_is_blocked():
    """回归：模型给 openid 起别名后，旧的按列名匹配会漏（评估集 trap-04 暴露）"""
    r = ex.execute_readonly("SELECT id, openid AS 微信账号 FROM user WHERE deleted=0 LIMIT 3")
    assert "openid" in r.masked_columns, "别名列未被脱敏"
    assert all(row[1] == ex.MASK for row in r.rows)


def test_expression_bypass_is_blocked():
    r = ex.execute_readonly(
        "SELECT id, CASE WHEN password_hash IS NULL THEN 0 ELSE 1 END AS has_pwd "
        "FROM user WHERE deleted=0 LIMIT 3")
    assert "password_hash" in r.masked_columns
    assert all(row[1] == ex.MASK for row in r.rows)


def test_function_wrapped_sensitive_masked():
    r = ex.execute_readonly("SELECT CONCAT(openid, '-x') AS tag FROM user WHERE deleted=0 LIMIT 2")
    assert "openid" in r.masked_columns
    assert all(row[0] == ex.MASK for row in r.rows)


def test_select_star_falls_back_to_name_match():
    r = ex.execute_readonly("SELECT * FROM user WHERE deleted=0 LIMIT 2")
    assert "openid" in r.masked_columns and "password_hash" in r.masked_columns


def test_normal_columns_not_masked():
    r = ex.execute_readonly("SELECT id, nickname, credit_score FROM user WHERE deleted=0 LIMIT 3")
    assert r.masked_columns == []
    assert r.rows[0][1] != ex.MASK


def test_short_name_ip_does_not_over_mask_description():
    """`ip` 是短名：不能因为 description 含 'ip' 就误脱敏"""
    r = ex.execute_readonly("SELECT id, title, description FROM task WHERE deleted=0 LIMIT 2")
    assert r.masked_columns == []


def test_ip_column_itself_is_masked():
    r = ex.execute_readonly("SELECT id, ip FROM login_log LIMIT 2")
    assert "ip" in r.masked_columns


def test_join_with_alias_sensitive_masked():
    r = ex.execute_readonly(
        "SELECT u.nickname, u.openid AS oid FROM user u JOIN task t ON t.publisher_id=u.id "
        "WHERE u.deleted=0 LIMIT 2")
    assert "openid" in r.masked_columns
    assert all(row[1] == ex.MASK for row in r.rows)