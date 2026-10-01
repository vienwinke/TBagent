# -*- coding: utf-8 -*-
"""审计落库（契约 §4）：一次问答一行，写 `ai_query_audit`。

四条设计约束，都是踩过坑才会写的：
1. **best-effort**：审计失败绝不能让用户的问题失败 —— 只记日志与计数，不往上抛。
   审计是"事后追溯"能力，不该成为在线链路的新故障点。
2. **独立连接**：问答链路走只读会话（`executor` 里 `SET SESSION TRANSACTION READ ONLY`），
   写审计必须另开一个普通连接。`AUDIT_DB_*` 可覆盖，默认复用 `DB_*`。
3. **默认关闭**：`AUDIT_ENABLED=false` 时完全不连库（本地没建表的环境不会刷错误日志）。
   与仓库既有惯例一致（Schema 精简、语义层兜底都是"默认关、显式开"）。
4. **不写敏感值**：只记 SQL 与元信息；问句原文按现有脱敏规则处理，本层不再加工。

⚠️ 生产建议：另建一个只给 `INSERT, SELECT` 的审计账号（见 `sql/ai_tables.sql` 末尾模板），
用 `AUDIT_DB_USER/AUDIT_DB_PASSWORD` 指向它，而不是复用应用账号。
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from loguru import logger

# 与 sql/ai_tables.sql 的列一一对应（顺序无关，但这里保持与 DDL 同序便于核对）
COLUMNS = ("trace_id", "user_id", "session_id", "question", "route", "scope",
           "detected_tables", "generated_sql", "rewritten_sql", "policy_version",
           "verdict", "deny_reason", "row_count", "truncated", "masked_columns",
           "latency_ms", "prompt_tokens", "completion_tokens", "cost_yuan",
           "cache_hit", "repaired", "model")

# verdict 取值（表里是 VARCHAR(16)，这里给一组固定语义，便于按值聚合）
VERDICT_OK = "ok"            # 正常作答
VERDICT_DENIED = "denied"    # 语义/策略层拒答
VERDICT_FAILED = "failed"    # 生成或执行失败


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default) or default


def enabled() -> bool:
    """是否开启审计落库（默认关闭）"""
    return _env("AUDIT_ENABLED", "false").lower() in ("1", "true", "yes")


@dataclass
class AuditRow:
    """一行审计 —— 字段与契约 §4 的审计行一致"""

    trace_id: str
    user_id: int
    question: str
    session_id: int | None = None       # 注意：L2 的 session_id 是字符串，这里要的是会话表主键（P3 落库后再填）
    route: str | None = None
    scope: str | None = None
    detected_tables: list[str] = field(default_factory=list)
    generated_sql: str | None = None
    rewritten_sql: str | None = None
    policy_version: str | None = None
    verdict: str = VERDICT_OK
    deny_reason: str | None = None
    row_count: int | None = None
    truncated: bool = False
    masked_columns: list[str] = field(default_factory=list)
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_yuan: float | None = None
    cache_hit: bool = False
    repaired: bool = False
    model: str | None = None
    created_at: datetime | None = None

    def as_params(self) -> tuple:
        """转成 INSERT 的参数元组（JSON 列手动序列化，布尔转 0/1）"""
        data = asdict(self)
        row = []
        for col in COLUMNS:
            value = data.get(col)
            if col in ("detected_tables", "masked_columns"):
                value = json.dumps(value or [], ensure_ascii=False)
            elif col in ("truncated", "cache_hit", "repaired"):
                value = int(bool(value))
            row.append(value)
        return tuple(row)


_STATS = {"written": 0, "failed": 0}
_LOCK = threading.Lock()


def stats() -> dict[str, int]:
    with _LOCK:
        return dict(_STATS)


def _connect():
    import pymysql

    from config import DB

    user = _env("AUDIT_DB_USER") or DB.user
    password = _env("AUDIT_DB_PASSWORD") or DB.password
    database = _env("AUDIT_DB_NAME") or DB.name
    host = _env("AUDIT_DB_HOST") or DB.host
    port = int(_env("AUDIT_DB_PORT") or DB.port)
    return pymysql.connect(host=host, port=port, user=user, password=password,
                           database=database, connect_timeout=5, autocommit=True)


def record(row: AuditRow | dict[str, Any]) -> bool:
    """写入一行审计。**永不抛异常**；返回是否写成功。"""
    if not enabled():
        return False
    try:
        row = row if isinstance(row, AuditRow) else AuditRow(**row)
        sql = "INSERT INTO ai_query_audit (%s) VALUES (%s)" % (
            ", ".join(COLUMNS), ", ".join(["%s"] * len(COLUMNS)))
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(sql, row.as_params())
        with _LOCK:
            _STATS["written"] += 1
        return True
    except Exception as exc:  # noqa: BLE001  审计失败不能影响在线链路
        with _LOCK:
            _STATS["failed"] += 1
        logger.warning("[audit] 审计写入失败（不影响本次问答）：{}: {}", type(exc).__name__,
                       str(exc)[:160])
        return False
