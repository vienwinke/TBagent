# -*- coding: utf-8 -*-
"""结果值必须是 **JSON 原生类型**（执行层归一化）。

背景（真实崩溃）：pymysql 原样返回 `Decimal` / `datetime`，而边车用 `json.dumps` 序列化 SSE 帧 →
流中途抛 `TypeError: Object of type Decimal is not JSON serializable` → 连接被掐 →
Java 侧报「边车不可达: I/O error ... closed」（排查方向被完全带偏）。

触发面不是一个小角落：金额列（`task.reward` 等 4 个）+ 业务表时间列（33 个），
只要 SQL 投影到它们就 100% 崩。这里把"必须可序列化"钉成测试。
"""
from __future__ import annotations

import datetime
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import pipeline  # noqa: E402
from agent.executor import jsonable, jsonable_rows  # noqa: E402
from agent.pipeline import Deps  # noqa: E402
from agent.policy import Principal  # noqa: E402

USER = Principal(user_id=2)


@pytest.mark.parametrize("value,expected", [
    (Decimal("12.50"), 12.5),
    (Decimal("0"), 0.0),
    (datetime.datetime(2026, 10, 2, 15, 0, 24), "2026-10-02 15:00:24"),
    (datetime.datetime(2026, 10, 2, 15, 0, 24, 123456), "2026-10-02 15:00:24.123456"),
    (datetime.date(2026, 10, 2), "2026-10-02"),
    (datetime.time(15, 0, 24), "15:00:24"),
    (datetime.timedelta(hours=2, minutes=3, seconds=4), "02:03:04"),
    (datetime.timedelta(seconds=-5), "-00:00:05"),
    (b"abc", "abc"),
    (bytearray(b"xy"), "xy"),
    (None, None), (7, 7), (1.5, 1.5), ("s", "s"), (True, True),
])
def test_jsonable_converts_non_json_types(value, expected):
    assert jsonable(value) == expected
    json.dumps(jsonable(value))               # 必须可序列化，不抛


def test_jsonable_is_defensive_for_unknown_types():
    """未知类型兜底成字符串：宁可显示难看，也不能让整条流崩掉"""

    class Weird:
        def __str__(self) -> str:
            return "weird"

    assert jsonable(Weird()) == "weird"
    assert json.dumps(jsonable(Weird())) == '"weird"'


def test_jsonable_rows_keeps_tuple_shape():
    rows = jsonable_rows([(Decimal("1.5"), datetime.date(2026, 1, 1)), (Decimal("2.5"), None)])
    assert rows == [(1.5, "2026-01-01"), (2.5, None)]
    assert isinstance(rows[0], tuple), "保持与 QueryResult.rows 一致的 tuple 形状"


@pytest.mark.parametrize("sql,label", [
    ("SELECT id, reward FROM task LIMIT 3", "金额列（Decimal）"),
    ("SELECT id, create_time FROM notification LIMIT 3", "时间列（datetime）"),
])
def test_table_event_is_json_serializable(sql, label):
    """★ 用户撞到的那条路径：pipeline 的 table 事件必须能直接序列化成 SSE 帧。

    这里用真实执行（mysql 后端）跑一遍：任何非 JSON 原生值漏出来，json.dumps 就会抛。
    """
    # 问句要能走 data 分支（含业务名词 + 聚合线索），否则桩根本不会被调用
    events = list(pipeline.answer_stream("任务赏金一共有多少", USER, use_cache=False,
                                         deps=Deps(nl2sql_llm=lambda m: {
                                             "sql": sql, "reason": "测试", "tables": []},
                                             summary_llm=lambda m: "ok")))
    tables = [d for e, d in events if e == "table"]
    if not tables:
        pytest.skip("%s：本环境没有可执行结果（可能无数据）" % label)
    payload = tables[0]
    frame = json.dumps(payload, ensure_ascii=False)     # 这一步以前会抛 TypeError
    assert "rows" in payload and isinstance(frame, str)
