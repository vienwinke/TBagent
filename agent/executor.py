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
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import pymysql
from loguru import logger

from config import DB, GUARD, IS_SQLITE, SENSITIVE_COLUMNS

MASK = "[已脱敏]"


def jsonable(value: Any) -> Any:
    """把数据库原值转成 **JSON 原生类型**（SSE / Streamlit / 图表都能直接序列化）。

    为什么必须在**执行层**做：pymysql 原样返回 `Decimal` / `datetime` / `date` / `time` /
    `timedelta` / `bytes`，而 `json.dumps` 对它们一律抛 TypeError。此前崩溃发生在边车
    序列化 SSE 帧时 —— 流中途异常把连接掐了，Java 侧报"边车不可达: I/O error ... closed"，
    排查方向被完全带偏（实测：只要 SQL 投影出金额或时间列就 100% 崩）。

    为什么不给 `json.dumps` 加 `default=str`：那会把金额变成**字符串**，
    前端格式化与数值排序都会受影响，而且只是掩盖问题。这里做语义正确的转换。

    转换规则：
    · `Decimal`  → float（本库金额为 2 位小数，float 显示安全；精确值仍在库里与审计表里）
    · `datetime` → "YYYY-MM-DD HH:MM:SS[.ffffff]"（可读且可排序）
    · `date`/`time` → ISO 字符串
    · `timedelta`（MySQL TIME 列）→ "HH:MM:SS"
    · `bytes` → UTF-8 解码（非法字节替换，不抛）
    · int / float / str / bool / None 原样返回
    """
    import datetime as _dt
    from decimal import Decimal

    if value is None or isinstance(value, (int, float, str, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, _dt.datetime):          # 必须在 date 之前判：datetime 是 date 的子类
        return value.isoformat(sep=" ")
    if isinstance(value, (_dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        total = int(value.total_seconds())
        sign = "-" if total < 0 else ""
        total = abs(total)
        return "%s%02d:%02d:%02d" % (sign, total // 3600, (total % 3600) // 60, total % 60)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return str(value)                            # 兜底：保证一定可序列化


def jsonable_rows(rows: Iterable[Sequence[Any]]) -> list[tuple]:
    """按行归一化（返回 tuple，保持与 QueryResult.rows 的类型一致）"""
    return [tuple(jsonable(v) for v in row) for row in rows]

MYSQL_HINTS = {
    1792: "（会话为只读事务，写操作/DDL 已被 MySQL 拒绝）",
    # SQLite（公开演示快照）保留同样语义
    "sqlite_readonly": "（SQLite query_only 模式，写操作已被拒绝）",
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
    masked_columns: list[str] = field(default_factory=list)        # 命中的【源敏感列名】（如 openid）
    masked_output_columns: list[str] = field(default_factory=list)  # 实际被替换的输出列名（可能是别名）
    sensitive_columns: list[str] = field(default_factory=list)      # 同 masked_columns，供评估使用
    explain_rows: int | None = None
    elapsed_ms: int = 0

    def as_dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, r)) for r in self.rows]

    def summary(self) -> dict[str, Any]:
        return {
            "columns": self.columns, "row_count": self.row_count, "truncated": self.truncated,
            "masked_columns": self.masked_columns, "masked_output_columns": self.masked_output_columns,
            "sensitive_columns": self.sensitive_columns, "explain_rows": self.explain_rows,
            "elapsed_ms": self.elapsed_ms,
        }


class _CursorCtx:
    """统一 pymysql / sqlite3 游标的上下文管理器

    pymysql 的 cursor 支持 `with`，而 sqlite3.Cursor 不支持（实测 TypeError），
    因此统一包一层，保证两种后端都能 `with _CursorCtx(conn) as cur`。
    """

    def __init__(self, conn) -> None:
        self.cur = conn.cursor()

    def __enter__(self):
        return self.cur

    def __exit__(self, *exc_info) -> bool:
        try:
            self.cur.close()
        except Exception:  # noqa: BLE001
            pass
        return False


def connect():
    """连接只读数据源：MySQL（真实评估）或 SQLite（公开演示快照）"""
    if IS_SQLITE:
        import sqlite3

        conn = sqlite3.connect(DB.sqlite_path, timeout=max(1.0, GUARD.timeout_ms / 1000.0))
        conn.execute("PRAGMA query_only = ON")       # 等价于会话级只读（护栏②）
        conn.execute("PRAGMA busy_timeout = %d" % GUARD.timeout_ms)
        return conn
    return pymysql.connect(
        host=DB.host, port=DB.port, user=DB.user, password=DB.password,
        database=DB.name, charset="utf8mb4", autocommit=True, connect_timeout=6,
    )


def _apply_session_guard(cur) -> None:
    """护栏①：会话级只读 护栏②：执行超时（SQLite 的只读在 connect() 里用 query_only 设好）"""
    if IS_SQLITE:
        return
    cur.execute("SET SESSION TRANSACTION READ ONLY")
    cur.execute("SET SESSION max_execution_time = %s", (GUARD.timeout_ms,))


def _mysql_error(exc: pymysql.err.MySQLError) -> SqlError:
    code = exc.args[0] if exc.args else "?"
    hint = MYSQL_HINTS.get(code, "")
    msg = str(exc.args[-1])[:160] if exc.args else str(exc)[:160]
    return SqlError("SQL 被拒绝/执行失败 [MySQL %s]%s: %s" % (code, hint, msg))


def explain_rows(cur, sql: str) -> int | None:
    """护栏③：EXPLAIN 预估扫描行数（各步骤 rows 相乘的保守估计）

    返回 **None 表示本后端无法估算**，调用方必须显式跳过该层 —— 绝不能把 None 当 0：
    历史实现在 sqlite 下返回 0，于是"估算过了、很便宜"是假象，而演示与 CI 用的
    恰恰是 sqlite 快照，这层护栏在演示环境等于不存在（已修）。
    MySQL 下若 EXPLAIN 未给出任何 rows 估算，同样返回 None，不谎报廉价。
    """
    if IS_SQLITE:
        return None
    cur.execute("EXPLAIN " + sql)
    cols = [d[0] for d in cur.description]
    idx = cols.index("rows") if "rows" in cols else None
    if idx is None:
        return None
    total, seen = 1, 0
    for row in cur.fetchall():
        value = row[idx]
        if value:
            total *= int(value)
            seen += 1
    return None if seen == 0 else total


def sensitive_positions(sql: str, columns: Sequence[str]) -> tuple[list[int], list[str]]:
    """定位需要脱敏的结果列。

    为什么不能只看结果列名：模型会给敏感列起别名（`openid AS 微信openid`）或用表达式包住
    （`CASE WHEN password_hash IS NULL ...`），此时结果列名不再是敏感列名 → 按名字匹配会被绕过。
    因此以 **SQL AST 的投影位置**为准：第 i 个投影引用了敏感列 → 脱敏第 i 列；
    无法用 AST 判定（SELECT *、投影数与结果列数不一致、解析失败）时，退化为列名匹配。
    """
    import sqlglot
    from sqlglot import exp

    hit: set[int] = set()
    names: set[str] = set()
    try:
        root = sqlglot.parse_one(sql, read="mysql")
        select = root if isinstance(root, exp.Select) else root.find(exp.Select)
        if select is not None:
            projections = list(select.expressions)
            has_star = any(isinstance(pr, exp.Star) for pr in projections)
            if not has_star and len(projections) == len(columns):
                for i, proj in enumerate(projections):
                    found = {(c.name or "").lower() for c in proj.find_all(exp.Column)} & SENSITIVE_COLUMNS
                    if found:
                        hit.add(i)
                        names |= found
                return sorted(hit), sorted(names)
    except Exception:  # noqa: BLE001
        pass

    # 兜底：列名精确匹配；较长敏感名（>=6 字符，如 openid/password_hash/unionid）允许子串命中别名；
    # `ip` 这类短名必须精确匹配，否则 description 之类的列会被误伤
    for i, col in enumerate(columns):
        low = (col or "").lower()
        for s in SENSITIVE_COLUMNS:
            if low == s or (len(s) >= 6 and s in low):
                hit.add(i)
                names.add(s)
    return sorted(hit), sorted(names)


def mask_rows(columns: Sequence[str], rows: Iterable[Sequence[Any]],
              positions: Sequence[int] | None = None) -> tuple[list[tuple], list[str]]:
    """结果脱敏：把指定位置（缺省则按列名判定）的值替换为 [已脱敏]"""
    if positions is None:
        positions, _ = sensitive_positions("", columns)
    idx = list(positions)
    if not idx:
        return [tuple(r) for r in rows], []
    masked = []
    for r in rows:
        row = list(r)
        for i in idx:
            if i < len(row):
                row[i] = MASK
        masked.append(tuple(row))
    return masked, [columns[i] for i in idx if i < len(columns)]


def execute_readonly(
    rewritten: Any,
    *,
    max_rows: int | None = None,
    check_cost: bool = True,
    row_limit: int | None = None,
) -> QueryResult:
    """执行一条**已重写**的只读 SQL（只接受 policy.RewrittenSql）。

    为什么改成类型约束（契约 §6 决策 B）：在此之前"所有 SQL 必须过 policy.rewrite"
    只是**约定 + 源码守卫测试**（一个正则扫 nl2sql/run_eval 里有没有直连）——
    新增一条代码路径就能绕过。现在拿裸字符串一律 TypeError，
    唯一出口从"我们记得这么做"变成"不这么做就跑不起来"。
    内部诊断脚本需要直连时，也必须先过 policy.rewrite（见 scripts/smoke_sqlite.py）。
    """
    from agent.policy import RewrittenSql      # 延迟导入：policy 依赖本模块，模块级会成环

    if not isinstance(rewritten, RewrittenSql):
        raise TypeError(
            "execute_readonly 只接受 policy.RewrittenSql（收到 %s）。"
            "所有 SQL 必须经 policy.rewrite() 产出 —— 见 docs/treatbord嵌入-接口契约.md §5.3"
            % type(rewritten).__name__)
    sql = rewritten.sql
    limit = max_rows or GUARD.max_rows
    started = time.time()
    result = QueryResult(sql=sql)
    columns: list[str] = []
    raw: list[tuple] = []
    with closing(connect()) as conn:
        with _CursorCtx(conn) as cur:
            try:
                _apply_session_guard(cur)
                if check_cost:
                    est = explain_rows(cur, sql)
                    result.explain_rows = est
                    if est is None:
                        # 显式声明"这层没生效"，而不是让 None 冒充 0 行的廉价查询
                        logger.warning(
                            "[guard] 本后端无法估算扫描行数，护栏③（EXPLAIN 限额）本次跳过: {}", sql[:120])
                    else:
                        threshold = row_limit or GUARD.explain_row_limit
                        if est > threshold:
                            raise SqlCostError("EXPLAIN 预估扫描 %d 行，超过阈值 %d，已拒绝执行"
                                               % (est, threshold))
                cur.execute(sql)
                if cur.description is None:
                    raise SqlError("该语句没有返回结果集（疑似写操作或 DDL），已拒绝")
                columns = [d[0] for d in cur.description]
                raw = list(cur.fetchmany(limit + 1))
            except pymysql.err.MySQLError as exc:
                raise _mysql_error(exc) from exc
            except Exception as exc:  # noqa: BLE001  sqlite3.Error 等
                if type(exc).__module__.startswith("sqlite3"):
                    msg = str(exc)
                    hint = ""
                    if "readonly" in msg.lower() or "query_only" in msg.lower():
                        hint = MYSQL_HINTS["sqlite_readonly"]
                    elif "no such table" in msg:
                        hint = "（表不存在）"
                    raise SqlError("SQL 被拒绝/执行失败 [SQLite]%s: %s" % (hint, msg[:160])) from exc
                raise
    if len(raw) > limit:
        result.truncated = True
        raw = raw[:limit]
    positions, source_names = sensitive_positions(sql, columns)
    rows, output_cols = mask_rows(columns, raw, positions)
    # 对评估与排查而言，"命中了哪个敏感列"（openid）比"输出列叫什么"（可能是别名）更有意义
    result.masked_columns = source_names or output_cols
    result.masked_output_columns = output_cols
    result.sensitive_columns = source_names
    # ★ 归一化成 JSON 原生类型：此处统一处理，SSE / Streamlit / 图表 spec 全都受益
    result.columns, result.rows = columns, jsonable_rows(rows)
    result.row_count = len(rows)
    result.elapsed_ms = int((time.time() - started) * 1000)
    logger.debug("[exec] {} 行 / {}ms / 命中敏感列={} / 脱敏输出列={} / 预估扫描={}",
                 result.row_count, result.elapsed_ms, result.masked_columns,
                 result.masked_output_columns, result.explain_rows)
    return result


def health() -> dict[str, Any]:
    """连通性 + 账号身份自检（M0 验收用）"""
    if IS_SQLITE:
        import sqlite3

        with connect() as conn:
            ro = conn.execute("PRAGMA query_only").fetchone()[0]
            ver = sqlite3.sqlite_version
        return {"user": "sqlite_query_only", "version": ver, "read_only": int(ro),
                "max_execution_time_ms": GUARD.timeout_ms, "target": DB.label}
    with closing(connect()) as conn:
        with _CursorCtx(conn) as cur:
            _apply_session_guard(cur)
            cur.execute("SELECT CURRENT_USER(), VERSION(), "
                        "@@session.transaction_read_only, @@session.max_execution_time")
            user, ver, ro, timeout = cur.fetchone()
    return {"user": user, "version": ver, "read_only": int(ro), "max_execution_time_ms": int(timeout),
            "target": DB.label}
