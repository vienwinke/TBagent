# -*- coding: utf-8 -*-
"""只读 SQL 执行器 —— 三层护栏的第 2 层（沙箱）

四重保护（即使 SQL 生成被绕过也守住）：
1. 会话级只读：`SET SESSION TRANSACTION READ ONLY` → 任何写操作被 MySQL 直接拒绝（错误码 1792）
2. 执行超时：`SET SESSION max_execution_time` → 慢查询自动中断（毫秒，错误码 3024）
3. 行数上限：只取 max_rows+1 行，超出即截断（避免把大结果集拉进内存）
4. 结果脱敏：SENSITIVE_COLUMNS 列的值替换为 [已脱敏]

另附 EXPLAIN 成本预估（护栏第 3 层）。注意：在只读会话里 `EXPLAIN UPDATE/DELETE` 本身也会被拒绝，
因此它天然成为写操作的第一道拦截点。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import pymysql
from loguru import logger

from config import DB, GUARD, SENSITIVE_COLUMNS

MASK = "[已脱敏]"

MYSQL_HINTS = {
    1792: "（会话为只读事务，写操作/DDL 已被 MySQL 拒绝）",
    3024: "（执行超过 %dms 上限，已被中断）" % GUARD.timeout_ms,
    1146: "（表不存在）",
    1054: "（字段不存在）",
    1064: "（SQL 语法错误）",
}


class SqlError(RuntimeError):
    """SQL 执行被拒绝或执行失败"""


class SqlCostError(SqlError):
    """EXPLAIN 预估代价超阈值（拒绝执行）"""


@dataclass
class QueryResult:
    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    masked_columns: list[str] = field(default_factory=list)
    explain_rows: int | None = None
    elapsed_ms: int = 0

    def as_dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, r)) for r in self.rows]

    def summary(self) -> dict[str, Any]:
        return {
            "columns": self.columns, "row_count": self.row_count, "truncated": self.truncated,
            "masked_columns": self.masked_columns, "explain_rows": self.explain_rows,
            "elapsed_ms": self.elapsed_ms,
        }


def connect() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=DB.host, port=DB.port, user=DB.user, password=DB.password,
        database=DB.name, charset="utf8mb4", autocommit=True, connect_timeout=6,
    )


def _apply_session_guard(cur: pymysql.cursors.Cursor) -> None:
    """护栏①：会话级只读 护栏②：执行超时"""
    cur.execute("SET SESSION TRANSACTION READ ONLY")
    cur.execute("SET SESSION max_execution_time = %s", (GUARD.timeout_ms,))


def _mysql_error(exc: pymysql.err.MySQLError) -> SqlError:
    code = exc.args[0] if exc.args else "?"
    hint = MYSQL_HINTS.get(code, "")
    msg = str(exc.args[-1])[:160] if exc.args else str(exc)[:160]
    return SqlError("SQL 被拒绝/执行失败 [MySQL %s]%s: %s" % (code, hint, msg))


def explain_rows(cur: pymysql.cursors.Cursor, sql: str) -> int:
    """护栏③：EXPLAIN 预估扫描行数（各步骤 rows 相乘的保守估计）"""
    cur.execute("EXPLAIN " + sql)
    cols = [d[0] for d in cur.description]
    idx = cols.index("rows") if "rows" in cols else None
    if idx is None:
        return 0
    total, seen = 1, 0
    for row in cur.fetchall():
        value = row[idx]
        if value:
            total *= int(value)
            seen += 1
    return 0 if seen == 0 else total


def mask_rows(columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> tuple[list[tuple], list[str]]:
    """结果脱敏：敏感列一律替换为 [已脱敏]"""
    hit = [i for i, c in enumerate(columns) if c.lower() in SENSITIVE_COLUMNS]
    if not hit:
        return [tuple(r) for r in rows], []
    masked = []
    for r in rows:
        row = list(r)
        for i in hit:
            row[i] = MASK
        masked.append(tuple(row))
    return masked, [columns[i] for i in hit]


def execute_readonly(
    sql: str,
    *,
    max_rows: int | None = None,
    check_cost: bool = True,
    row_limit: int | None = None,
) -> QueryResult:
    """执行一条只读 SQL，返回结果 + 元信息；任何写操作/超限都会被拒绝"""
    limit = max_rows or GUARD.max_rows
    started = time.time()
    result = QueryResult(sql=sql)
    columns: list[str] = []
    raw: list[tuple] = []
    with connect() as conn:
        with conn.cursor() as cur:
            try:
                _apply_session_guard(cur)
                if check_cost:
                    est = explain_rows(cur, sql)
                    result.explain_rows = est
                    threshold = row_limit or GUARD.explain_row_limit
                    if est > threshold:
                        raise SqlCostError("EXPLAIN 预估扫描 %d 行，超过阈值 %d，已拒绝执行" % (est, threshold))
                cur.execute(sql)
                if cur.description is None:
                    raise SqlError("该语句没有返回结果集（疑似写操作或 DDL），已拒绝")
                columns = [d[0] for d in cur.description]
                raw = list(cur.fetchmany(limit + 1))
            except pymysql.err.MySQLError as exc:
                raise _mysql_error(exc) from exc
    if len(raw) > limit:
        result.truncated = True
        raw = raw[:limit]
    rows, masked_cols = mask_rows(columns, raw)
    result.columns, result.rows = columns, rows
    result.row_count, result.masked_columns = len(rows), masked_cols
    result.elapsed_ms = int((time.time() - started) * 1000)
    logger.debug("[exec] {} 行 / {}ms / 脱敏列={} / 预估扫描={}",
                 result.row_count, result.elapsed_ms, masked_cols, result.explain_rows)
    return result


def health() -> dict[str, Any]:
    """连通性 + 账号身份自检（M0 验收用）"""
    with connect() as conn:
        with conn.cursor() as cur:
            _apply_session_guard(cur)
            cur.execute("SELECT CURRENT_USER(), VERSION(), "
                        "@@session.transaction_read_only, @@session.max_execution_time")
            user, ver, ro, timeout = cur.fetchone()
    return {"user": user, "version": ver, "read_only": int(ro), "max_execution_time_ms": int(timeout),
            "target": DB.label}
