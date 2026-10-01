# -*- coding: utf-8 -*-
"""从 `sql/ai_tables.sql` 生成 treatbord（Java 侧）的 Flyway 迁移文件。

为什么需要它：`ai_*` 表的权威定义在本仓库的 `sql/ai_tables.sql`（含运维说明与授权模板），
而 treatbord 的 schema 由 **Flyway** 管理（`V8__add_login_lock_config_keys.sql` 之后是 V9）。
两边各写一份必然会漂移 —— 所以迁移文件由脚本**从权威定义生成**，不手抄。

treatbord 的 Flyway 用**独立迁移账号**（`MYSQL_MIGRATE_USER`，默认 `db_migrate`，含 DDL），
所以应用侧不需要人工执行任何 mysql 命令：把生成的文件放进迁移目录，下次启动即建表。

用法：
    python scripts/export_ai_migration.py                    # 默认写入 ../treatbord/.../db/migration
    python scripts/export_ai_migration.py --out /path/V9__add_ai_tables.sql
"""
from __future__ import annotations

import argparse
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "sql" / "ai_tables.sql"
DEFAULT_OUT = pathlib.Path("/home/KeYa/Project/treatbord/src/main/resources/db/migration/V9__add_ai_tables.sql")

HEADER = """-- ============================================================================
-- V9 · AI 边车所需表（会话 / 消息 / 审计 / 反馈 / 提示词版本）
--
-- ⚠️ 本文件由 TBagent 仓库的 scripts/export_ai_migration.py **自动生成**，请勿手改：
--     权威定义在 TBagent/sql/ai_tables.sql（含授权模板与运维说明），改那边后重新生成。
--
-- Flyway 用独立迁移账号（MYSQL_MIGRATE_USER / db_migrate，含 DDL），
-- 因此本迁移会在应用启动时自动建表，无需人工执行 mysql 命令。
-- ============================================================================

"""


def ddl_statements() -> list[str]:
    """从权威定义里取出 CREATE TABLE 语句（跳过注释、GRANT 模板等运维内容）"""
    text = SOURCE.read_text(encoding="utf-8")
    body = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip().upper().startswith("CREATE TABLE")]


def render() -> str:
    return HEADER + ";\n\n".join(ddl_statements()) + ";\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="生成 treatbord 的 Flyway 迁移（V9）")
    ap.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    ap.add_argument("--check", action="store_true", help="只校验目标文件是否与生成结果一致")
    args = ap.parse_args()

    content = render()
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        print("一致 ✓" if current == content else "不一致 ✗：需要重新生成")
        raise SystemExit(0 if current == content else 1)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(content, encoding="utf-8")
    print("已生成 %s（%d 条 CREATE TABLE）" % (args.out, len(ddl_statements())))


if __name__ == "__main__":
    main()
