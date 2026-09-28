# -*- coding: utf-8 -*-
"""三层护栏 · 第 1 层：SQL 静态校验（sqlglot AST）

在把 SQL 送到数据库之前，先在 AST 层拦截：
  1. 只允许 SELECT / WITH（含 UNION）；INSERT/UPDATE/DELETE/DDL/事务 一律拒绝
  2. 禁止多语句（`SELECT 1; DROP TABLE user` → 解析出 2 条 → 拒绝）
  3. 表白名单：只能查 schema.json 里的业务表；禁止跨库/系统库（information_schema / mysql / performance_schema）
  4. 强制 LIMIT：没有则补，超过 max_rows 则收紧到 max_rows
  5. 危险函数与子句黑名单：SLEEP / BENCHMARK / LOAD_FILE / INTO OUTFILE / GET_LOCK 等

设计原则：**白名单优于黑名单**，且在 AST 上判断（不做字符串匹配，避免注释/大小写/空格绕过）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from config import GUARD, SCHEMA_PATH, SQL_DIALECT, SYSTEM_TABLES

DIALECT = SQL_DIALECT if SQL_DIALECT in ("mysql", "sqlite") else "mysql"

# 危险函数（在 AST 上按函数名匹配，大小写不敏感）
FORBIDDEN_FUNCS = {
    "sleep", "benchmark", "load_file", "get_lock", "release_lock", "sys_eval", "sys_exec",
    "master_pos_wait", "uuid_short",
}
# 允许的顶层表达式类型
ALLOWED_ROOTS = ("Select", "Union", "UnionAll", "Intersect", "Except", "Subquery", "With")
# 系统库前缀（一旦出现即拒绝）
FORBIDDEN_SCHEMAS = {"information_schema", "mysql", "performance_schema", "sys"}


class SqlGuardError(RuntimeError):
    """SQL 未通过静态校验"""


@dataclass
class GuardResult:
    sql: str                       # 规范化并（必要时）补好 LIMIT 的 SQL
    tables: list[str] = field(default_factory=list)
    limit: int | None = None
    limit_added: bool = False
    limit_capped: bool = False
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {"sql": self.sql, "tables": self.tables, "limit": self.limit,
                "limit_added": self.limit_added, "limit_capped": self.limit_capped,
                "warnings": self.warnings}


def allowed_tables() -> set[str]:
    """白名单 = schema.json 里的业务表（排除系统表）"""
    if SCHEMA_PATH.exists():
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        return {t["name"].lower() for t in schema["tables"] if t["name"] not in SYSTEM_TABLES}
    return set()


def _root_allowed(root: exp.Expression) -> bool:
    return type(root).__name__ in ALLOWED_ROOTS


def validate(sql: str, *, max_rows: int | None = None, enforce_limit: bool = True) -> GuardResult:
    """校验并规范化 SQL；不通过则抛 SqlGuardError"""
    limit_cap = max_rows or GUARD.max_rows
    raw = (sql or "").strip().rstrip(";").strip()
    if not raw:
        raise SqlGuardError("空 SQL")

    # ---- 1) 解析 + 多语句检查 ----
    try:
        statements = [s for s in sqlglot.parse(raw, read=DIALECT) if s is not None]
    except ParseError as exc:
        raise SqlGuardError("SQL 解析失败: %s" % str(exc)[:160]) from exc
    if not statements:
        raise SqlGuardError("SQL 解析结果为空")
    if len(statements) > 1:
        kinds = ", ".join(type(s).__name__ for s in statements)
        raise SqlGuardError("禁止多语句（解析出 %d 条: %s）" % (len(statements), kinds))
    root = statements[0]

    # ---- 2) 只读语句类型 ----
    if not _root_allowed(root):
        raise SqlGuardError("只允许 SELECT / WITH 查询，检测到 %s" % type(root).__name__)
    for node_type in ("Insert", "Update", "Delete", "Drop", "Create", "Alter", "Truncate",
                      "Command", "Transaction", "Commit", "Rollback", "Grant", "Merge", "Copy"):
        cls = getattr(exp, node_type, None)
        if cls is not None and root.find(cls):
            raise SqlGuardError("查询中出现禁止的语句/子句: %s" % node_type)

    # ---- 3) 表与库白名单 ----
    whitelist = allowed_tables()
    # CTE 别名（WITH x AS (...)）不是真实表，需从白名单校验中排除
    cte_names = {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE) if cte.alias_or_name}
    tables: list[str] = []
    for table in root.find_all(exp.Table):
        name = (table.name or "").lower()
        if name in cte_names:
            continue
        schema_name = (table.db or "").lower()
        catalog = (table.catalog or "").lower()
        if schema_name in FORBIDDEN_SCHEMAS or catalog in FORBIDDEN_SCHEMAS:
            raise SqlGuardError("禁止访问系统库: %s" % (schema_name or catalog))
        if schema_name and schema_name != "":
            # 只允许当前库（treatbord），显式跨库一律拒绝
            raise SqlGuardError("禁止跨库查询: %s.%s" % (schema_name, name))
        if whitelist and name not in whitelist:
            raise SqlGuardError("表 %s 不在白名单内（允许: %s）" % (name, ", ".join(sorted(whitelist))))
        if name and name not in tables:
            tables.append(name)

    # ---- 4) 危险函数 ----
    for func in root.find_all(exp.Func):
        candidates = set()
        # sqlglot 对已知函数给出 sql_name()；未知/自定义函数会落到 exp.Anonymous，
        # 其真实名字在 this 里（如 SLEEP(10) → Anonymous(this="SLEEP")），必须单独取。
        if isinstance(func, exp.Anonymous):
            candidates.add(str(func.this).lower())
        else:
            candidates.add((func.sql_name() or "").lower())
            candidates.add(type(func).__name__.lower())
        if candidates & FORBIDDEN_FUNCS:
            raise SqlGuardError("禁止使用危险函数: %s" % ", ".join(sorted(candidates & FORBIDDEN_FUNCS)))

    # ---- 5) INTO OUTFILE / DUMPFILE ----
    into_cls = getattr(exp, "Into", None)
    if into_cls is not None and root.find(into_cls):
        raise SqlGuardError("禁止 SELECT ... INTO（写文件）")

    # ---- 6) 强制 LIMIT ----
    warnings: list[str] = []
    limit_added = limit_capped = False
    limit_node = root.args.get("limit")
    current = None
    if limit_node is not None:
        try:
            current = int(limit_node.expression.name)
        except Exception:  # noqa: BLE001
            current = None
    if enforce_limit:
        if current is None:
            root = root.limit(limit_cap)
            limit_added = True
            warnings.append("原 SQL 无 LIMIT，已自动追加 LIMIT %d" % limit_cap)
            current = limit_cap
        elif current > limit_cap:
            root = root.limit(limit_cap)
            limit_capped = True
            warnings.append("原 LIMIT %d 超过上限，已收紧为 %d" % (current, limit_cap))
            current = limit_cap

    return GuardResult(sql=root.sql(dialect=DIALECT), tables=tables, limit=current,
                       limit_added=limit_added, limit_capped=limit_capped, warnings=warnings)


def is_safe(sql: str, **kwargs: Any) -> bool:
    """便捷判断（不抛异常）"""
    try:
        validate(sql, **kwargs)
        return True
    except SqlGuardError:
        return False
