# -*- coding: utf-8 -*-
"""可写连接的统一取法（审计 / 会话共用）。

为什么单独一个模块：这些写入（审计、会话消息、反馈）**不能用问答链路的连接** ——
`executor` 会在会话里 `SET SESSION TRANSACTION READ ONLY`，那条连接写不了任何东西。
所以写入一律另开连接，并且按优先级取配置：

    <SUFFIX>_DB_HOST/PORT/NAME/USER/PASSWORD  →  回退 DB_*

生产建议（见 sql/ai_tables.sql 末尾）：审计用一个只给 INSERT/SELECT 的账号，
会话用另一个账号；两者都与只读的问答账号分开，这样"只读"这条硬约束不会被写入需求侵蚀。
"""
from __future__ import annotations

import os
from typing import Any


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default) or default


def connect(suffix: str = "") -> Any:
    """建立可写连接；suffix 形如 "AUDIT" / "SESSION"（大写，对应 <SUFFIX>_DB_* 环境变量）"""
    import pymysql

    from config import DB

    prefix = ("%s_" % suffix) if suffix else ""
    return pymysql.connect(
        host=_env(prefix + "DB_HOST") or DB.host,
        port=int(_env(prefix + "DB_PORT") or DB.port),
        user=_env(prefix + "DB_USER") or DB.user,
        password=_env(prefix + "DB_PASSWORD") or DB.password,
        database=_env(prefix + "DB_NAME") or DB.name,
        connect_timeout=5,
        autocommit=True,
    )
