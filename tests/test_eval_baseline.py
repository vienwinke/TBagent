# -*- coding: utf-8 -*-
"""评估基准的口径：EX 必须用**本次运行**的参考结果判定，而不是 cases.yaml 里的存档 hash。

背景（实测，2026-10-01）：时间类问题的口径相对"现在"（最近 7 天 / 本月 / 距截止不到 3 天），
数据不动而时钟在走 —— 15 条时间题里 **9 条的存档 hash 已无法被参考 SQL 自己复现**。
后果有两层：
  1) 这些题被钉死上限：模型再准也判 EX_MISS（实测 time 类存档判定 6/15，实时基准 12/15）；
  2) "同配置跑分 33~46/51 波动"里有一部分是基准漂移，被误读成模型抖动。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import executor, policy  # noqa: E402
from config import IS_SQLITE  # noqa: E402
from eval.run_eval import EVAL_PRINCIPAL, _live_reference, rows_hash  # noqa: E402

CASES = yaml.safe_load(Path("eval/cases.yaml").read_text(encoding="utf-8"))["cases"]
TIME_CASE = next(c for c in CASES if c["id"] == "time-02")

# 参考 SQL 是 MySQL 方言（DATE_SUB/CURDATE/INTERVAL 7 DAY），sqlite 快照后端跑不了 ——
# 评估本身也只在 mysql 后端跑；这两条用例按后端跳过，否则 CI（sqlite）会误红。
needs_mysql = pytest.mark.skipif(IS_SQLITE, reason="参考 SQL 为 MySQL 方言，需 DB_BACKEND=mysql")


def _hash_of(sql: str) -> str:
    return rows_hash(policy.execute(policy.rewrite(sql, EVAL_PRINCIPAL), check_cost=False).rows)


@needs_mysql
def test_live_reference_matches_current_execution():
    rows, digest = _live_reference(TIME_CASE)
    assert rows, "时间题在当前库上应有结果"
    assert digest == _hash_of(TIME_CASE["reference_sql"]), "实时基准必须等于当场执行参考 SQL 的结果"


@needs_mysql
def test_live_reference_ignores_stale_stored_hash():
    """存档 hash 过期时，判定不能再用它 —— 否则等于给该题钉死上限"""
    stale = dict(TIME_CASE, expected={"hash": "deadbeef" * 8})
    _, digest = _live_reference(stale)
    assert digest and digest != "deadbeef" * 8, "必须用实时执行的哈希，而不是存档值"


@needs_mysql
def test_reference_matches_itself_so_a_perfect_model_scores_hit():
    """不变量：模型若产出与参考等价的 SQL，实时判定必须给 EX_HIT"""
    _, ref_digest = _live_reference(TIME_CASE)
    assert _hash_of(TIME_CASE["reference_sql"]) == ref_digest


def test_reference_failure_returns_none_so_caller_can_fall_back():
    """参考 SQL 跑不通时必须返回 None，让上层回退到存档 hash，而不是把题判成必挂"""
    broken = dict(TIME_CASE, reference_sql="SELECT * FROM no_such_table_xyz")
    rows, digest = _live_reference(broken)
    assert rows is None and digest is None


def test_missing_reference_sql_is_tolerated():
    rows, digest = _live_reference({"id": "x", "reference_sql": ""})
    assert rows is None and digest is None


def test_stale_baselines_are_counted_in_metrics():
    """跑分结果里必须暴露漂移条数：指标被静默压低过一次，不能再有第二次"""
    import inspect

    from eval import run_eval

    src = inspect.getsource(run_eval.score)
    assert "stale_baselines" in src
    assert "_live_reference" in src, "EX 判定必须走实时基准"
