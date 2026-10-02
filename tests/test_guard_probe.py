# -*- coding: utf-8 -*-
"""护栏探针的回归测试 —— 重点是证明它**可以失败**（不是同义反复）。

原来的「危险操作执行率 0%」判定式是 `not r.ok or not r.sql or is_safe(...)`，
被拦必然判 SAFE；而跑分时 danger_generated=0（模型自己就拒答了），
护栏一次都没触发 —— 那个指标无论如何都是 0%，没有信息量。
本探针的分母来自用例集，并且下面第二、三个用例专门证明它会红。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval import guard_probe                        # noqa: E402
from eval import security_suite as ss               # noqa: E402


def test_probe_runs_offline_and_passes():
    r = guard_probe.run()
    assert r["probe_total"] == 54, "分母应是 49 条 YAML + 5 条内置"
    assert r["probe_blocked"] == 54
    assert r["probe_leaks"] == 0
    assert r["probe_ok"] is True
    assert r["probe_block_rate"] == 1.0


def test_probe_can_fail_when_a_case_leaks(monkeypatch):
    """把任意一条用例改成失败 -> 探针必须变红（证明它不是恒真）"""
    real = ss.check_case

    def broken(case):
        if case.get("id") == "other-user-01":
            return ss.Outcome("other-user-01", "他人数据", False, "人为失败", leak=True)
        return real(case)

    monkeypatch.setattr(ss, "check_case", broken)
    r = guard_probe.run()
    assert r["probe_ok"] is False
    assert r["probe_leaks"] >= 1
    assert r["probe_blocked"] == 53


def test_probe_can_fail_when_a_case_is_blocked_but_not_leaking(monkeypatch):
    """失败但不算泄漏（如写操作被拒）也要让探针变红"""

    def broken(case):
        return ss.Outcome(case.get("id", "?"), "写操作", False, "人为失败", leak=False)

    monkeypatch.setattr(ss, "check_case", broken)
    r = guard_probe.run()
    assert r["probe_ok"] is False
    assert r["probe_blocked"] < r["probe_total"]


def test_cli_exit_code_follows_probe(capsys):
    assert guard_probe.main([]) == 0
    out = capsys.readouterr().out
    assert "护栏有效性探针" in out and "通过" in out
    assert guard_probe.main(["--json"]) == 0
