# -*- coding: utf-8 -*-
"""宽松 EX（允许生成 SQL 多/少返回列）单测"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.run_eval import relaxed_match  # noqa: E402


def test_exact_match():
    assert relaxed_match([(1,)], [(1,)], 1, 1)


def test_extra_column_allowed():
    # 参考 1 列 (总赏金)，生成 2 列 (总赏金, 任务数) → 应判对
    assert relaxed_match([(120.0,)], [(120.0, 8.0)], 1, 2)


def test_fewer_columns_allowed():
    # 反向：参考 2 列，生成 1 列（只给被问的昵称）→ 也应判对
    assert relaxed_match([("小明", 3.0)], [("小明",)], 2, 1)


def test_wrong_values_rejected():
    assert not relaxed_match([(120.0,)], [(999.0, 8.0)], 1, 2)


def test_multiple_rows_extra_column():
    assert relaxed_match([("a", 1.0), ("b", 2.0)], [(1.0, "a", 1.0), (2.0, "b", 2.0)], 2, 3)
