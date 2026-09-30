# -*- coding: utf-8 -*-
"""行级隔离红线用例（嵌入 treatbord 的上线红线：越权拦截 100%、泄漏 0）

这些用例**不需要数据库**：只验证"送往执行器的 SQL 长什么样"，
这正是行级隔离唯一需要被证明的地方。
"""
import sys
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import policy  # noqa: E402
from agent.policy import Principal, PolicyDenied, rewrite, visible_tables  # noqa: E402

UID = 7
USER = Principal(user_id=UID)
OPERATOR = Principal(user_id=2, role="OPERATOR")
ADMIN = Principal(user_id=1, role="ADMIN")


# ---------------------------------------------------------------- 测试辅助
def _parse(sql: str):
    return sqlglot.parse_one(sql, read="mysql")


def _filtered_refs(sql: str, table: str) -> list[bool]:
    """每个 <table> 引用是否被包在"带 WHERE 的派生表"里

    这是行级隔离最本质的断言：不是"SQL 文本里出现了 user_id = 7"，
    而是"**每一处**该表的引用都处在过滤之内"。文本断言会被别名/格式骗过。
    """
    result = []
    for node in _parse(sql).find_all(exp.Table):
        if (node.name or "").lower() != table:
            continue
        cursor, ok = node, False
        while cursor.parent is not None:
            cursor = cursor.parent
            if isinstance(cursor, exp.Subquery) and cursor.this.args.get("where") is not None:
                ok = True
                break
        result.append(ok)
    return result


def _derived_tables(sql: str) -> set[str]:
    out = set()
    for sub in _parse(sql).find_all(exp.Subquery):
        # ⚠️ sqlglot 30 用 "from_"；用错 key 会让本函数恒返回空集合，
        #    使"断言没有派生表"的用例**假通过**（实测踩到）。
        from_ = None
        if isinstance(sub.this, exp.Select):
            from_ = sub.this.args.get("from_") or sub.this.args.get("from")
        if from_ is not None and isinstance(from_.this, exp.Table):
            out.add((from_.this.name or "").lower())
    return out


# ---------------------------------------------------------------- Principal
def test_principal_rejects_non_int_and_non_positive():
    with pytest.raises(ValueError):
        Principal(user_id="7")            # 字符串 user_id 是最常见的越权入口
    with pytest.raises(ValueError):
        Principal(user_id=0)
    with pytest.raises(ValueError):
        Principal(user_id=7, role="ROOT")


def test_role_rank_is_ordered():
    assert policy.ROLE_RANK["USER"] < policy.ROLE_RANK["OPERATOR"] < policy.ROLE_RANK["ADMIN"]


def test_visible_tables_by_role():
    """三档严格递增：USER ⊂ OPERATOR ⊂ ADMIN"""
    user_tables = visible_tables(USER)
    operator_tables = visible_tables(OPERATOR)
    admin_tables = visible_tables(ADMIN)
    assert user_tables < operator_tables < admin_tables
    # 登录风控下放给运营；操作审计与系统配置只给管理员
    assert "login_log" in operator_tables and "login_log" not in user_tables
    assert "audit_log" not in operator_tables and "audit_log" in admin_tables
    assert "app_config" not in operator_tables and "app_config" in admin_tables


def test_operator_sees_all_rows_without_rewriting():
    """运营及以上不做行级过滤"""
    r = rewrite("SELECT id FROM task_claim", OPERATOR)
    assert _derived_tables(r.sql) == set()
    assert r.rewritten_tables == []


def test_every_user_visible_table_is_registered():
    """fail-closed 守卫：treatbord 将来新增表却没登记策略时，这条会红"""
    missing = sorted(t for t in visible_tables(USER) if t not in policy.USER_POLICY)
    assert missing == [], "新表必须在 USER_POLICY 里登记，否则会被无过滤开放"


# ---------------------------------------------------------------- 行级重写
def test_own_claim_query_gets_filtered():
    r = rewrite("SELECT COUNT(*) AS c FROM task_claim", USER)
    assert _filtered_refs(r.sql, "task_claim") == [True]
    assert "task_claim" in r.rewritten_tables
    assert str(UID) in r.sql


def test_public_task_table_not_filtered():
    r = rewrite("SELECT COUNT(*) FROM task WHERE deleted = 0 AND status = 'OPEN'", USER)
    assert _derived_tables(r.sql) == set()
    assert r.rewritten_tables == []


def test_left_join_escape_is_contained():
    """LEFT JOIN 是最典型的逃逸手法：不能靠 WHERE 过滤（会把外连接退化成内连接）"""
    sql = "SELECT t.id FROM task t LEFT JOIN task_claim tc ON 1 = 1"
    r = rewrite(sql, USER)
    assert _filtered_refs(r.sql, "task_claim") == [True]
    assert " left join " in r.sql.lower()          # 外连接语义必须保留


