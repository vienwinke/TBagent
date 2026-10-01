# -*- coding: utf-8 -*-
"""导出公开演示用的 SQLite 快照（MySQL → SQLite），并【脱敏】敏感字段

为什么需要：Streamlit Cloud / HF Spaces 无法访问你本机的 MySQL。
把 14 张表导出成随仓库分发的 SQLite 快照，公开演示就能零依赖跑起来；
完整评估仍在真实 MySQL 上跑（评估集是 MySQL 方言）。

脱敏规则（公开仓库必须做）：
  openid      → 'openid_demo_<id>'（可关联性破坏，长度格式保留）
  unionid     → NULL 或 'unionid_demo_<id>'
  password_hash → 'demo_hash_removed'
  ip          → '10.0.0.<id % 254 + 1>'（保留网段结构，去掉真实来源）

用法：
  python scripts/export_snapshot.py                 # 生成 data/snapshot.sqlite + data/schema.json（SQLite 版）
  python scripts/export_snapshot.py --keep-sensitive # 保留敏感字段（仅本地调试用，勿提交）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql  # noqa: E402

from config import DB, SYSTEM_TABLES, setup_logging  # noqa: E402

SNAPSHOT = ROOT / "data" / "snapshot.sqlite"
SCHEMA_JSON = ROOT / "data" / "schema.json"


def read_mysql() -> dict[str, list[tuple]]:
    conn = pymysql.connect(host=DB.host, port=DB.port, user=DB.user, password=DB.password,
                           database=DB.name, charset="utf8mb4", connect_timeout=6)
    data: dict[str, list[tuple]] = {}
    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name", (DB.name,))
        for (table,) in cur.fetchall():
            if table in SYSTEM_TABLES:
                continue
            cur.execute("SELECT * FROM `%s`" % table)
            cols = [d[0] for d in cur.description]
            data[table] = (cols, cur.fetchall())
    conn.close()
    return data


def _coerce(v):
    """把 MySQL 取回的值转成 SQLite 可绑定的类型（Decimal/datetime 不支持直接绑定）"""
    import datetime as _dt
    from decimal import Decimal

    if v is None or isinstance(v, (int, float, str, bytes)):
        return v
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat(sep=" ") if isinstance(v, _dt.datetime) else v.isoformat()
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _sqlite_type(v) -> str:
    from decimal import Decimal

    if isinstance(v, bool) or isinstance(v, int):
        return "INTEGER"
    if isinstance(v, (float, Decimal)):
        return "REAL"
    return "TEXT"


def mask_rows(table: str, columns: list[str], rows: list[tuple]) -> list[tuple]:
    """按列名脱敏（公开仓库必须不含真实敏感数据）"""
    out = []
    for r in rows:
        row = list(r)
        for i, c in enumerate(columns):
            if c == "openid" and row[i]:
                row[i] = "openid_demo_%s" % row[0]
            elif c == "unionid":
                row[i] = None
            elif c == "password_hash" and row[i]:
                row[i] = "demo_hash_removed"
            elif c == "ip" and row[i]:
                row[i] = "10.0.0.%d" % (int(row[0]) % 254 + 1)
        out.append(tuple(row))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="导出 SQLite 演示快照")
    ap.add_argument("--keep-sensitive", action="store_true", help="保留敏感字段（仅本地调试）")
    ap.add_argument("--out", default=str(SNAPSHOT))
    args = ap.parse_args()
    setup_logging()

    data = read_mysql()
    snapshot = Path(args.out)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    if snapshot.exists():
        snapshot.unlink()

    sq = sqlite3.connect(snapshot)
    schema_tables = []
    for table, (columns, rows) in data.items():
        if not args.keep_sensitive:
            rows = mask_rows(table, columns, rows)
        col_defs = []
        for c, v in zip(columns, rows[0] if rows else [None] * len(columns)):
            col_defs.append('"%s" %s' % (c, _sqlite_type(v)))
        sq.execute('CREATE TABLE "%s" (%s)' % (table, ", ".join(col_defs)))
        if rows:
            rows = [tuple(_coerce(v) for v in r) for r in rows]
            sq.executemany('INSERT INTO "%s" VALUES (%s)' % (table, ",".join("?" * len(columns))), rows)
        schema_tables.append({"name": table, "columns": columns, "rows": len(rows)})
    sq.commit()

    # 同步生成 SQLite 版 schema.json（供 Schema 检索与表白名单使用）
    old = json.loads(SCHEMA_JSON.read_text(encoding="utf-8"))
    by_name = {t["name"]: t for t in old["tables"]}
    for st in schema_tables:
        src = by_name.get(st["name"])
        if src:
            src["rows"] = st["rows"]
    new_schema = dict(old)
    new_schema["backend"] = "sqlite"
    new_schema["snapshot"] = snapshot.name
    new_schema["generated_at"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
    SCHEMA_JSON.write_text(json.dumps(new_schema, ensure_ascii=False, indent=2), encoding="utf-8")
    sq.close()

    total = sum(s["rows"] for s in schema_tables)
    print("✅ 快照导出完成: %s（%d 表 / %d 行）%.0f KB" % (
        snapshot, len(schema_tables), total, snapshot.stat().st_size / 1024))
    print("   敏感字段脱敏: %s" % ("否（--keep-sensitive）" if args.keep_sensitive else "是（openid/unionid/password_hash/ip）"))
    print("   schema.json 已同步为 sqlite 版（表结构描述不变）")
    print("   公开演示请设置 DB_BACKEND=sqlite，并把 snapshot.sqlite 一起提交")


if __name__ == "__main__":
    main()