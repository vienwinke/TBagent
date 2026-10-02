# -*- coding: utf-8 -*-
"""NL2SQL 链路单测：用桩函数模拟模型输出，验证「生成→护栏→执行→回环修复」全链路（不需要 API Key）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import nl2sql  # noqa: E402
from agent import prompts_user  # noqa: E402
from agent.policy import Principal  # noqa: E402

GOOD = {"sql": "SELECT COUNT(*) AS 任务数 FROM task WHERE deleted = 0", "reason": "统计任务总数"}


def stub(*responses):
    """按顺序返回预设响应；用完后重复最后一个"""
    box = {"i": 0}

    def fn(messages):
        i = min(box["i"], len(responses) - 1)
        box["i"] += 1
        r = responses[i]
        if isinstance(r, Exception):
            raise r
        return r

    return fn


def test_happy_path_real_db():
    """真实执行：SELECT COUNT(*) FROM task → 8 行"""
    r = nl2sql.answer("任务一共有多少个？", llm_fn=stub(GOOD))
    assert r.ok, r.error
    assert r.attempts == 1 and not r.repaired
    assert r.query["row_count"] == 1
    assert list(r.rows[0].values())[0] == 8
    assert "LIMIT" in r.sql.upper()
    assert "user" in r.tables or "task" in r.tables


def test_guard_rejection_then_repair():
    """第一次给出写操作 → 被护栏拒绝 → 回环修复成功"""
    bad = {"sql": "UPDATE task SET title='x' WHERE id=1", "reason": "误判"}
    r = nl2sql.answer("把任务标题改掉", llm_fn=stub(bad, GOOD))
    assert r.ok and r.repaired and r.attempts == 2
    assert r.sql.upper().startswith("SELECT")


def test_multi_statement_rejected_then_repair():
    bad = {"sql": "SELECT 1; DROP TABLE user", "reason": "注入尝试"}
    r = nl2sql.answer("删除用户表", llm_fn=stub(bad, GOOD))
    assert r.ok and r.repaired


def test_unknown_table_then_repair():
    bad = {"sql": "SELECT COUNT(*) FROM mysql.user", "reason": "越权"}
    r = nl2sql.answer("有多少个系统用户", llm_fn=stub(bad, GOOD))
    assert r.ok and r.repaired


def test_execution_error_then_repair():
    """列名臆造 → MySQL 1054 → 回环修复"""
    bad = {"sql": "SELECT nosuch_column FROM task LIMIT 10", "reason": "臆造列"}
    r = nl2sql.answer("查一下不存在的东西", llm_fn=stub(bad, GOOD))
    assert r.ok and r.repaired and r.attempts == 2


def test_zero_rows_then_repair():
    """条件写错导致 0 行 → 触发修复"""
    bad = {"sql": "SELECT id FROM task WHERE status='NOT_A_STATUS' LIMIT 10", "reason": "枚举错"}
    r = nl2sql.answer("查询某个状态的任务", llm_fn=stub(bad, GOOD))
    assert r.ok and r.repaired


def test_repair_exhausted_returns_error():
    """一直给坏 SQL → 达到重试上限后返回失败（不抛异常）"""
    bad = {"sql": "DELETE FROM task", "reason": "坏"}
    r = nl2sql.answer("删掉任务", llm_fn=stub(bad), max_repair=1)
    assert not r.ok and r.attempts == 2 and r.error


def test_malformed_model_output():
    """模型输出缺 sql 字段 → 报错并回环"""
    r = nl2sql.answer("随便问问", llm_fn=stub({"reason": "忘了给 sql"}, GOOD), max_repair=1)
    assert r.ok and r.repaired


def test_llm_exception_is_captured():
    r = nl2sql.answer("随便问问", llm_fn=stub(RuntimeError("429 Too Many Requests")), max_repair=0)
    assert not r.ok and "429" in (r.error or "")


def test_dry_run_skips_execution():
    r = nl2sql.answer("任务数", llm_fn=stub(GOOD), execute=False)
    assert r.ok and r.query == {} and "LIMIT" in r.sql.upper()


def test_prompt_injects_domain_rules_and_schema():
    """提示词必须注入业务规则 + 检索到的 schema（G2 后来源换成嵌入版提示词包）

    这条测的是"注入"，不是"用哪个模块"：模块从 agent.prompts 换成 agent.prompts_user 后，
    原来三条断言（业务规则 / schema / 严格 JSON）必须继续成立。
    """
    principal = Principal(user_id=7)
    msgs = prompts_user.nl2sql_messages(
        "表 task（任务主表）\n  status varchar(20) 状态", "待接取任务数", principal)
    text = msgs[0]["content"] + msgs[1]["content"]
    assert "OPEN" in text and "deleted = 0" in text          # 业务规则注入
    assert "表 task" in text                                  # 检索到的 schema 注入
    assert "严格 JSON" in msgs[0]["content"]
    # 嵌入版特有：身份策略前置 + {{ME}} 占位符纪律（普通用户的提示词里不得有字面身份常量）
    assert "user_id = 7" in msgs[0]["content"]
    assert "{{ME}}" in text


def test_privileged_role_gets_admin_prompt():
    """OPERATOR 也必须拿到运营版提示词（用 is_privileged 而非 is_admin 判据）"""
    user_msgs = prompts_user.nl2sql_messages("表 task", "任务数", Principal(user_id=7))
    op_msgs = prompts_user.nl2sql_messages("表 task", "任务数", Principal(user_id=7, role="OPERATOR"))
    assert "本角色（USER）的额外约束" in user_msgs[0]["content"]
    assert "本角色（ADMIN）说明" in op_msgs[0]["content"]


def test_guard_stats_recorded():
    r = nl2sql.answer("任务数", llm_fn=stub(GOOD))
    assert r.guard["limit"] == 200 and r.guard["tables"] == ["task"]


def test_repair_failure_falls_back_to_first_result():
    """★ 修复失败不能把已拿到的答案丢掉。

    实测场景：第 1 次生成成功但查到 0 行 → 触发回环修复 → 修复调用超时。
    修复只是"尽力而为"的增强，此时应**回退到首次结果**（0 行也是有意义的答案），
    而不是让用户白等一场、最后看到报错。
    """
    from agent import nl2sql
    from agent.policy import Principal

    calls = {"n": 0}

    def flaky_llm(messages):
        calls["n"] += 1
        if calls["n"] == 1:
            # 必须真的返回 0 行，才会触发回环修复（注意 COUNT(*) 会返回 1 行——测试踩过）
            return {"sql": "SELECT id FROM task WHERE id = -1",
                    "reason": "第一次（0 行）"}
        raise RuntimeError("生成阶段失败: 模型调用失败（尝试 2 次）: APITimeoutError")

    r = nl2sql.answer("待接取的任务有几个？", principal=Principal(user_id=7),
                      llm_fn=flaky_llm, use_cache=False)

    assert r.stage == "done", "修复失败也要给出答案：%s" % r.error
    assert r.fell_back is True and r.repaired is False, "这是回退，不是修复成功"
    assert r.query is not None and r.query.get("row_count") == 0, "回退结果应是首次的 0 行"
    assert calls["n"] >= 2, "确实尝试过修复"