def test_subquery_and_union_branches_are_filtered():
    sub = rewrite("SELECT id FROM task_claim WHERE id IN (SELECT claim_id FROM settlement)", USER)
    assert _filtered_refs(sub.sql, "task_claim") == [True]
    assert _filtered_refs(sub.sql, "settlement") == [True]

    union = rewrite("SELECT id FROM task_claim UNION SELECT id FROM settlement", USER)
    assert _filtered_refs(union.sql, "task_claim") == [True]
    assert _filtered_refs(union.sql, "settlement") == [True]


def test_cte_body_is_filtered():
    r = rewrite("WITH mine AS (SELECT id FROM notification) SELECT * FROM mine", USER)
    assert _filtered_refs(r.sql, "notification") == [True]
    assert "mine" in r.sql.lower()


def test_cte_name_collision_is_denied():
    """CTE 与业务表同名会连带跳过内部真实表 → 必须直接拒绝"""
    sql = "WITH task_claim AS (SELECT id FROM task_claim) SELECT * FROM task_claim"
    with pytest.raises(PolicyDenied) as exc:
        rewrite(sql, USER)
    assert exc.value.reason == policy.DENY_CTE


def test_model_supplied_user_id_cannot_escape():
    """模型自己写 user_id = 999：注入的过滤仍然生效，结果只会是 0 行，不会越权"""
    r = rewrite("SELECT COUNT(*) FROM task_claim WHERE user_id = 999", USER)
    assert _filtered_refs(r.sql, "task_claim") == [True]
    assert "999" in r.sql          # 模型的条件被保留
    assert str(UID) in r.sql       # 系统注入的过滤也在


def test_me_placeholder_is_resolved():
    r = rewrite("SELECT COUNT(*) FROM settlement WHERE user_id = {{ME}}", USER)
    assert "{{ME}}" not in r.sql
    assert str(UID) in r.sql
    assert _filtered_refs(r.sql, "settlement") == [True]


def test_self_referencing_policy_tables():
    """task_status_log 的策略引用了 task / task_claim，替换后仍需全部受限"""
    r = rewrite("SELECT id FROM task_status_log", USER)
    assert _filtered_refs(r.sql, "task_status_log") == [True]
    assert "publisher_id" in r.sql and "user_id" in r.sql


# ---------------------------------------------------------------- 角色与护栏
def test_admin_only_table_denied_for_user():
    with pytest.raises(PolicyDenied) as exc:
        rewrite("SELECT COUNT(*) FROM audit_log", USER)
    assert exc.value.reason == policy.DENY_PLATFORM


def test_admin_can_read_admin_only_table_without_row_filter():
    r = rewrite("SELECT COUNT(*) FROM audit_log", ADMIN)
    assert _derived_tables(r.sql) == set()
    assert "audit_log" in r.sql.lower()


@pytest.mark.parametrize("sql", [
    "UPDATE task SET status = 'OPEN'",
    "DELETE FROM task_claim",
    "DROP TABLE task",
    "SELECT 1; DROP TABLE task",                                  # 多语句
    "SELECT COUNT(*) FROM information_schema.tables",             # 系统库
    "SELECT COUNT(*) FROM other_db.task",                         # 跨库
    "SELECT SLEEP(10)",
])
def test_static_guard_blocks_dangerous_sql(sql):
    with pytest.raises(PolicyDenied) as exc:
        rewrite(sql, USER)
    assert exc.value.reason == policy.DENY_GUARD


def test_limit_is_enforced_after_rewrite():
    r = rewrite("SELECT id FROM task_claim", USER)
    assert "limit" in r.sql.lower()


# ---------------------------------------------------------------- 缓存与唯一出口
def test_cache_key_is_scoped_by_role_not_by_user():
    """同角色共享缓存是安全的（缓存值是重写前 SQL），但技能不能跨角色复用"""
    a = policy.cache_scope_key("我接了几个任务", 5, Principal(user_id=7))
    b = policy.cache_scope_key("我接了几个任务", 5, Principal(user_id=8))
    c = policy.cache_scope_key("我接了几个任务", 5, ADMIN)
    assert a == b, "重写前 SQL 与用户无关，同角色应命中同一缓存"
    assert a != c, "跨角色必须隔离"


def test_execute_rejects_raw_sql():
    with pytest.raises(TypeError):
        policy.execute("SELECT COUNT(*) FROM task_claim")     # type: ignore[arg-type]


# ---------------------------------------------------------------- 提示词一致性
def test_prompts_and_policy_agree():
    from agent import prompts_user as P

    assert policy.ME_TOKEN in P.NL2SQL_USER_RULES
    # 重写层可能抛出的拒答原因，都必须有话术兜底
    for reason in (policy.DENY_GUARD, policy.DENY_PLATFORM, policy.DENY_TABLE, policy.DENY_CTE):
        assert reason in P.DENY_TEMPLATES, "缺少拒答话术: %s" % reason


