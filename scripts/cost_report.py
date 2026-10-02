# -*- coding: utf-8 -*-
"""成本与用量报表：从 `ai_query_audit` 聚合（契约 §4 / 设计文档 P2 的最后一项）。

为什么放在脚本而不是边车里：这是**离线分析**，不该出现在请求路径上。
审计表已按行记录 trace/verdict/tokens/cost，所以"按用户、按天归集成本"只是一条 GROUP BY。

用法：
    python scripts/cost_report.py                 # 最近 7 天，按用户
    python scripts/cost_report.py --days 30 --top 10
    python scripts/cost_report.py --by day        # 按天
    python scripts/cost_report.py --json          # 给别的系统消费

连接：复用 `AUDIT_DB_*`（未设则用 `DB_*`）—— 只读查询，任何有 SELECT 的账号都够。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA = {
    "user": ("user_id", "SELECT user_id AS k, COUNT(*) AS requests,"
                        " SUM(prompt_tokens) AS prompt_tokens,"
                        " SUM(completion_tokens) AS completion_tokens,"
                        " SUM(cost_yuan) AS cost_yuan,"
                        " SUM(verdict='denied') AS denied,"
                        " SUM(cache_hit) AS cache_hits"
                        " FROM ai_query_audit WHERE created_at >= NOW() - INTERVAL %s DAY"
                        " GROUP BY user_id ORDER BY cost_yuan DESC"),
    # 满意度：赞/踩/被改过的次数。V10 给 ai_feedback 加了 updated_at ——
    # updated_at > created_at 说明用户改过评价（原先完全看不出痕迹）。
    "feedback": ("DATE(created_at)", "SELECT DATE(created_at) AS k, COUNT(*) AS ratings,"
                                    " SUM(rating = 1) AS up,"
                                    " SUM(rating = -1) AS down,"
                                    " SUM(updated_at > created_at) AS edited"
                                    " FROM ai_feedback WHERE created_at >= NOW() - INTERVAL %s DAY"
                                    " GROUP BY DATE(created_at) ORDER BY k DESC"),
    "day": ("DATE(created_at)", "SELECT DATE(created_at) AS k, COUNT(*) AS requests,"
                               " SUM(prompt_tokens) AS prompt_tokens,"
                               " SUM(completion_tokens) AS completion_tokens,"
                               " SUM(cost_yuan) AS cost_yuan,"
                               " SUM(verdict='denied') AS denied,"
                               " SUM(cache_hit) AS cache_hits"
                               " FROM ai_query_audit WHERE created_at >= NOW() - INTERVAL %s DAY"
                               " GROUP BY DATE(created_at) ORDER BY k DESC"),
}


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default) or default


def connect():
    import pymysql

    from config import DB

    return pymysql.connect(host=_env("AUDIT_DB_HOST") or DB.host,
                           port=int(_env("AUDIT_DB_PORT") or DB.port),
                           user=_env("AUDIT_DB_USER") or DB.user,
                           password=_env("AUDIT_DB_PASSWORD") or DB.password,
                           database=_env("AUDIT_DB_NAME") or DB.name,
                           connect_timeout=5, cursorclass=pymysql.cursors.DictCursor)


def aggregate(*, by: str = "user", days: int = 7, top: int | None = None) -> list[dict]:
    """按 user / day 聚合最近 N 天的用量与成本"""
    if by not in SCHEMA:
        raise ValueError("by 只能是 user 或 day")
    _, sql = SCHEMA[by]
    if top:
        sql += " LIMIT %d" % int(top)
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")     # 报表只读，顺手把约束加上
            cur.execute(sql, (int(days),))       # 天数是**值**，INTERVAL 单位是语法：不能整段当字符串传
            rows = cur.fetchall()
    finally:
        conn.close()
    for row in rows:
        if by == "feedback":
            for key in ("ratings", "up", "down", "edited"):
                row[key] = row[key] or 0
            row["up_rate"] = round(row["up"] / row["ratings"], 3) if row["ratings"] else 0.0
            continue
        for key in ("prompt_tokens", "completion_tokens", "cost_yuan", "denied", "cache_hits"):
            row[key] = row[key] or 0
        row["cost_yuan"] = float(row["cost_yuan"])
        row["cache_hit_rate"] = round(row["cache_hits"] / row["requests"], 3) if row["requests"] else 0.0
    return rows


def render_feedback(rows: list[dict], *, days: int) -> str:
    """满意度报表（按天）：赞 / 踩 / 满意率 / 被改过的评价数"""
    lines = ["# AI 满意度报表（最近 %d 天，按天）" % days, "",
             "| 日期 | 评价数 | 👍 | 👎 | 满意率 | 改过评价 |", "|---|---|---|---|---|---|"]
    total = {"ratings": 0, "up": 0, "down": 0, "edited": 0}
    for r in rows:
        lines.append("| %s | %d | %d | %d | %.0f%% | %d |"
                     % (r["k"], r["ratings"], r["up"], r["down"], r["up_rate"] * 100, r["edited"]))
        for k in total:
            total[k] += r[k]
    rate = (total["up"] / total["ratings"] * 100) if total["ratings"] else 0.0
    lines += ["", "合计：%d 条评价 · 👍%d / 👎%d · 满意率 %.0f%% · 其中 %d 条被改过"
              % (total["ratings"], total["up"], total["down"], rate, total["edited"])]
    return "\n".join(lines)


def render(rows: list[dict], *, by: str, days: int) -> str:
    head = "| %s | 请求 | 输入 tokens | 输出 tokens | 成本(¥) | 拒答 | 缓存命中率 |" % (
        "用户" if by == "user" else "日期")
    lines = ["# AI 成本报表（最近 %d 天，按%s）" % (days, "用户" if by == "user" else "天"), "",
             head, "|---|---|---|---|---|---|---|"]
    total_cost = sum(r["cost_yuan"] for r in rows)
    total_req = sum(r["requests"] for r in rows)
    for r in rows:
        lines.append("| %s | %d | %s | %s | %.4f | %d | %.0f%% |" % (
            r["k"], r["requests"], r["prompt_tokens"], r["completion_tokens"],
            r["cost_yuan"], r["denied"], r["cache_hit_rate"] * 100))
    lines += ["", "合计：%d 次请求 · ¥%.4f · 单次均值 ¥%.4f" % (
        total_req, total_cost, (total_cost / total_req) if total_req else 0.0)]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="从 ai_query_audit / ai_feedback 聚合成本、用量与满意度")
    ap.add_argument("--by", choices=("user", "day", "feedback"), default="user",
                    help="user 按用户 · day 按天 · feedback 满意度（赞踩）")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--top", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = aggregate(by=args.by, days=args.days, top=args.top)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    elif args.by == "feedback":
        print(render_feedback(rows, days=args.days))
    else:
        print(render(rows, by=args.by, days=args.days))


if __name__ == "__main__":
    main()
