# -*- coding: utf-8 -*-
"""选图规则 + 意图路由 + 结果转述 的单测（全部可离线运行，不依赖 API Key）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import answer, chart, router  # noqa: E402


def test_metric_for_single_value():
    s = chart.choose_spec(["任务数"], [(8,)])
    assert s.kind == chart.KIND_METRIC


def test_line_for_time_series():
    rows = [("2026-09-20", 2), ("2026-09-21", 5), ("2026-09-22", 1)]
    s = chart.choose_spec(["日期", "任务数"], rows)
    assert s.kind == chart.KIND_LINE and s.x == "日期" and s.y == "任务数"


def test_line_for_hour_numbers():
    """HOUR() 返回的是数字，但列名暗示时间 → 应判为折线"""
    s = chart.choose_spec(["小时", "登录次数"], [(13, 15), (14, 8), (15, 4)])
    assert s.kind == chart.KIND_LINE


def test_bar_for_category_compare():
    rows = [("小美", 3), ("微信用户", 1), ("张同学", 2)]
    s = chart.choose_spec(["发布者昵称", "任务数"], rows)
    assert s.kind == chart.KIND_BAR


def test_barh_when_many_categories():
    rows = [("任务标题很长很长很长的名字%d" % i, i) for i in range(20)]
    s = chart.choose_spec(["标题", "接取数"], rows)
    assert s.kind == chart.KIND_BARH


def test_pie_when_percentages():
    rows = [("正面", 60.0), ("负面", 25.0), ("中性", 15.0)]
    s = chart.choose_spec(["情感", "占比"], rows)
    assert s.kind == chart.KIND_PIE


def test_table_when_single_column_multi_row():
    s = chart.choose_spec(["标题"], [("a",), ("b",)])
    assert s.kind == chart.KIND_TABLE


def test_router_sends_greeting_to_chat():
    for q in ["你好", "你是谁？", "谢谢", "hello"]:
        assert router.route(q) == router.CHAT, q


def test_router_sends_business_questions_to_data():
    for q in ["待接取的任务有几个？", "最近 7 天每天新增的接取数量", "统计各状态任务占比", "平均赏金是多少"]:
        assert router.route(q) == router.DATA, q


def test_router_default_for_short_unknown_is_chat():
    assert router.route("嗯") == router.CHAT


def test_summarize_uses_injected_llm():
    captured = {}

    def fake(messages):
        captured["prompt"] = messages[-1]["content"]
        return "共 8 个任务。"

    out = answer.summarize("任务有几个？", ["任务数"], [(8,)], llm_fn=fake)
    assert out == "共 8 个任务。"
    assert "任务有几个" in captured["prompt"] and "任务数" in captured["prompt"]


def test_summarize_handles_empty_rows():
    out = answer.summarize("被封禁的用户有几个？", ["cnt"], [], llm_fn=lambda m: "没有查到符合条件的数据")
    assert "没有查到" in out


def test_summarize_survives_llm_error():
    def boom(messages):
        raise RuntimeError("429")

    out = answer.summarize("q", ["c"], [(1,)], llm_fn=boom)
    assert "转述失败" in out