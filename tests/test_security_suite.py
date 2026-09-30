# -*- coding: utf-8 -*-
"""越权红线门禁：套件必须全绿（拦截率 100%、泄漏 0）

这条用例是 CI 的"红线条"：只要有人改坏了行级重写，它立刻变红。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import policy  # noqa: E402
from agent.policy import Principal  # noqa: E402
from eval.security_suite import run  # noqa: E402


def test_security_suite_is_green():
    r = run()
    assert r["total"] >= 30, "用例数异常（可能没读到 YAML）：%d" % r["total"]
    assert r["failed"] == 0, "存在失败用例：%s" % r["failures"]
    assert r["deny_rate"] == 1.0, "越权拦截率不是 100%%：%.4f" % r["deny_rate"]
    assert r["leaks"] == 0, "存在泄漏：%s" % r["failures"]


def test_selfcheck_catches_unfiltered_reference():
    """元测试：fail-closed 自检必须真的能发现"未过滤引用"，
    否则它只是一段永远返回空列表的装饰"""
    principal = Principal(user_id=7)

    good = policy.rewrite("SELECT COUNT(*) FROM task_claim", principal)
    assert policy.unfiltered_refs(good) == []

    # 手工构造"漏掉过滤"的产物：拿原始 SQL 冒充重写结果
    fake = policy.RewrittenSql(
        sql="SELECT COUNT(*) FROM task_claim",
        tables=["task_claim"],
        principal=principal,
        rewritten_tables=["task_claim"],
    )
    assert policy.unfiltered_refs(fake) == ["task_claim"]


def test_selfcheck_ignores_privileged_roles():
    """运营及以上不做行级重写，自检不应误报"""
    rw = policy.rewrite("SELECT COUNT(*) FROM task_claim", Principal(user_id=2, role="OPERATOR"))
    assert policy.unfiltered_refs(rw) == []
