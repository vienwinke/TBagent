# -*- coding: utf-8 -*-
"""三层护栏 · 第 0 层：行级权限重写（SQL 通往数据库的唯一出口）

背景：嵌入 treatbord 后普通用户也能提问，必须保证"问不到别人的数据"。
      提示词里写"只看自己的数据"只是**建议**，模型可能被绕过；本模块是**保证**。

分工：
  agent/prompts_user.py   告诉模型"不要写身份条件，系统会自动注入"
  agent/policy.py         真正注入，并且是 SQL 通往数据库的唯一通道

核心手法：把有身份约束的表引用，替换成"已按身份过滤的派生表"：

    SELECT * FROM task_claim tc
  → SELECT * FROM (SELECT * FROM task_claim WHERE …) AS tc

为什么不用"在 WHERE 里 AND 一个条件"：
  LEFT JOIN 的空表一侧被 WHERE 过滤，会把外连接悄悄退化成内连接，
  结果集语义被改写（用户看到的数字会错）。派生表替换没有这个陷阱。

★ 唯一出口（choke point）约定：
  正常生成 / 缓存命中复用 / 回环修复重试 / 运营工具 / 批量评估，
  任何 SQL 都必须先过 rewrite()，再由 execute() 执行。
  业务代码不得直接调用 executor.execute_readonly。

MySQL 没有行级安全（RLS），所以行级只能在应用层做 —— 这也是本模块必须存在的原因。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import lru_cache

import sqlglot
from sqlglot import exp

from config import SCHEMA_PATH, SQL_DIALECT, SYSTEM_TABLES
from agent import sql_guard

# 策略语义变更时必须一起提升：缓存键含版本，旧缓存据此整体失效
POLICY_VERSION = "pol-2026.11-02"
DIALECT = SQL_DIALECT if SQL_DIALECT in ("mysql", "sqlite") else "mysql"

# 角色档位：数值越大权限越高（判定全部走 rank 比较，加档位不用改逻辑）
ROLE_USER = "USER"
ROLE_OPERATOR = "OPERATOR"
ROLE_ADMIN = "ADMIN"
ROLE_RANK = {ROLE_USER: 0, ROLE_OPERATOR: 1, ROLE_ADMIN: 2}
ROLES = tuple(ROLE_RANK)

# 占位符：提示词让模型写出的记号，重写层在解析前替换为调用者 user_id。
# user_id 来自内部 JWT 的 sub（int），因此是**整数**字面量，不存在注入面。
ME_TOKEN = "{{ME}}"

# 表 → 最低可见角色（**未登记 = USER 可见**）
#
# ⚠️ 这里的默认方向与下面 USER_POLICY 的 fail-closed 默认**相反**，是刻意的：
#     表级可见性未登记 → 放行：业务表默认对用户开放，且行级过滤仍然兜底；
#     行级策略未登记 → 拒绝：没有过滤条件就等于整表泄露，没有第二道防线。
#   不要把两者"统一"成同一种默认。
TABLE_MIN_ROLE: dict[str, str] = {
    "login_log":  ROLE_OPERATOR,   # 登录风控：运营日常
    "audit_log":  ROLE_ADMIN,      # 操作审计
    "app_config": ROLE_ADMIN,      # 系统配置/阈值
}

# 兼容旧引用；新代码请用 TABLE_MIN_ROLE
ADMIN_ONLY_TABLES = frozenset(t for t, r in TABLE_MIN_ROLE.items() if r == ROLE_ADMIN)

# ★ 行级策略：表 → 强制过滤条件（None = 公开表，不加过滤）
#   条件里的表名必须写**全名**：它会被嵌进 (SELECT * FROM <表> WHERE <条件>) 里，
#   此时外层别名还不存在，只有全名可用。
#
#   粒度是"我与该行有关系"，不是"该行的 user_id 是我"：
#   发布者必须能看到自己任务下的接取/提交/评价/结算，否则
#   "我发布的任务被几个人接了"会被过滤成 1 —— 这是**错答**，比拒答更糟。
#   条件里引用的子表不会再被二次重写，所以它们各自自带过滤条件。
USER_POLICY: dict[str, str | None] = {
    # 本人；以及与我发生过任务关系的用户（我接取任务的发布者 / 接取我任务的用户）
    "user": "user.id = {{ME}} "
            "OR user.id IN (SELECT tc.user_id FROM task_claim tc "
            "               WHERE tc.task_id IN (SELECT t.id FROM task t WHERE t.publisher_id = {{ME}})) "
            "OR user.id IN (SELECT t.publisher_id FROM task t "
            "               WHERE t.id IN (SELECT tc.task_id FROM task_claim tc WHERE tc.user_id = {{ME}}))",
    # 我接取的 ∪ 我发布任务下的接取
    "task_claim": "task_claim.user_id = {{ME}} "
                  "OR task_claim.task_id IN (SELECT t.id FROM task t WHERE t.publisher_id = {{ME}})",
    # 我提交的 ∪ 我发布任务下的提交
    "task_submission": "task_submission.claim_id IN "
                       "(SELECT tc.id FROM task_claim tc WHERE tc.user_id = {{ME}} "
                       " OR tc.task_id IN (SELECT t.id FROM task t WHERE t.publisher_id = {{ME}}))",
    "file": "file.uploader_id = {{ME}}",
    "notification": "notification.user_id = {{ME}}",
    # 我发出的 ∪ 我收到的 ∪ 我发布任务下的评价
    "review": "review.from_user_id = {{ME}} OR review.to_user_id = {{ME}} "
              "OR review.claim_id IN (SELECT tc.id FROM task_claim tc "
              "                       WHERE tc.task_id IN "
              "                       (SELECT t.id FROM task t WHERE t.publisher_id = {{ME}}))",
    "report": "report.reporter_id = {{ME}}",
    # 我的结算 ∪ 我发布任务的结算（钱是我付的）
    "settlement": "settlement.user_id = {{ME}} "
                  "OR settlement.task_id IN (SELECT t.id FROM task t WHERE t.publisher_id = {{ME}})",
    "task_status_log": "task_status_log.task_id IN "
                       "(SELECT id FROM task WHERE publisher_id = {{ME}} "
                       " UNION SELECT task_id FROM task_claim WHERE user_id = {{ME}})",
    # 我接取的 ∪ 我发布任务下的接取，两者的流转记录
    "claim_status_log": "claim_status_log.claim_id IN "
                        "(SELECT tc.id FROM task_claim tc WHERE tc.user_id = {{ME}} "
                        " OR tc.task_id IN (SELECT t.id FROM task t WHERE t.publisher_id = {{ME}}))",
    "task": None,          # 公开市场：任务行情对所有用户可见
}

# USER 角色下，这些表的**投影列**白名单（他人信息的最小可见集）
#
# 只有行级放开是不够的：发布者能看接取者的行，但不该看接取者的账号字段。
# ⚠️ 不用 schema.json 的 sensitive 标记当唯一依据：它把 username 标成
#    sensitive=false，而 username 是登录账号（等价于半个凭据），不能给他人看。
USER_COLUMN_ALLOW: dict[str, frozenset[str]] = {
    "user": frozenset({"id", "nickname", "avatar", "credit_score"}),
}

# 哨兵：区分"公开表（策略值为 None）"与"根本没登记策略的新表"。
# 若不做这个区分，treatbord 将来新增一张表时，get() 同样返回 None →
# 该表会被**无过滤地**开放给普通用户（fail-open）。这里一律拒绝（fail-closed）。
_UNREGISTERED = object()

# 拒答原因（对应 prompts_user.DENY_TEMPLATES 的话术，前端直接取用）
DENY_GUARD = "DENY_GUARD"            # 静态护栏拦截（写操作/跨库/多语句/危险函数）
DENY_SENSITIVE = "DENY_SENSITIVE"    # 越权列 / 敏感字段
DENY_PLATFORM = "DENY_PLATFORM"      # 角色档位不足
DENY_TABLE = "DENY_TABLE"            # 表不存在/不在白名单
DENY_CTE = "DENY_CTE"                # CTE 名称与业务表同名
DENY_INTERNAL = "DENY_INTERNAL"      # 重写层自身异常（不应出现）

# PolicyDenied.reason → 回环修复的 kind（None = 不回环，直接拒答）
# 用字面量而不是 import prompts_user.REPAIR_KINDS：prompts_user 已经 import 本模块，
# 反向 import 会成环。一致性由 tests/test_policy.py::test_repair_kind_mapping_is_valid 兜住。
POLICY_TO_KIND: dict[str, str | None] = {
    DENY_GUARD: "guard_rejected",      # 能用提示词救回来 → 回环 1 次
    DENY_CTE: "cte_name_conflict",     # 改个 CTE 名就行 → 回环 1 次
    DENY_TABLE: "guard_rejected",      # 白名单问题 → 回环 1 次
    DENY_SENSITIVE: "column_denied",   # 去掉越权列 → 回环 1 次
    DENY_PLATFORM: None,               # 角色档位不足，重试无意义 → 直接拒答
    DENY_INTERNAL: None,               # 重写层自身异常 → 直接拒答 + 告警
}


class PolicyDenied(RuntimeError):
    """行级/角色策略拒绝 —— 走拒答话术，不是 500"""

    def __init__(self, message: str, *, reason: str = DENY_TABLE) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Principal:
    """调用者身份 —— **只能**由 treatbord 签发的内部 JWT 构造"""

    user_id: int
    role: str = ROLE_USER

    def __post_init__(self) -> None:
        if isinstance(self.user_id, bool) or not isinstance(self.user_id, int):
            raise ValueError("user_id 必须是整数（来自 JWT sub，禁止字符串）")
        if self.user_id <= 0:
            raise ValueError("user_id 必须为正整数")
        if self.role not in ROLES:
            raise ValueError("未知角色: %s（可选 %s）" % (self.role, "/".join(ROLES)))

    @property
    def rank(self) -> int:
        return ROLE_RANK[self.role]

    @property
    def is_privileged(self) -> bool:
        """运营及以上：不按行过滤、可见生成的 SQL"""
        return self.rank >= ROLE_RANK[ROLE_OPERATOR]

    @property
    def is_admin(self) -> bool:
        """管理员：额外可见 audit_log / app_config"""
        return self.rank >= ROLE_RANK[ROLE_ADMIN]


@dataclass(frozen=True)
class RewrittenSql:
    """重写产物 —— execute() 只接受这个类型，裸字符串一律拒绝"""

    sql: str
    tables: list[str]
    principal: Principal
    policy_version: str = POLICY_VERSION
    rewritten_tables: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    guard: dict = field(default_factory=dict)      # sql_guard.validate().summary()


@lru_cache(maxsize=1)
def _all_business_tables() -> frozenset[str]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return frozenset(t["name"].lower() for t in schema["tables"] if t["name"] not in SYSTEM_TABLES)


def visible_tables(principal: Principal) -> frozenset[str]:
    """角色可见表集合（提示词注入与白名单共用同一份口径）

    未在 TABLE_MIN_ROLE 登记的表默认 USER 可见（见该常量的注释）。
    """
    rank = principal.rank
    return frozenset(
        t for t in _all_business_tables()
        if ROLE_RANK[TABLE_MIN_ROLE.get(t, ROLE_USER)] <= rank
    )


def resolve_me(sql: str, user_id: int) -> str:
    """把模型写出的 {{ME}} 占位符替换为整数 user_id（int 来自 JWT，字面量安全）"""
    return (sql or "").replace(ME_TOKEN, str(int(user_id)))


# --------------------------------------------------------------- 列级收敛（B2）
def _select_scope_tables(select: exp.Select) -> set[str]:
    """该 Select 直接 FROM / JOIN 的表名（不含子查询里的表）"""
    names: set[str] = set()
    # ⚠️ sqlglot 30 把该参数改名为 "from_"（避开 Python 关键字），旧版本叫 "from"。
    #    用错 key 会静默拿到空作用域 → 列级收敛整体失效，所以两个都试。
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is not None and isinstance(from_.this, exp.Table):
        names.add((from_.this.name or "").lower())
    for join in select.args.get("joins") or []:
        if isinstance(join.this, exp.Table):
            names.add((join.this.name or "").lower())
    return names


def _check_projection_columns(root: exp.Expression) -> None:
    """USER 角色：对 USER_COLUMN_ALLOW 登记的表做投影列收敛（fail-closed）

    只在**投影**（SELECT 出来的列）上判定：WHERE / ORDER BY 引用这些列是必需的，
    限制它们会连正常过滤都做不了；真正决定"用户能看到什么"的是投影。
    """
    if not USER_COLUMN_ALLOW:
        return

    alias_map: dict[str, str] = {}
    for table in root.find_all(exp.Table):
        name = (table.name or "").lower()
        if name:
            alias_map[(table.alias or name).lower()] = name

    for select in root.find_all(exp.Select):
        scope = _select_scope_tables(select)
        restricted = scope & set(USER_COLUMN_ALLOW)
        if not restricted:
            continue
        allowed: frozenset[str] = frozenset().union(*(USER_COLUMN_ALLOW[t] for t in restricted))

        for proj in select.expressions:
            if isinstance(proj, exp.Star):
                raise PolicyDenied(
                    "禁止 SELECT *（无法核对受限表的列），请显式列出允许的列",
                    reason=DENY_SENSITIVE,
                )
            for col in proj.find_all(exp.Column):
                qualifier = (col.table or "").lower()
                if qualifier:
                    owner = alias_map.get(qualifier)
                    if owner not in restricted:
                        continue
                elif len(scope) == 1:
                    owner = next(iter(restricted))
                else:
                    # 未限定表名且作用域里有多张表 → 无法静态判定归属，fail-closed
                    raise PolicyDenied(
                        "列 %s 未限定表名，无法核对权限，请写成 表名.列名 或去掉该列" % col.name,
                        reason=DENY_SENSITIVE,
                    )
                if (col.name or "").lower() not in allowed:
                    raise PolicyDenied(
                        "%s 表的列 %s 对普通用户不可见（允许：%s）"
                        % (owner, col.name, "、".join(sorted(allowed))),
                        reason=DENY_SENSITIVE,
                    )


# --------------------------------------------------------------- 行级重写（L4）
def _derived_subquery(table: str, condition: str, alias: str, uid: int) -> exp.Subquery:
    """(SELECT * FROM 表 WHERE 条件) AS 别名"""
    cond_sql = resolve_me(condition, uid)
    try:
        cond = sqlglot.parse_one(cond_sql, read=DIALECT)
        inner = exp.select("*").from_(table).where(cond)
        return inner.subquery(alias)
    except Exception as exc:  # noqa: BLE001
        raise PolicyDenied("重写失败：%s（%s）" % (table, exc), reason=DENY_INTERNAL) from exc


def rewrite(sql: str, principal: Principal) -> RewrittenSql:
    """把裸 SQL 变成"已按身份限定"的 SQL。任何失败都抛 PolicyDenied（可安全展示给用户）"""
    # 0) 先消解占位符，后续环节（解析 / 校验 / 重写）只见整数
    sql = resolve_me(sql, principal.user_id)

    # 1) 静态护栏（第 1 层）：写操作 / 跨库 / 多语句 / 危险函数 / 强制 LIMIT
    try:
        guard = sql_guard.validate(sql)
    except sql_guard.SqlGuardError as exc:
        raise PolicyDenied("静态校验未通过: %s" % exc, reason=DENY_GUARD) from exc

    # 2) 角色白名单：表级（L3）
    allowed = visible_tables(principal)
    forbidden = [t for t in guard.tables if t not in allowed]
    if forbidden:
        needs_higher = [t for t in forbidden if t in TABLE_MIN_ROLE]
        raise PolicyDenied(
            "表 %s 对角色 %s 不可见" % (", ".join(sorted(forbidden)), principal.role),
            reason=DENY_PLATFORM if needs_higher else DENY_TABLE,
        )

    warnings = list(guard.warnings)

    # 3) 运营及以上：不做行级/列级限制
    if principal.is_privileged:
        return RewrittenSql(sql=guard.sql, tables=guard.tables, principal=principal,
                            warnings=warnings, guard=guard.summary())

    root = sqlglot.parse_one(guard.sql, read=DIALECT)
    if root is None:
        raise PolicyDenied("SQL 解析结果为空", reason=DENY_INTERNAL)

    # 4) 列级收敛（L5）
    _check_projection_columns(root)

    cte_names = {c.alias_or_name.lower() for c in root.find_all(exp.CTE) if c.alias_or_name}

    # ★ 关闭 CTE 同名绕过：
    #   `WITH task_claim AS (SELECT * FROM task_claim) SELECT * FROM task_claim`
    #   会让"跳过 CTE 引用"的规则连带跳过内部真实表，从而逃过行过滤。
    #   合法查询不需要这种命名，直接拒绝（模型改名重试即可）。
    collision = cte_names & _all_business_tables()
    if collision:
        raise PolicyDenied(
            "CTE 名称不能与业务表同名（%s）" % ", ".join(sorted(collision)), reason=DENY_CTE
        )

    # 5) 行级重写（L4）。注意：先把节点列表**快照**下来，避免遍历到刚插入的派生表节点。
    rewritten_tables: list[str] = []
    for table in list(root.find_all(exp.Table)):
        name = (table.name or "").lower()
        if not name or name in cte_names:
            continue                                    # CTE 引用：不是真实表
        condition = USER_POLICY.get(name, _UNREGISTERED)
        if condition is _UNREGISTERED:
            # fail-closed：没登记策略的表不开给普通用户，宁可拒绝也不漏
            raise PolicyDenied(
                "表 %s 未登记行级策略，已按 fail-closed 拒绝" % name, reason=DENY_TABLE
            )
        if condition is None:
            continue                                    # 公开表（task）：不动
        alias = table.alias_or_name or name
        table.replace(_derived_subquery(name, condition, alias, principal.user_id))
        rewritten_tables.append(name)

    if rewritten_tables:
        warnings.append("已按身份限定表: %s" % ", ".join(sorted(set(rewritten_tables))))

    result = RewrittenSql(
        sql=root.sql(dialect=DIALECT),
        tables=guard.tables,
        principal=principal,
        rewritten_tables=sorted(set(rewritten_tables)),
        warnings=warnings,
        guard=guard.summary(),
    )
    # ★ fail-closed 自检：确认每一处受限表引用都被行级过滤包住
    missed = unfiltered_refs(result)
    if missed:
        raise PolicyDenied(
            "行级重写自检未通过：%s 仍有未被过滤的引用" % ", ".join(missed),
            reason=DENY_INTERNAL,
        )
    return result


def unfiltered_refs(rewritten: RewrittenSql) -> list[str]:
    """自检：重写后的 SQL 里是否还有**未被行级过滤包住**的受限表引用

    返回违规表名（空 = 通过）。这是 fail-closed 的第二道保险：
    派生表替换一旦因 sqlglot 行为变化而漏掉某个引用（UNION 分支、深层子查询、
    新语法），这里当场发现，而不是等用户查到了别人的数据。

    判定方式与测试一致：从每个受限表的 Table 节点向上找，看是否存在一个
    "带 WHERE 的派生表"祖先 —— 只要有一处引用不在其中，就算违规。
    """
    if rewritten.principal.is_privileged:
        return []                                   # 运营及以上不做行级限制

    restricted = {t for t, cond in USER_POLICY.items() if cond is not None}
    try:
        root = sqlglot.parse_one(rewritten.sql, read=DIALECT)
    except Exception:  # noqa: BLE001  解析不了就当作最坏情况
        return sorted(restricted)
    if root is None:
        return sorted(restricted)

    violations: list[str] = []
    for table in root.find_all(exp.Table):
        name = (table.name or "").lower()
        if name not in restricted:
            continue
        cursor, covered = table, False
        while cursor.parent is not None:
            cursor = cursor.parent
            if isinstance(cursor, exp.Subquery) and cursor.this.args.get("where") is not None:
                covered = True
                break
        if not covered:
            violations.append(name)
    return sorted(set(violations))


def cache_scope(principal: Principal) -> str:
    """缓存的**权限隔离维度**：角色 + 策略版本（不含 user_id）

    为什么不含 user_id：缓存里存的是**重写前**的 SQL（纯 SQL 文本、不含任何用户数据），
    每次请求都重新 rewrite() 注入身份。因此同角色的用户共享同一份缓存是安全且正确的，
    还能保住命中率。
    ★ 不变式：缓存值只能是重写前 SQL；任何情况下都不得缓存重写后的 SQL。
    """
    return "%s|%s" % (principal.role, POLICY_VERSION)


def cache_scope_key(question: str, top_k: int | None, principal: Principal) -> str:
    """问题级缓存键（问题 + top_k + 权限维度），供需要自行建键的调用方使用"""
    raw = "%s|%s|%s" % ((question or "").strip().lower(), top_k or 0, cache_scope(principal))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def execute(rewritten: RewrittenSql, **kwargs):
    """唯一允许的执行入口（延迟导入 executor，避免与 DB/metrics 初始化耦合）"""
    if not isinstance(rewritten, RewrittenSql):
        raise TypeError("必须传入 policy.rewrite() 的返回值：SQL 不得绕过重写层直接执行")
    from agent import executor as ex

    return ex.execute_readonly(rewritten.sql, **kwargs)
