# -*- coding: utf-8 -*-
"""评估指标口径：三项"曾经只在 README 里手写"的指标必须能被自动算出。

历史问题：SQL 可执行率 / Schema 召回率 / 首次修复成功率 在 README 指标表里，
但 run_eval 的 metric 键里根本没有这三项 —— 指标表有两行干脆是残缺的 3 列表格。
这里用合成数据钉住算法（不调模型、不执行 SQL），并用真实存档回归一次。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.run_eval import recompute_extra_metrics  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class _Hit:
    def __init__(self, table: str) -> None:
        self.table = table


class _FakeIndex:
    """检索命中表由测试指定：{"问题关键词": [表...]}"""

    def __init__(self, mapping: dict[str, list[str]]) -> None:
        self.mapping = mapping

    def search(self, question: str, top_k=None):
        return [_Hit(t) for t in self.mapping.get(question, [])]


def test_recompute_metrics_from_archived_rows():
    rows = [
        # 执行成功 + 参考表在 Top-K 内
        {"id": "agg-01", "expect": "answer", "question": "问A", "stage": "done", "attempts": 1},
        # 执行失败（护栏/报错），且参考表不在 Top-K
        {"id": "join-01", "expect": "answer", "question": "问B", "stage": "failed", "attempts": 1},
        # 首次失败 → 回环救回（执行成功），参考表在 Top-K
        {"id": "time-01", "expect": "answer", "question": "问C", "stage": "done",
         "attempts": 2, "repaired": True},
        # 首次失败且没救回
        {"id": "time-02", "expect": "answer", "question": "问D", "stage": "failed",
         "attempts": 2, "repaired": False},
        # 陷阱题不参与 EX/可执行率统计
        {"id": "trap-01", "expect": "reject", "question": "问E", "stage": "denied", "attempts": 1},
    ]
    index = _FakeIndex({"问A": ["task"], "问B": ["user"], "问C": ["task", "user"], "问D": ["task"]})
    cases = ROOT / "eval" / "cases.yaml"
    got = recompute_extra_metrics(rows, cases_path=cases, index=index)

    # 可执行率：4 个 answer 题里 2 个 done
    assert got["sql_exec_total"] == 4 and got["sql_exec_rate"] == 0.5
    # 首次修复：2 题曾失败，1 题救回
    assert got["first_fix_attempted"] == 2 and got["first_fix_rate"] == 0.5
    # Schema 召回：参考表取自 cases.yaml（agg-01→task、join-01→…），只断言分母与取值范围
    assert got["schema_recall_total"] >= 3
    assert 0.0 <= got["schema_recall"] <= 1.0


def test_empty_rows_do_not_divide_by_zero():
    got = recompute_extra_metrics([], cases_path=ROOT / "eval" / "cases.yaml",
                                  index=_FakeIndex({}))
    assert got["sql_exec_rate"] == 0.0 and got["first_fix_rate"] == 0.0


def test_real_archived_run_recomputes_to_plausible_numbers():
    """真实存档回归：三项都应在 0~1 之间，且可执行率不会低于 EX"""
    files = sorted((ROOT / "eval" / "out").glob("eval-baseline-2026*.json"))
    full = [f for f in files if len(json.loads(f.read_text(encoding="utf-8"))["cases"]) == 60]
    if not full:
        return                      # 没有存档时跳过（CI 里 eval/out 是 gitignore 的）
    data = json.loads(full[-1].read_text(encoding="utf-8"))
    got = recompute_extra_metrics(data["cases"])

    assert 0.0 <= got["sql_exec_rate"] <= 1.0
    assert got["sql_exec_rate"] >= data["metrics"]["ex_rate"], "能执行是能答对的前提"
    assert 0.0 <= got["schema_recall"] <= 1.0
    assert 0.0 <= got["first_fix_rate"] <= 1.0