def test_domain_rules_no_double_percent():
    """DATE_FORMAT 的 %% 会被模型照抄成错误 SQL（MySQL 里 %% 是字面 %）"""
    from agent.prompts_user import domain_rules

    assert "%%" not in domain_rules()


# ---------------------------------------------------------------- B1 发布者视角
def test_publisher_can_see_claims_on_own_task():
    """行级粒度是"我与该行有关系"，不是"该行的 user_id 是我"

    否则"我发布的任务被几个人接了"会被过滤成 1 —— **错答比拒答更糟**。
    """
    r = rewrite("SELECT COUNT(*) FROM task_claim", USER)
    assert _filtered_refs(r.sql, "task_claim") == [True]
    assert "publisher_id" in r.sql, "发布者维度必须进入行级条件"


@pytest.mark.parametrize("table", ["task_submission", "review", "settlement", "claim_status_log"])
def test_publisher_dimension_present_on_related_tables(table):
    r = rewrite("SELECT id FROM %s" % table, USER)
    assert _filtered_refs(r.sql, table) == [True], table
    assert "publisher_id" in r.sql, "%s 缺少发布者维度" % table


def test_notification_and_report_stay_self_only():
    """B3/B4：通知只给自己；举报只给提交人（被举报人看不到）"""
    for table, col in (("notification", "user_id"), ("report", "reporter_id")):
        r = rewrite("SELECT id FROM %s" % table, USER)
        assert _filtered_refs(r.sql, table) == [True]
        assert "%s.%s = %d" % (table, col, UID) in r.sql


# ---------------------------------------------------------------- B2 列级收敛
def test_user_table_projection_whitelist():
    ok = rewrite("SELECT u.nickname, u.credit_score FROM user u", USER)
    assert _filtered_refs(ok.sql, "user") == [True]

    for bad in ("username", "openid", "unionid", "password_hash", "role", "status"):
        with pytest.raises(PolicyDenied) as exc:
            rewrite("SELECT u.%s FROM user u" % bad, USER)
        assert exc.value.reason == policy.DENY_SENSITIVE, bad


def test_select_star_on_restricted_table_denied():
    with pytest.raises(PolicyDenied) as exc:
        rewrite("SELECT * FROM user", USER)
    assert exc.value.reason == policy.DENY_SENSITIVE


def test_unqualified_column_with_multiple_tables_is_denied():
    """未限定表名 + 多表作用域 → 无法静态判定归属，fail-closed"""
    with pytest.raises(PolicyDenied) as exc:
        rewrite("SELECT nickname FROM user u JOIN task t ON t.publisher_id = u.id", USER)
    assert exc.value.reason == policy.DENY_SENSITIVE


def test_aggregate_over_restricted_table_allowed():
    """COUNT(*) 不暴露具体列，应当放行"""
    r = rewrite("SELECT COUNT(*) FROM user", USER)
    assert _filtered_refs(r.sql, "user") == [True]


def test_operator_ignores_column_whitelist():
    r = rewrite("SELECT u.username FROM user u", OPERATOR)
    assert "username" in r.sql


# ---------------------------------------------------------------- A4 映射一致性
def test_repair_kind_mapping_is_valid():
    from agent.prompts_user import REPAIR_KINDS

    for reason, kind in policy.POLICY_TO_KIND.items():
        if kind is not None:
            assert kind in REPAIR_KINDS, "未定义的修复类型: %s -> %s" % (reason, kind)
    for reason in (policy.DENY_GUARD, policy.DENY_SENSITIVE, policy.DENY_PLATFORM,
                   policy.DENY_TABLE, policy.DENY_CTE, policy.DENY_INTERNAL):
        assert reason in policy.POLICY_TO_KIND, "新增拒答原因后忘了配修复类型: %s" % reason


# ---------------------------------------------------------------- 测试自身自检
def test_derived_table_helper_is_not_vacuous():
    """元测试：_derived_tables 一旦因 sqlglot 参数改名而恒返回空集合，
    所有"断言没有派生表"的用例都会**假通过**，所以这里必须能测出真值。"""
    r = rewrite("SELECT COUNT(*) FROM task_claim", USER)
    # 行级条件里注入的子查询（task）也会被识别，所以断言"包含"而非"等于"
    assert {"task_claim", "task"} <= _derived_tables(r.sql)

    plain = rewrite("SELECT COUNT(*) FROM task WHERE deleted = 0", USER)
    assert _derived_tables(plain.sql) == set()


def test_scope_helper_detects_from_table():
    """元测试：作用域解析必须能取到 FROM 表（sqlglot 30 的 key 是 from_）"""
    root = sqlglot.parse_one("SELECT * FROM user", read="mysql")
    assert policy._select_scope_tables(root) == {"user"}
