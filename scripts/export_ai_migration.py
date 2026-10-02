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


# 迁移清单：Java 侧文件名 → (本仓库权威源, 抽取方式)
#   create = 从权威 DDL 里抽 CREATE TABLE（表定义的唯一真源）
#   raw    = 整个文件就是迁移体（如 ALTER 语句）
MIGRATIONS = {
    "V9__add_ai_tables.sql": ("ai_tables.sql", "create"),
    "V10__ai_feedback_updated_at.sql": ("ai_feedback_updated_at.sql", "raw"),
    "V11__ai_feedback_backfill_updated_at.sql": ("ai_feedback_backfill_updated_at.sql", "raw"),
}


def ddl_statements(name: str) -> list[str]:
    """按迁移类型抽取语句（跳过注释、GRANT 模板等运维内容）"""
    source, mode = MIGRATIONS[name]
    text = (ROOT / "sql" / source).read_text(encoding="utf-8")
    body = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("--"))
    stmts = [s.strip() for s in body.split(";") if s.strip()]
    if mode == "create":
        return [s for s in stmts if s.upper().startswith("CREATE TABLE")]
    return stmts


def render(name: str) -> str:
    header = HEADER.replace("V9 · AI 边车所需表（会话 / 消息 / 审计 / 反馈 / 提示词版本）",
                            {"V9__add_ai_tables.sql": "V9 · AI 边车所需表（会话 / 消息 / 审计 / 反馈 / 提示词版本）",
                             "V10__ai_feedback_updated_at.sql": "V10 · ai_feedback 加 updated_at（评价被改过可见）",
                             "V11__ai_feedback_backfill_updated_at.sql": "V11 · 修正 V10 的历史行假阳性（updated_at 对齐 created_at）"}[name])
    body = ";\n\n".join(ddl_statements(name)) + ";\n"
    return header + body


def main() -> None:
    ap = argparse.ArgumentParser(description="生成 treatbord 的 Flyway 迁移（V9）")
    ap.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT.parent)
    ap.add_argument("--check", action="store_true", help="只校验目标文件是否与生成结果一致")
    args = ap.parse_args()

    out_dir = args.out if args.out.is_dir() else args.out.parent
    bad = 0
    for name in MIGRATIONS:
        content = render(name)
        target = out_dir / name
        if args.check:
            current = target.read_text(encoding="utf-8") if target.exists() else ""
            ok = current == content
            print("  %s %s" % ("一致 ✓" if ok else "不一致 ✗", name))
            bad += 0 if ok else 1
            continue
        target.write_text(content, encoding="utf-8")
        print("已生成 %s（%d 条语句）" % (target, len(ddl_statements(name))))
    if args.check:
        raise SystemExit(0 if bad == 0 else 1)


if __name__ == "__main__":
    main()
