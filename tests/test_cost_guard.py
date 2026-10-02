# -*- coding: utf-8 -*-
"""护栏③（EXPLAIN 扫描行数阈值）的回归测试。

修掉的 bug：这一层**从来没生效过** —— 生产路径 agent/nl2sql.py 的两处调用都
显式传了 check_cost=False（而紧邻的注释还写着"护栏②③在 executor 内"），
于是「三层护栏」实际只有两层在跑，而 README / 方案 / 面试讲稿 / 视频脚本
共 5 处把它当卖点。

本文件守四件事：
1. 生产路径不得再关闭成本护栏（源码级守卫，防复发）；
2. 超阈值时确实抛 SqlCostError（异常链路已通：→ too_expensive → 定向回环收窄范围）；
3. 阈值配置必须落在"会真的拦住东西"的量级，而不是形同虚设的天文数字；
4. sqlite 下必须显式报"无法估算"，不能谎报 0 行。
"""
import ast
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import executor as ex              # noqa: E402
from agent import policy                      # noqa: E402
from agent.policy import ROLE_ADMIN, Principal   # noqa: E402
from config import GUARD, IS_SQLITE           # noqa: E402

ADMIN = Principal(user_id=1, role=ROLE_ADMIN)
SQL = "SELECT COUNT(*) AS c FROM task WHERE deleted = 0"
ROOT = Path(__file__).resolve().parents[1]


def _rw():
    return policy.rewrite(SQL, ADMIN)


def test_production_path_does_not_disable_cost_guard():
    """源码级守卫：agent/nl2sql.py 里不得再有「以关键字实参关闭成本护栏」的调用。

    用 **AST 而不是文本搜索**：代码注释里会提到这个历史 bug 的原文
    （"此处曾传 check_cost=False"），文本搜索会把注释也判成违规 —— 本测试第一版
    就是这么被自己绊倒的。AST 只看真正的调用实参，注释与文档字符串一律不算。
    """
    src = (ROOT / "agent" / "nl2sql.py").read_text(encoding="utf-8")
    offenders = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if (kw.arg == "check_cost"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value is False):
                    offenders.append(node.lineno)
    assert not offenders, (
        "agent/nl2sql.py 第 %s 行以 check_cost=False 关闭了成本护栏 —— "
        "护栏③会退回死代码（正是本次修掉的 bug）" % offenders)


def test_cost_guard_blocks_when_estimate_exceeds_threshold(monkeypatch):
    """超阈值必须拒绝执行，而不是放过去。"""
    monkeypatch.setattr(ex, "explain_rows", lambda cur, sql: GUARD.explain_row_limit * 10)
    with pytest.raises(ex.SqlCostError) as err:
        ex.execute_readonly(_rw())
    assert "超过阈值" in str(err.value)


def test_cost_guard_passes_and_records_estimate(monkeypatch):
    """未超阈值时正常执行，并且**记录**实测估算值（便于排查与观测）。"""
    monkeypatch.setattr(ex, "explain_rows", lambda cur, sql: 1)
    qr = ex.execute_readonly(_rw())
    assert qr.row_count == 1
    assert qr.explain_rows == 1


def test_threshold_default_is_meaningful():
    """阈值默认值必须落在「会真的拦住东西」的量级。

    依据（2026-10-02 实测）：把 eval/cases.yaml 里 56 条可执行 reference_sql 在
    真实 treatbord 库上跑 EXPLAIN，合法查询估算 min=1 / p50=8 / p90=22 / max=800。
    - 太小（如历史值 100）会误杀正常聚合；
    - 太大（如历史默认 100000）等于永不触发，这层就白写了。
    这里直接读 config.py 的**默认值**（读运行时环境会被 .env 干扰），
    要求落在 1e3 ~ 5e4 之间。
    """
    src = (ROOT / "config.py").read_text(encoding="utf-8")
    m = re.search(r'_env\("SQL_EXPLAIN_ROW_LIMIT",\s*"(\d+)"\)', src)
    assert m, "config.py 里找不到 SQL_EXPLAIN_ROW_LIMIT 的默认值"
    default = int(m.group(1))
    assert 1_000 <= default <= 50_000, (
        "阈值默认值 %d 不在可用区间：过小会误杀正常聚合，过大等于关闭这层" % default)


def test_explain_rows_is_testable(monkeypatch):
    """explain_rows 的返回契约：int（可估算）或 None（本后端无法估算）。"""
    monkeypatch.setattr(ex, "explain_rows", lambda cur, sql: None)
    # None 不得被当成 0 去比较（历史实现会在 sqlite 下返回 0，掩盖"未测量"）
    qr = ex.execute_readonly(_rw(), row_limit=1)
    assert qr.row_count == 1
    assert qr.explain_rows is None


def test_sqlite_reports_none_not_zero():
    """sqlite 后端无法估算行数 → 必须报 None。"""
    if not IS_SQLITE:
        pytest.skip("仅在 sqlite 后端下有意义")
    with ex.closing(ex.connect()) as conn:
        with ex._CursorCtx(conn) as cur:
            assert ex.explain_rows(cur, SQL) is None
