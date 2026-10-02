# -*- coding: utf-8 -*-
"""成本报表：从 ai_query_audit 聚合（P2 的"成本按用户归集"）。

用真实 MySQL（treatbord_test）插合成行 → 聚合 → 核对 → 清理；CI 用 sqlite 快照时跳过。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 测试写库统一走 TEST_DB：CI 的库名不是 treatbord_test（空库 fixture）
# 使用方式：TEST_DB=treatbord_test（本地）· TEST_DB=tb_ci（CI）
TEST_DB = os.getenv("TEST_DB_NAME", "treatbord_test")

from config import IS_SQLITE  # noqa: E402
from scripts import cost_report  # noqa: E402

needs_mysql = pytest.mark.skipif(IS_SQLITE, reason="报表查的是 MySQL 审计表（CI 用 sqlite 快照）")

TRACE = "pytest-cost-report"


@needs_mysql
def test_aggregate_by_user_and_day(monkeypatch):
    monkeypatch.setenv("AUDIT_DB_NAME", TEST_DB)
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


@needs_mysql
def test_feedback_satisfaction_report(monkeypatch):
    """满意度聚合（--by feedback）：赞/踩/满意率/被改过的评价数。

    "被改过" 靠 V10 加的 updated_at：updated_at > created_at ⇒ 用户改过评价
    （原先只有 created_at，改主意完全看不出痕迹）。
    """
    monkeypatch.setenv("AUDIT_DB_NAME", TEST_DB)
    conn = cost_report.connect()
    with conn.cursor() as cur:
        cur.execute("SHOW COLUMNS FROM ai_feedback LIKE 'updated_at'")
        if cur.fetchone() is None:
            conn.close()
            pytest.skip("该库还没有 V10（ai_feedback.updated_at）；见 sql/ai_feedback_updated_at.sql")
    old_created = "2026-01-01 00:00:00"
    def totals():
        agg = cost_report.aggregate(by="feedback", days=3650)
        return {k: sum(r[k] for r in agg) for k in ("ratings", "up", "down", "edited")}

    # 共享测试库里有别人/别的用例留下的评价 → 一律用**增量**断言，不假设表是空的
    before = totals()
    m1, m2, m3 = 990001, 990002, 990003              # message_id 是 BIGINT，不能用字符串
    rows = [(m1, 7, 1, old_created),                 # 赞，没改过
            (m2, 7, -1, old_created),                # 踩；随后被改（updated_at 自动更新）
            (m3, 8, 1, old_created)]
    try:
        with conn.cursor() as cur:
            for mid, uid, rating, created in rows:
                cur.execute("INSERT INTO ai_feedback (message_id, user_id, rating, comment, created_at)"
                            " VALUES (%s,%s,%s,%s,%s)", (mid, uid, rating, "自检", created))
            # 制造一次"改过"：更新会触发 ON UPDATE CURRENT_TIMESTAMP
            cur.execute("UPDATE ai_feedback SET rating=1 WHERE message_id=%s", (m2,))
        conn.commit()

        agg = cost_report.aggregate(by="feedback", days=3650)
        assert agg, "应有按天聚合结果"
        after = totals()
        assert after["ratings"] - before["ratings"] == 3
        # m2 先记 👎、随后被改成 👍 → 终态是 3 赞 0 踩
        assert after["up"] - before["up"] == 3 and after["down"] - before["down"] == 0
        assert after["edited"] - before["edited"] >= 1, "改过的评价必须被统计出来"

        text = cost_report.render_feedback(agg, days=3650)
        assert "满意度报表" in text and "改过" in text
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ai_feedback WHERE message_id IN (%s,%s,%s)", (m1, m2, m3))
        conn.commit()
        conn.close()
