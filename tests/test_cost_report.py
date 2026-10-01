# -*- coding: utf-8 -*-
"""成本报表：从 ai_query_audit 聚合（P2 的"成本按用户归集"）。

用真实 MySQL（treatbord_test）插合成行 → 聚合 → 核对 → 清理；CI 用 sqlite 快照时跳过。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import IS_SQLITE  # noqa: E402
from scripts import cost_report  # noqa: E402

needs_mysql = pytest.mark.skipif(IS_SQLITE, reason="报表查的是 MySQL 审计表（CI 用 sqlite 快照）")

TRACE = "pytest-cost-report"


@needs_mysql
def test_aggregate_by_user_and_day(monkeypatch):
    monkeypatch.setenv("AUDIT_DB_NAME", "treatbord_test")
    conn = cost_report.connect()
    rows = [
        (TRACE + "-1", 7, 100, 10, 0.0010, "ok", 0),
        (TRACE + "-2", 7, 200, 20, 0.0020, "ok", 1),
        (TRACE + "-3", 8, 300, 30, 0.0030, "denied", 0),
    ]
    try:
        # 共享测试库：不假设它是空的，用**增量**断言（实测踩到过自己先前的残留行）
        before = {r["k"]: r for r in cost_report.aggregate(by="user", days=1)}
        with conn.cursor() as cur:
            for t, uid, pt, ct, cost, verdict, cache in rows:
                cur.execute("INSERT INTO ai_query_audit (trace_id, user_id, question, verdict,"
                            " prompt_tokens, completion_tokens, cost_yuan, cache_hit)"
                            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                            (t, uid, "报表自检", verdict, pt, ct, cost, cache))
        conn.commit()

        after = {r["k"]: r for r in cost_report.aggregate(by="user", days=1)}

        def delta(uid, field):
            return after[uid][field] - before.get(uid, {}).get(field, 0)

        assert delta(7, "requests") == 2
        assert delta(7, "prompt_tokens") == 300 and delta(7, "completion_tokens") == 30
        assert abs(delta(7, "cost_yuan") - 0.003) < 1e-9
        assert delta(7, "cache_hits") == 1
        assert delta(8, "denied") == 1

        assert cost_report.aggregate(by="day", days=1), "按天聚合应有结果"
        text = cost_report.render(cost_report.aggregate(by="user", days=1), by="user", days=1)
        assert "成本报表" in text and "合计" in text
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ai_query_audit WHERE trace_id LIKE %s", (TRACE + "%",))
        conn.commit()
        conn.close()


def test_aggregate_rejects_unknown_dimension():
    with pytest.raises(ValueError):
        cost_report.aggregate(by="hour")
