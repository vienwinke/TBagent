# -*- coding: utf-8 -*-
"""选图/兜底的边界用例（演示暴露的问题）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import answer, chart  # noqa: E402


def test_wide_detail_table_not_charted():
    cols = ["id", "publisher_id", "title", "description", "reward", "quota", "claimed_count"]
    rows = [(i, 1, "t%d" % i, "d", 10, 1, 0) for i in range(8)]
    s = chart.choose_spec(cols, rows)
    assert s.kind == chart.KIND_TABLE


def test_masked_result_not_charted():
    s = chart.choose_spec(["id", "password_hash"], [(1, "[已脱敏]"), (2, "[已脱敏]")],
                          masked_columns=["password_hash"])
    assert s.kind == chart.KIND_TABLE


def test_normal_two_column_still_charted():
    s = chart.choose_spec(["昵称", "任务数"], [("a", 1), ("b", 2)])
    assert s.kind == chart.KIND_BAR


def test_summarize_fallback_when_model_empty():
    out = answer.summarize("待接取的任务有几个？", ["任务数"], [(1,)], llm_fn=lambda m: "")
    assert "1" in out and "任务数" in out


def test_summarize_fallback_empty_result():
    out = answer.summarize("被封禁用户有几个？", ["cnt"], [], llm_fn=lambda m: "")
    assert "没有查到" in out


def test_summarize_fallback_multi_row():
    out = answer.summarize("q", ["昵称", "任务数"], [("a", 1), ("b", 2)], llm_fn=lambda m: "   ")
    assert "2 行" in out