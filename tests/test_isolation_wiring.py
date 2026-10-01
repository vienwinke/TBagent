# -*- coding: utf-8 -*-
"""隔离接线守卫：证明 SQL **真的**经过 policy（唯一出口）

之前的教训：policy 实现完整、41 条红线全绿，但生产代码零调用 —— 测试全绿而产品不安全。
这个文件专门钉住"接线"本身，而不只是钉住 policy 的逻辑。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import cache as cache_mod  # noqa: E402
from agent import executor as ex  # noqa: E402
from agent import nl2sql, policy  # noqa: E402
from agent.policy import Principal  # noqa: E402

USER = Principal(user_id=7)
RAW_SQL = "SELECT COUNT(*) AS c FROM task_claim WHERE deleted = 0"


def _fake_qr(sql: str) -> ex.QueryResult:
    return ex.QueryResult(sql=sql, columns=["c"], rows=[(3,)], row_count=3, elapsed_ms=1)


def _capture_execute(monkeypatch, captured: list):
    """拦截真正送库的 SQL：唯一能观测到"最终执行了什么"的位置"""

    def fake(rewritten, **kwargs):
        captured.append(rewritten)
        return _fake_qr(rewritten.sql)

    monkeypatch.setattr(policy, "execute", fake)


def _llm(sql: str):
    return lambda messages: {"sql": sql, "reason": "test", "tables": ["task_claim"]}


# ---------------------------------------------------------------- 接线本身
def test_answer_goes_through_policy_rewrite(monkeypatch):
    calls = []
    real = policy.rewrite

    def counting(sql, principal):
        calls.append(sql)
        return real(sql, principal)

    monkeypatch.setattr(policy, "rewrite", counting)
    _capture_execute(monkeypatch, captured := [])

    r = nl2sql.answer("我接了几个任务", principal=USER, use_cache=False, llm_fn=_llm(RAW_SQL))

    assert calls == [RAW_SQL], "生成的 SQL 必须原样交给 policy.rewrite"
    assert captured, "policy.execute 必须被调用（而不是直连 executor）"
    assert r.ok and r.isolated is True


def test_user_sql_is_row_filtered(monkeypatch):
    captured = []
    _capture_execute(monkeypatch, captured)

    nl2sql.answer("我接了几个任务", principal=USER, use_cache=False, llm_fn=_llm(RAW_SQL))

    sent = captured[0].sql
    assert "SELECT * FROM task_claim WHERE" in sent, "未发生派生表替换：%s" % sent
    assert "user_id = 7" in sent, "未注入本人过滤：%s" % sent


def test_cache_hit_is_also_isolated(monkeypatch):
    """第二个绕过点：缓存命中后也必须重新 rewrite（缓存里是重写前 SQL）"""

    class FakeCache:
        def get(self, question, top_k=None, *, scope=""):
            return {"sql": RAW_SQL, "tables": ["task_claim"]}

        def put(self, *a, **k):
            raise AssertionError("命中缓存不应再写缓存")

    monkeypatch.setattr(cache_mod, "cache", lambda: FakeCache())
    captured = []
    _capture_execute(monkeypatch, captured)

    def boom(messages):
        raise AssertionError("缓存命中不应调用 LLM")

    r = nl2sql.answer("我接了几个任务", principal=USER, use_cache=True, llm_fn=boom)

    assert r.cache_hit is True
    assert "user_id = 7" in captured[0].sql, "缓存路径绕过了行级隔离：%s" % captured[0].sql


# ---------------------------------------------------------------- 单机与拒答
def test_no_principal_is_single_user_admin_and_not_isolated(monkeypatch):
    captured = []
    _capture_execute(monkeypatch, captured)

    r = nl2sql.answer("接了几个任务", principal=None, use_cache=False, llm_fn=_llm(RAW_SQL))

    assert r.isolated is False, "单机模式必须显式标记为未隔离"
    assert nl2sql._local_principal().is_privileged is True
    assert "user_id = 7" not in captured[0].sql, "管理员不应被注入行过滤"


def test_platform_scope_denial_is_terminal_and_never_touches_db(monkeypatch):
    """语义越权：不回环、不查库，直接拒答

    G2 之前：拒答发生在 policy.rewrite（模型先产出一条引用了 audit_log 的 SQL 才被拦），
    所以这里断言"生成调用 1 次"。G2 把范围判定前移到生成之前 —— 现在连生成都不做，
    因此断言改为 0 次：拒答不再依赖"模型恰好写到了那张不该写的表"。
    """
    llm_calls = []
    captured = []
    _capture_execute(monkeypatch, captured)

    def llm(messages):
        llm_calls.append(1)
        return {"sql": "SELECT COUNT(*) FROM audit_log", "reason": "test"}

    r = nl2sql.answer("平台一共有多少条审计记录", principal=USER, use_cache=False, llm_fn=llm,
                      max_repair=1)

    assert r.stage == "denied" and r.deny_reason == policy.DENY_PLATFORM
    assert r.scope == "PLATFORM", "拒答必须带上语义层判定的范围，便于审计"
    assert r.ok is False
    assert len(llm_calls) == 0, "语义层应在生成之前拦下，不该再花一次模型调用"
    assert captured == [], "被拒的查询绝不能送到数据库"


def test_guard_rejection_repairs_once(monkeypatch):
    """可修复的拦截（写操作）应该回环一次，并在第二次成功"""
    seen = []
    captured = []
    _capture_execute(monkeypatch, captured)

    def llm(messages):
        seen.append(messages[-1]["content"])          # user 消息里才有回环回灌的内容
        if len(seen) == 1:
            return {"sql": "UPDATE task SET reward = 1", "reason": "第一次是写操作"}
        return {"sql": RAW_SQL, "reason": "改正为只读"}

    r = nl2sql.answer("把赏金改成 1 元", principal=USER, use_cache=False, llm_fn=llm)

    assert r.ok and r.repaired is True and r.attempts == 2
    assert "上一次尝试失败" in seen[1], "第二次调用没有带上失败上下文"
    assert "UPDATE task" in seen[1] and "策略拦截" in seen[1], "上次 SQL 与失败原因必须回灌"
    assert captured and "user_id = 7" in captured[0].sql


# ---------------------------------------------------------------- Schema 裁剪
def test_schema_and_hits_are_scoped_to_role(monkeypatch):
    seen = {}
    _capture_execute(monkeypatch, [])

    def llm(messages):
        seen["system"] = messages[0]["content"]
        return {"sql": RAW_SQL, "reason": "test"}

    r = nl2sql.answer("我接了几个任务", principal=USER, use_cache=False, llm_fn=llm)

    visible = set(policy.visible_tables(USER))
    assert set(r.tables) <= visible, "命中了角色不可见的表：%s" % (set(r.tables) - visible)
    assert not (set(r.tables) & policy.ADMIN_ONLY_TABLES)
    for hidden in policy.ADMIN_ONLY_TABLES:
        assert "表 %s（" % hidden not in seen["system"], "USER 的提示词里出现了 %s 的列结构" % hidden


# ---------------------------------------------------------------- 静态守卫
def test_no_bypass_left_in_model_and_eval_paths():
    """源码级守卫：模型路径与评测路径不得再出现直连执行器/静态护栏的调用

    注意：policy.execute 内部、scripts/ 诊断脚本、tests/test_masking.py（测脱敏本身）
    是**故意**直连的，不在本守卫范围内。
    """
    root = Path(__file__).resolve().parents[1]
    banned = ("ex.execute_readonly(", "sql_guard.validate(")
    for rel in ("agent/nl2sql.py", "eval/run_eval.py"):
        src = (root / rel).read_text(encoding="utf-8")
        for pat in banned:
            assert pat not in src, "%s 里仍有绕过唯一出口的调用：%s" % (rel, pat)


# ---------------------------------------------------------------- 类型约束（决策 B）
def test_executor_rejects_raw_sql_strings():
    """唯一出口的**硬保证**：executor 只接受 policy.RewrittenSql。

    在此之前"所有 SQL 必须过 policy.rewrite"只是约定 + 源码正则守卫
    （只扫 nl2sql 与 run_eval 两个文件）—— 新增一条路径就能绕过。
    现在拿裸字符串直接 TypeError，绕过在类型层面就不可行。
    """
    import pytest

    with pytest.raises(TypeError) as exc:
        ex.execute_readonly("SELECT 1")
    assert "RewrittenSql" in str(exc.value) and "policy.rewrite" in str(exc.value)


def test_executor_accepts_rewritten_sql():
    """走唯一出口就能正常执行（ADMIN 身份不加行过滤）"""
    from agent.policy import ROLE_ADMIN, Principal

    rw = policy.rewrite("SELECT COUNT(*) AS c FROM task WHERE deleted = 0",
                        Principal(user_id=1, role=ROLE_ADMIN))
    qr = ex.execute_readonly(rw, check_cost=False)
    assert qr.row_count == 1
